from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from demo_trading.engine import SimulationEngine
from demo_trading.models import RiskConfig, Signal
from fund_chat import ChatOrchestrator
from fund_chat.audit import AuditLog

from .ticks import DeterministicTickSource
from trading_automation import AutomationService
from trading_automation.providers import market_provider_from_env


@dataclass(frozen=True)
class RuntimeConfig:
    seed: int = 7
    symbol: str = "DEMO"
    start_price: Decimal = Decimal("100")
    interval_seconds: float = 1.0
    quantity: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0 or self.quantity <= 0:
            raise ValueError("interval_seconds and quantity must be positive")


class LocalRuntime:
    """Thread-safe local demo worker with durable runtime events."""

    def __init__(self, state_dir: Path, config: RuntimeConfig | None = None,
                 risk_config: RiskConfig | None = None):
        self.state_dir = Path(state_dir)
        self.config = config or RuntimeConfig()
        self.events = AuditLog(self.state_dir / "runtime-events.jsonl")
        self.chat = ChatOrchestrator(self.state_dir / "chat-events.jsonl", self._apply_proposal)
        self.automation = AutomationService(self.state_dir, market_provider_from_env())
        self.ticks = DeterministicTickSource(self.config.seed, self.config.symbol, self.config.start_price)
        self.engine = SimulationEngine({self.config.symbol: self.config.start_price}, risk_config, self.state_dir / "engine")
        self.status = "idle"
        self.error: str | None = None
        self._previous: Decimal | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._requests: set[str] = set()
        self._restore_runtime()
        self._reconcile_simulation_fills()

    def _restore_runtime(self) -> None:
        tick_count = 0
        for event in self.events.read():
            payload = event["payload"]
            if "request_id" in payload:
                self._requests.add(payload["request_id"])
            if event["event_type"] == "tick":
                tick_count += 1
                self._previous = Decimal(payload["price"])
        # Replay only the private PRNG (not trading) to continue the deterministic
        # stream without reusing tick/order identifiers after restart.
        for _ in range(tick_count):
            self.ticks.next()
        # A process restart never resumes unattended execution.
        if list(self.events.read()):
            self.status = "stopped"

    def _reconcile_simulation_fills(self) -> None:
        """Backfill the paper ledger from the durable simulation-engine journal.

        The local runtime intentionally assumes a single process/worker owns its
        state directory.  The automation ledger uses ``source_fill_id`` as its
        idempotency key, so replay is safe after either a completed save or a
        crash between the engine-journal append and automation-state replace.
        """
        path = self.state_dir / "engine" / "events.jsonl"
        if not path.exists():
            return
        positions: dict[str, tuple[Decimal, Decimal]] = {}
        rationales: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            order_id = str(item["order_id"])
            payload = item["payload"]
            if item["event_type"] == "signal_received":
                rationales[order_id] = str(payload.get("pm_reason", ""))
                continue
            if item["event_type"] != "order_filled":
                continue
            symbol = str(payload["symbol"])
            side = str(payload["side"])
            quantity = Decimal(payload["quantity"])
            price = Decimal(payload["fill_price"])
            commission = Decimal(payload["commission"])
            held, average = positions.get(symbol, (Decimal("0"), Decimal("0")))
            if side == "BUY":
                new_held = held + quantity
                new_average = ((held * average + quantity * price) / new_held
                               if new_held else Decimal("0"))
                pnl = -commission
            else:
                new_held = held - quantity
                new_average = average if new_held else Decimal("0")
                pnl = quantity * (price - average) - commission
            positions[symbol] = (new_held, new_average)
            self.automation.record_simulation_fill({
                "fill_id": order_id, "symbol": symbol, "team": "scalping",
                "side": side, "quantity": quantity, "price": price, "pnl": pnl,
                "rationale": rationales.get(order_id) or
                "Recovered demo-only simulation fill from the durable local engine journal; no broker route or live ordering is available.",
            })

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append(kind, payload)

    def _claim(self, request_id: str) -> None:
        if not request_id or request_id in self._requests:
            raise ValueError("request_id must be non-empty and unique")
        self._requests.add(request_id)

    def _apply_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        # Proposals are recorded, never executed as code or connected externally.
        result = {"applied_to": "demo_runtime_notes", "proposal_id": proposal["proposal_id"]}
        self._emit("proposal_applied", result)
        return result

    def start(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            self._claim(request_id)
            if self._thread and self._thread.is_alive():
                raise ValueError("worker already running")
            self._stop.clear()
            self.error = None
            self.status = "running"
            self._emit("worker_started", {"request_id": request_id, "config": self.config_json()})
            self._thread = threading.Thread(target=self._loop, name="demo-scalper", daemon=True)
            self._thread.start()
            return self.status_json()

    def pause(self, request_id: str) -> dict[str, Any]:
        return self._halt(request_id, "paused")

    def stop(self, request_id: str) -> dict[str, Any]:
        return self._halt(request_id, "stopped")

    def _halt(self, request_id: str, target: str) -> dict[str, Any]:
        with self._lock:
            self._claim(request_id)
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.config.interval_seconds * 2))
        with self._lock:
            self.status = target
            self._emit(f"worker_{target}", {"request_id": request_id})
            return self.status_json()

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self.step()
                # Schedule checks are timezone-aware and idempotent; unavailable
                # providers remain an explicit no-op rather than using fake data.
                self.automation.run_due()
                self._stop.wait(self.config.interval_seconds)
        except Exception as exc:
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "error_stopped"
                self._stop.set()
                self._emit("worker_error_stopped", {"error": self.error})

    def step(self) -> dict[str, Any]:
        with self._lock:
            tick = self.ticks.next()
            self.engine.prices[tick.symbol] = tick.price
            self._emit("tick", tick.json())
            side = None
            if self._previous is not None and tick.price != self._previous:
                side = "BUY" if tick.price > self._previous else "SELL"
            self._previous = tick.price
            if side is None:
                return {"tick": tick.json(), "signal": None}
            # Never short: a falling tick without inventory is an observed/no-op signal.
            position = self.engine.positions.get(tick.symbol)
            if side == "SELL" and (position is None or position.quantity < self.config.quantity):
                self._emit("signal_skipped", {"tick_sequence": tick.sequence, "reason": "no_demo_inventory"})
                return {"tick": tick.json(), "signal": None}
            order_id = f"demo-{self.config.seed}-{tick.sequence}-{side.lower()}"
            signal = Signal(order_id, tick.symbol, side, self.config.quantity, tick.price,
                            "ScalpingDemoWorker", True,
                            "explicit demo-only auto-approval policy; deterministic tick momentum, bounded quantity, no broker route, and simulation ledger only")
            self._emit("signal_generated", signal.json())
            prior = self.engine.positions.get(tick.symbol)
            prior_average = prior.average_price if prior is not None else Decimal("0")
            before = len(self.engine.events)
            self.engine.process(signal)
            new_events = self.engine.events[before:]
            engine_path = self.state_dir / "engine" / "events.jsonl"
            engine_path.parent.mkdir(parents=True, exist_ok=True)
            with engine_path.open("a", encoding="utf-8") as handle:
                for item in new_events:
                    handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            for item in new_events:
                self._emit("engine_event", item)
                if item["event_type"] == "order_filled":
                    fill = item["payload"]
                    commission = Decimal(fill["commission"])
                    pnl = (-commission if fill["side"] == "BUY" else
                           Decimal(fill["quantity"]) * (Decimal(fill["fill_price"]) - prior_average) - commission)
                    self.automation.record_simulation_fill({
                        "fill_id": item["order_id"], "symbol": fill["symbol"],
                        "team": "scalping", "side": fill["side"], "quantity": fill["quantity"],
                        "price": fill["fill_price"], "pnl": pnl, "rationale": signal.pm_reason,
                    })
            return {"tick": tick.json(), "signal": signal.json(), "engine_events": new_events}

    def status_json(self) -> dict[str, Any]:
        return {"mode": "simulation", "status": self.status, "error": self.error,
                "tick_sequence": self.ticks.sequence, "config": self.config_json()}

    def config_json(self) -> dict[str, Any]:
        raw = asdict(self.config)
        raw["start_price"] = str(raw["start_price"])
        raw["quantity"] = str(raw["quantity"])
        return raw

    def results(self) -> dict[str, Any]:
        with self._lock:
            result = self.engine.run([])
            result["runtime"] = self.status_json()
            return result

    def event_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.events.read())
