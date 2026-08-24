from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .models import Position, RiskConfig, Signal, decimal_text


class SimulationEngine:
    """Pure simulation. It contains no network, broker, credential, or withdrawal path."""

    def __init__(self, prices: dict[str, Decimal], config: RiskConfig | None = None,
                 state_dir: Path | None = None):
        if any(not p.is_finite() or p <= 0 for p in prices.values()):
            raise ValueError("market prices must be finite and positive")
        self.prices = prices
        self.config = config or RiskConfig()
        self.cash = self.config.initial_cash
        self.positions: dict[str, Position] = {}
        self.seen_orders: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self.sequence = 0
        if state_dir is not None:
            self._restore(state_dir / "events.jsonl")

    def _restore(self, path: Path) -> None:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            self.sequence = max(self.sequence, int(event["sequence"]))
            self.seen_orders.add(str(event["order_id"]))
            if event["event_type"] == "position_updated":
                data = event["payload"]
                self.cash = Decimal(data["cash"])
                self.positions[data["symbol"]] = Position(
                    Decimal(data["quantity"]), Decimal(data["average_price"]),
                    Decimal(data["realized_pnl"]), Decimal(data["fees"]))

    def _emit(self, event_type: str, order_id: str, payload: dict[str, Any]) -> None:
        self.sequence += 1
        self.events.append({"sequence": self.sequence, "event_type": event_type,
                            "order_id": order_id, "payload": payload})

    def _reject(self, signal: Signal, stage: str, reason: str) -> None:
        self._emit(stage, signal.order_id, {"accepted": False, "reason": reason})

    def process(self, signal: Signal) -> None:
        self._emit("signal_received", signal.order_id, signal.json())
        if signal.order_id in self.seen_orders:
            self._reject(signal, "order_rejected", "duplicate_order_id")
            return
        self.seen_orders.add(signal.order_id)
        market = self.prices.get(signal.symbol)
        if market is None:
            self._reject(signal, "risk_rejected", "missing_market_price")
            return
        position = self.positions.setdefault(signal.symbol, Position())
        slip = self.config.slippage_bps / Decimal("10000")
        slipped = market * (Decimal("1") + slip if signal.side == "BUY" else Decimal("1") - slip)
        fill_price = min(slipped, signal.limit_price) if signal.side == "BUY" else max(slipped, signal.limit_price)
        notional = signal.quantity * fill_price
        commission = notional * self.config.commission_rate
        projected = position.quantity + signal.quantity if signal.side == "BUY" else position.quantity - signal.quantity
        reasons = []
        if notional > self.config.max_order_notional:
            reasons.append("max_order_notional")
        if projected < 0:
            reasons.append("insufficient_position")
        if projected > self.config.max_position_quantity:
            reasons.append("max_position_quantity")
        total_pnl = sum((p.realized_pnl + p.quantity * (self.prices[s] - p.average_price) - p.fees
                         for s, p in self.positions.items()), Decimal("0"))
        if total_pnl <= -self.config.max_total_loss:
            reasons.append("max_total_loss")
        if signal.side == "BUY" and notional + commission > self.cash:
            reasons.append("insufficient_cash")
        if reasons:
            self._reject(signal, "risk_rejected", ",".join(reasons))
            return
        self._emit("risk_approved", signal.order_id, {"accepted": True, "notional": decimal_text(notional)})
        if not signal.pm_approved:
            self._reject(signal, "pm_rejected", signal.pm_reason or "pm_not_approved")
            return
        self._emit("pm_approved", signal.order_id, {"accepted": True, "reason": signal.pm_reason})
        # A limit order fills only when the deterministic market price crosses its limit.
        if (signal.side == "BUY" and market > signal.limit_price) or (signal.side == "SELL" and market < signal.limit_price):
            self._reject(signal, "order_unfilled", "limit_not_crossed")
            return
        if signal.side == "BUY":
            old_cost = position.quantity * position.average_price
            position.quantity += signal.quantity
            position.average_price = (old_cost + signal.quantity * fill_price) / position.quantity
            self.cash -= notional + commission
        else:
            position.realized_pnl += signal.quantity * (fill_price - position.average_price)
            position.quantity -= signal.quantity
            self.cash += notional - commission
            if position.quantity == 0:
                position.average_price = Decimal("0")
        position.fees += commission
        self._emit("order_filled", signal.order_id, {"symbol": signal.symbol, "side": signal.side,
                   "quantity": decimal_text(signal.quantity), "fill_price": decimal_text(fill_price),
                   "commission": decimal_text(commission)})
        self._emit("position_updated", signal.order_id, self._position_json(signal.symbol, market))

    def _position_json(self, symbol: str, market: Decimal) -> dict[str, str]:
        p = self.positions[symbol]
        unrealized = p.quantity * (market - p.average_price)
        return {"symbol": symbol, "quantity": decimal_text(p.quantity),
                "average_price": decimal_text(p.average_price),
                "realized_pnl": decimal_text(p.realized_pnl),
                "unrealized_pnl": decimal_text(unrealized), "fees": decimal_text(p.fees),
                "cash": decimal_text(self.cash),
                "total_pnl": decimal_text(p.realized_pnl + unrealized - p.fees)}

    def run(self, signals: Iterable[Signal]) -> dict[str, Any]:
        for signal in signals:
            self.process(signal)
        positions = [self._position_json(s, self.prices[s]) for s in sorted(self.positions)]
        canonical = json.dumps({"prices": {k: str(v) for k, v in sorted(self.prices.items())},
                                "config": {k: str(v) for k, v in asdict(self.config).items()},
                                "events": self.events}, sort_keys=True, separators=(",", ":"))
        total = sum((p.realized_pnl + p.quantity * (self.prices[s] - p.average_price) - p.fees
                     for s, p in self.positions.items()), Decimal("0"))
        return {"mode": "simulation", "run_id": hashlib.sha256(canonical.encode()).hexdigest()[:16],
                "cash": decimal_text(self.cash), "total_pnl": decimal_text(total),
                "positions": positions, "events": self.events}

    @staticmethod
    def write_outputs(result: dict[str, Any], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            for event in result["events"]:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
        summary = {key: value for key, value in result.items() if key != "events"}
        (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
