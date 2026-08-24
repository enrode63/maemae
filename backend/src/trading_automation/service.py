from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .providers import ProviderUnavailable, UnconfiguredProvider, provider_status
from .schedule import schedule_state


@dataclass(frozen=True)
class AutomationConfig:
    rationale_min_length: int = 60


TRADING_TEAMS = frozenset({"scalping", "day", "swing", "longterm"})


def normalize_team(team: str | None) -> str | None:
    """Validate a reporting team and map the explicit overall scope to None."""
    if team is None or team == "all":
        return None
    if team not in TRADING_TEAMS:
        raise ValueError("invalid team")
    return team


class AutomationService:
    """Report and paper-ledger orchestration. It deliberately has no broker adapter."""
    def __init__(self, state_dir: Path, market_provider: object | None = None,
                 report_provider: object | None = None, config: AutomationConfig | None = None):
        self.path = Path(state_dir) / "automation-state.json"
        self.market = market_provider or UnconfiguredProvider("market_data")
        self.reporter = report_provider or UnconfiguredProvider("llm_report")
        self.config = config or AutomationConfig()
        self._lock = RLock()
        self._state = {"completed_jobs": {}, "reports": [], "trades": [], "evaluations": []}
        if self.path.exists():
            self._state.update(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        return {"mode": "simulation", "live_ordering": False,
                "providers": {"market_data": provider_status(self.market, "market_data"),
                              "llm_report": provider_status(self.reporter, "llm_report")},
                "schedule": schedule_state(now), "rationale_min_length": self.config.rationale_min_length}

    def universe(self) -> dict[str, Any]:
        if not provider_status(self.market, "market_data")["configured"]:
            return {"status": "not_configured", "us_equities": [], "crypto": ["BTC", "ETH"],
                    "rule": "S&P500 union Nasdaq100, duplicates removed"}
        sp = self.market.symbols("sp500")
        ndx = self.market.symbols("nasdaq100")
        return {"status": "ready", "us_equities": sorted(set(sp) | set(ndx)), "crypto": ["BTC", "ETH"],
                "counts": {"sp500": len(set(sp)), "nasdaq100": len(set(ndx)), "deduplicated": len(set(sp) | set(ndx))}}

    def run_due(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        schedule = schedule_state(now)
        results = []
        with self._lock:
            for market in ("us", "crypto"):
                key = f"{market}:{schedule[market]['local_date']}"
                if not schedule[market]["due"] or key in self._state["completed_jobs"]:
                    results.append({"market": market, "state": "not_due_or_already_completed"})
                    continue
                try:
                    universe = self.universe()
                    symbols = universe["us_equities"] if market == "us" else universe["crypto"]
                    if not symbols:
                        raise ProviderUnavailable("market universe unavailable")
                    snapshots = self.market.snapshot(symbols)
                    content = self.reporter.generate(market, snapshots)
                    report = {"id": uuid4().hex, "market": market, "scheduled_date": schedule[market]["local_date"],
                              "created_at": now.isoformat(), "symbols": symbols, "content": str(content)}
                    self._state["reports"].append(report)
                    self._state["completed_jobs"][key] = report["id"]
                    results.append({"market": market, "state": "generated", "report_id": report["id"]})
                except ProviderUnavailable as exc:
                    results.append({"market": market, "state": "blocked_not_configured", "reason": str(exc)})
                except Exception:
                    results.append({"market": market, "state": "provider_error"})
            self._save()
        return {"mode": "simulation", "jobs": results}

    def reports(self, team: str | None = None) -> dict[str, Any]:
        with self._lock:
            selected = normalize_team(team)
            performance = self.performance(selected)
            allocation_values = {key: Decimal(value) for key, value in performance["asset_allocation"].items()}
            allocation_total = sum(allocation_values.values(), Decimal("0"))
            allocation = [{"asset": asset, "weight": float(value / allocation_total * 100)}
                          for asset, value in allocation_values.items()] if allocation_total else []
            evaluations = [item for item in self._state["evaluations"]
                           if selected is None or item["team"] == selected]
            latest = evaluations[-1] if evaluations else {}
            report_teams = [selected] if selected else sorted(TRADING_TEAMS)
            teams = []
            for name in report_teams:
                item = self.performance(name)
                teams.append({"team": name, "teamName": name, "title": "주간 리포트",
                              "returnRate": float(item["return_pct"]),
                              "summary": f"{item['trade_count']} paper trades recorded."})
            return {"kpis": {"cumulativePnl": float(performance["pnl"]),
                              "averageRr": float(performance["rr"]) if performance["rr"] is not None else None,
                              "positions": sum(Decimal(value) != 0 for value in performance["positions"].values()),
                              "winRate": float(Decimal(performance["win_rate"]) * 100) if performance["win_rate"] is not None else None,
                              "returnRate": float(performance["return_pct"])},
                    "teams": teams, "assetAllocation": allocation,
                    "weekly": {"period": latest.get("week", "최근 7일"),
                               "returnRate": float(latest.get("performance", {}).get("return_pct", 0)),
                               "wins": latest.get("strengths", []),
                               "improvements": latest.get("improvements", [])},
                    "sentiment": {"crypto": {}, "usStocks": {}},
                    "updatedAt": datetime.now(timezone.utc).isoformat()}

    def record_trade(self, raw: dict[str, Any]) -> dict[str, Any]:
        required = ("symbol", "team", "side", "quantity", "price", "rationale")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"missing trade fields: {', '.join(missing)}")
        side = str(raw["side"]).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        rationale = str(raw["rationale"]).strip()
        if len(rationale) < self.config.rationale_min_length:
            raise ValueError(f"entry rationale must be at least {self.config.rationale_min_length} characters")
        team = str(raw["team"])
        if team not in TRADING_TEAMS:
            raise ValueError("invalid team")
        symbol = str(raw["symbol"]).upper()
        universe = self.universe()
        allowed_symbols = set(universe["us_equities"]) | set(universe["crypto"])
        if symbol not in allowed_symbols:
            raise ValueError("symbol is outside the S&P500, Nasdaq100, BTC/ETH universe")
        quantity, price = Decimal(str(raw["quantity"])), Decimal(str(raw["price"]))
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be finite and positive")
        numeric = {}
        for key in ("risk", "reward", "pnl", "return_pct"):
            numeric[key] = Decimal(str(raw.get(key, "0")))
            if not numeric[key].is_finite() or (key in {"risk", "reward"} and numeric[key] < 0):
                raise ValueError(f"{key} must be finite and non-negative where applicable")
        trade = {"id": uuid4().hex, "mode": "simulation", "symbol": symbol,
                 "team": team, "side": side, "quantity": str(quantity), "price": str(price),
                 "rationale": rationale, **{key: str(value) for key, value in numeric.items()},
                 "created_at": str(raw.get("created_at") or datetime.now(timezone.utc).isoformat())}
        with self._lock:
            self._state["trades"].append(trade)
            self._save()
        return trade

    def performance(self, team: str | None = None) -> dict[str, Any]:
        selected = normalize_team(team)
        trades = [t for t in self._state["trades"] if selected is None or t["team"] == selected]
        pnl = sum((Decimal(t["pnl"]) for t in trades), Decimal("0"))
        returns = sum((Decimal(t["return_pct"]) for t in trades), Decimal("0"))
        closed = [t for t in trades if Decimal(t["pnl"]) != 0]
        wins = sum(Decimal(t["pnl"]) > 0 for t in closed)
        risk = sum((Decimal(t["risk"]) for t in trades), Decimal("0"))
        reward = sum((Decimal(t["reward"]) for t in trades), Decimal("0"))
        positions: dict[str, Decimal] = {}
        allocation: dict[str, Decimal] = {}
        for t in trades:
            signed = Decimal(t["quantity"]) * (1 if t["side"] == "BUY" else -1)
            positions[t["symbol"]] = positions.get(t["symbol"], Decimal("0")) + signed
            allocation[t["symbol"]] = allocation.get(t["symbol"], Decimal("0")) + abs(Decimal(t["quantity"]) * Decimal(t["price"]))
        return {"scope": "team" if selected else "overall", "team": selected, "pnl": str(pnl),
                "rr": str(reward / risk) if risk else None, "positions": {k: str(v) for k, v in sorted(positions.items())},
                "win_rate": str(Decimal(wins) / len(closed)) if closed else None, "return_pct": str(returns),
                "asset_allocation": {k: str(v) for k, v in sorted(allocation.items())}, "trade_count": len(trades)}

    def weekly_evaluation(self, team: str | None, week: str, strengths: list[str], improvements: list[str]) -> dict[str, Any]:
        selected = normalize_team(team)
        value = {"team": selected or "all", "week": week, "performance": self.performance(selected),
                 "strengths": [str(x) for x in strengths], "improvements": [str(x) for x in improvements]}
        with self._lock:
            self._state["evaluations"].append(value)
            self._save()
        return value

    def evaluations(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._state["evaluations"])
