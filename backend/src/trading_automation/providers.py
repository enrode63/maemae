from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
import os
from statistics import fmean, pstdev
from typing import Callable, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen


class MarketDataProvider(Protocol):
    name: str
    def symbols(self, index: str) -> list[str]: ...
    def snapshot(self, symbols: list[str]) -> dict[str, dict]: ...


class ReportProvider(Protocol):
    name: str
    def generate(self, market: str, snapshots: dict[str, dict]) -> str: ...


@dataclass(frozen=True)
class UnconfiguredProvider:
    """Fail-closed adapter used until an explicitly supplied provider is configured."""
    kind: str
    name: str = "unconfigured"

    @property
    def status(self) -> dict[str, str | bool]:
        return {"configured": False, "name": self.name,
                "state": "not_configured", "kind": self.kind}

    def symbols(self, index: str) -> list[str]:
        raise ProviderUnavailable(f"{self.kind} provider is not configured")

    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        raise ProviderUnavailable(f"{self.kind} provider is not configured")

    def generate(self, market: str, snapshots: dict[str, dict]) -> str:
        raise ProviderUnavailable(f"{self.kind} provider is not configured")


class ProviderUnavailable(RuntimeError):
    pass


class _IndexTableParser(HTMLParser):
    """Extract a Symbol/Ticker column from Wikipedia's constituent tables."""
    def __init__(self) -> None:
        super().__init__()
        self.in_cell = False
        self.cell = []
        self.row = []
        self.rows = []
        self.tables = []
        self.in_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.in_table, self.rows = True, []
        elif tag in {"td", "th"} and self.in_table:
            self.in_cell, self.cell = True, []

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []
        elif tag == "table" and self.in_table:
            if self.rows:
                self.tables.append(self.rows)
            self.in_table = False


