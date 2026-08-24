import csv
import json
from decimal import Decimal
from pathlib import Path

from .models import Signal, finite_decimal


def load_prices(path: Path) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("symbol") or not row.get("price"):
                raise ValueError("price CSV requires symbol and price")
            price = finite_decimal(row["price"], "market price")
            if price <= 0:
                raise ValueError("market price must be positive")
            prices[row["symbol"].upper()] = price
    return prices


def load_signals(path: Path) -> list[Signal]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("signals JSON must be an array")
    return [Signal.from_dict(item) for item in raw]
