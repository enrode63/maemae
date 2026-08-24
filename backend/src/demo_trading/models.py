from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


def decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def finite_decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class Signal:
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    agent: str
    pm_approved: bool
    pm_reason: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Signal":
        required = ("order_id", "symbol", "side", "quantity", "limit_price", "agent", "pm_approved")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"missing signal fields: {', '.join(missing)}")
        side = str(raw["side"]).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        quantity = finite_decimal(raw["quantity"], "quantity")
        limit_price = finite_decimal(raw["limit_price"], "limit_price")
        if quantity <= 0 or limit_price <= 0:
            raise ValueError("quantity and limit_price must be positive")
        return cls(str(raw["order_id"]), str(raw["symbol"]).upper(), side, quantity,
                   limit_price, str(raw["agent"]), raw["pm_approved"] is True,
                   str(raw.get("pm_reason", "")))

    def json(self) -> dict[str, Any]:
        result = asdict(self)
        result["quantity"] = decimal_text(self.quantity)
        result["limit_price"] = decimal_text(self.limit_price)
        return result


@dataclass
class Position:
    quantity: Decimal = Decimal("0")
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")


@dataclass(frozen=True)
class RiskConfig:
    initial_cash: Decimal = Decimal("100000")
    max_order_notional: Decimal = Decimal("10000")
    max_position_quantity: Decimal = Decimal("100")
    max_total_loss: Decimal = Decimal("1000")
    commission_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("initial_cash", "max_order_notional", "max_position_quantity", "max_total_loss"):
            if getattr(self, name) == 0:
                raise ValueError(f"{name} must be positive")