class FreeEodMarketDataProvider:
    """Keyless, read-only Yahoo EOD adapter; it never exposes an order route."""
    name = "free-eod-yahoo"
    _INDEX_URLS = {
        "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "nasdaq100": "https://en.wikipedia.org/wiki/Nasdaq-100",
    }

    def __init__(self, fetch: Callable[[str, float], bytes] | None = None,
                 timeout: float = 8.0, max_age_days: int = 5) -> None:
        if timeout <= 0 or max_age_days <= 0:
            raise ValueError("timeout and max_age_days must be positive")
        self.timeout, self.max_age_days = timeout, max_age_days
        self._fetch = fetch or self._download
        self._symbols: dict[str, list[str]] = {}

    @property
    def status(self) -> dict[str, str | bool]:
        return {"configured": True, "name": self.name, "state": "ready", "kind": "market_data"}

    @staticmethod
    def _download(url: str, timeout: float) -> bytes:
        request = Request(url, headers={"User-Agent": "trading-journal-eod/1.0"})
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def symbols(self, index: str) -> list[str]:
        if index not in self._INDEX_URLS:
            raise ProviderUnavailable("unsupported market universe")
        if index in self._symbols:
            return list(self._symbols[index])
        try:
            parser = _IndexTableParser()
            parser.feed(self._fetch(self._INDEX_URLS[index], self.timeout).decode("utf-8"))
            symbols: list[str] = []
            for table in parser.tables:
                for row_index, row in enumerate(table):
                    header = [value.lower() for value in row]
                    column = next((i for i, value in enumerate(header) if value in {"symbol", "ticker"}), None)
                    if column is None:
                        continue
                    for values in table[row_index + 1:]:
                        if len(values) <= column:
                            continue
                        value = values[column].upper().replace(".", "-").strip()
                        if value and value.replace("-", "").isalnum() and value not in symbols:
                            symbols.append(value)
                    break
                if symbols:
                    break
            if len(symbols) < (400 if index == "sp500" else 90):
                raise ProviderUnavailable("market universe response was incomplete")
            self._symbols[index] = symbols
            return list(symbols)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable("market universe unavailable") from exc

    def snapshot(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            raise ProviderUnavailable("no symbols requested")
        # Bound concurrency so a full index cycle is usable without creating an
        # unbounded request burst against the free endpoint.
        with ThreadPoolExecutor(max_workers=min(12, len(symbols))) as pool:
            values = list(pool.map(self._snapshot_one, symbols))
        result = dict(zip(symbols, values))
        if set(result) != set(symbols):
            raise ProviderUnavailable("market snapshot was incomplete")
        return result

    def _snapshot_one(self, symbol: str) -> dict:
        yahoo_symbol = {"BTC": "BTC-USD", "ETH": "ETH-USD"}.get(symbol, symbol)
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{quote(yahoo_symbol)}?range=3mo&interval=1d&events=history")
        try:
            payload = json.loads(self._fetch(url, self.timeout).decode("utf-8"))
            chart = payload["chart"]["result"][0]
            timestamps = chart["timestamp"]
            quote_data = chart["indicators"]["quote"][0]
            rows = []
            for ts, close, volume_value in zip(timestamps, quote_data["close"], quote_data["volume"]):
                if close is None or volume_value is None:
                    continue
                timestamp_value = float(ts)
                close_value = float(close)
                parsed_volume = float(volume_value)
                if not all(math.isfinite(value) for value in
                           (timestamp_value, close_value, parsed_volume)):
                    raise ProviderUnavailable("market snapshot contains non-finite data")
                if close_value > 0 and parsed_volume >= 0:
                    rows.append((int(timestamp_value), close_value, parsed_volume))
            if len(rows) < 20:
                raise ValueError("insufficient history")
            age = (datetime.now(timezone.utc).timestamp() - rows[-1][0]) / 86400
            if age < -1 or age > self.max_age_days:
                raise ProviderUnavailable("market snapshot is stale")
            closes, volumes = [r[1] for r in rows], [r[2] for r in rows]
            sma20, avg_volume = fmean(closes[-20:]), fmean(volumes[-20:])
            returns = [(b / a) - 1 for a, b in zip(closes[-20:-1], closes[-19:])]
            momentum = 50 + max(-50, min(50, (closes[-1] / closes[-6] - 1) * 500))
            trend = 75 if closes[-1] >= sma20 else 25
            volume = 50 if avg_volume == 0 else max(0, min(100, volumes[-1] / avg_volume * 50))
            risk = max(0, min(100, 100 - pstdev(returns) * math.sqrt(252) * 100))
            regime = 70 if closes[-1] >= sma20 else 30
            calculated = (sma20, avg_volume, *returns, momentum, trend, volume, risk, regime)
            if not all(math.isfinite(value) for value in calculated):
                raise ProviderUnavailable("market snapshot calculation was non-finite")
            signals = {team: {"trend": trend, "momentum": momentum, "volume": volume,
                              "volatility_risk": risk, "support_resistance": trend,
                              "market_regime": regime} for team in ("scalping", "day", "swing", "longterm")}
            return {"close": closes[-1], "price": closes[-1], "volume": volumes[-1],
                    "as_of": datetime.fromtimestamp(rows[-1][0], timezone.utc).isoformat(),
                    "data_quality": "free", "side": "BUY" if closes[-1] >= sma20 else "SELL",
                    "signals": signals}
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable("market snapshot unavailable") from exc


def market_provider_from_env(environ: dict[str, str] | None = None) -> object:
    """Opt-in configuration. Missing/unknown values remain explicitly fail-closed."""
    value = (environ or os.environ).get("MARKET_DATA_PROVIDER", "").strip().lower()
    if value == "free_eod":
        return FreeEodMarketDataProvider()
    return UnconfiguredProvider("market_data")


def provider_status(provider: object, kind: str) -> dict[str, str | bool]:
    allowed = ("configured", "name", "state", "kind")
    try:
        status = getattr(provider, "status", None)
        if isinstance(status, dict):
            safe = {key: status[key] for key in allowed if key in status}
            return {"configured": bool(safe.get("configured", True)),
                    "name": str(safe.get("name", type(provider).__name__)),
                    "state": str(safe.get("state", "ready")), "kind": kind}
        return {"configured": True, "name": str(getattr(provider, "name", type(provider).__name__)),
                "state": "ready", "kind": kind}
    except Exception:
        return {"configured": False, "name": type(provider).__name__,
                "state": "error", "kind": kind}
