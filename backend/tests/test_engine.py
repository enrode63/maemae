from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from demo_trading.engine import SimulationEngine
from demo_trading.models import RiskConfig, Signal


def signal(order_id="o1", **changes):
    raw = {"order_id": order_id, "symbol": "DEMO", "side": "BUY", "quantity": "2",
           "limit_price": "101", "agent": "bull", "pm_approved": True}
    raw.update(changes)
    return Signal.from_dict(raw)


def event_types(result):
    return [e["event_type"] for e in result["events"]]


class EngineTests(unittest.TestCase):
    def test_normal_fill_and_pnl(self):
        result = SimulationEngine({"DEMO": Decimal("100")}).run([signal()])
        self.assertEqual(event_types(result), ["signal_received", "risk_approved", "pm_approved", "order_filled", "position_updated"])
        self.assertEqual(result["positions"][0]["quantity"], "2.00000000")
        self.assertEqual(result["positions"][0]["unrealized_pnl"], "-0.10000000")
        self.assertEqual(result["positions"][0]["fees"], "0.20010000")
        self.assertEqual(result["cash"], "99799.69990000")

    def test_risk_block(self):
        engine = SimulationEngine({"DEMO": Decimal("100")}, RiskConfig(max_order_notional=Decimal("100")))
        result = engine.run([signal(quantity="2")])
        self.assertEqual(event_types(result)[-1], "risk_rejected")
        self.assertEqual(result["events"][-1]["payload"]["reason"], "max_order_notional")

    def test_pm_rejection(self):
        result = SimulationEngine({"DEMO": Decimal("100")}).run([signal(pm_approved=False, pm_reason="no")])
        self.assertEqual(event_types(result)[-1], "pm_rejected")
        self.assertNotIn("order_filled", event_types(result))

    def test_duplicate_order_is_rejected(self):
        item = signal()
        result = SimulationEngine({"DEMO": Decimal("100")}).run([item, item])
        self.assertEqual(result["events"][-1]["event_type"], "order_rejected")
        self.assertEqual(result["events"][-1]["payload"]["reason"], "duplicate_order_id")
        self.assertEqual(sum(e["event_type"] == "order_filled" for e in result["events"]), 1)

    def test_reproducible_result(self):
        inputs = [signal("a"), signal("b", side="SELL", quantity="1", limit_price="99")]
        first = SimulationEngine({"DEMO": Decimal("100")}).run(inputs)
        second = SimulationEngine({"DEMO": Decimal("100")}).run(inputs)
        self.assertEqual(first, second)

    def test_insufficient_cash_and_sell_quantity_are_rejected(self):
        cfg = RiskConfig(initial_cash=Decimal("100"), max_order_notional=Decimal("1000"))
        result = SimulationEngine({"DEMO": Decimal("100")}, cfg).run([
            signal("buy", quantity="1", limit_price="101"),
            signal("sell", side="SELL", quantity="1", limit_price="99")])
        reasons = [e["payload"].get("reason") for e in result["events"] if not e["payload"].get("accepted", True)]
        self.assertIn("insufficient_cash", reasons)
        self.assertIn("insufficient_position", reasons)

    def test_limit_remains_respected_after_slippage(self):
        cfg = RiskConfig(slippage_bps=Decimal("500"))
        engine = SimulationEngine({"DEMO": Decimal("100")}, cfg)
        result = engine.run([signal("b", limit_price="101"),
                             signal("s", side="SELL", quantity="1", limit_price="99")])
        fills = [e["payload"] for e in result["events"] if e["event_type"] == "order_filled"]
        self.assertEqual(fills[0]["fill_price"], "101.00000000")
        self.assertEqual(fills[1]["fill_price"], "99.00000000")

    def test_sell_updates_cash_realized_pnl_and_fees(self):
        engine = SimulationEngine({"DEMO": Decimal("100")}, RiskConfig(slippage_bps=Decimal("0")))
        engine.process(signal("b", quantity="2", limit_price="100"))
        engine.prices["DEMO"] = Decimal("110")
        result = engine.run([signal("s", side="SELL", quantity="1", limit_price="110")])
        p = result["positions"][0]
        self.assertEqual(p["realized_pnl"], "10.00000000")
        self.assertEqual(p["unrealized_pnl"], "10.00000000")
        self.assertEqual(p["fees"], "0.31000000")
        self.assertEqual(result["cash"], "99909.69000000")

    def test_total_loss_includes_unrealized_and_fees(self):
        cfg = RiskConfig(max_total_loss=Decimal("5"), commission_rate=Decimal("0"), slippage_bps=Decimal("0"))
        engine = SimulationEngine({"DEMO": Decimal("100")}, cfg)
        engine.process(signal("b", quantity="1", limit_price="100"))
        engine.prices["DEMO"] = Decimal("90")
        result = engine.run([signal("next", quantity="1", limit_price="90")])
        self.assertEqual(result["events"][-1]["payload"]["reason"], "max_total_loss")

    def test_append_only_log_restores_duplicate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = SimulationEngine({"DEMO": Decimal("100")})
            SimulationEngine.write_outputs(first.run([signal("persistent")]), output)
            before = (output / "events.jsonl").read_text(encoding="utf-8")
            second = SimulationEngine({"DEMO": Decimal("100")}, state_dir=output)
            result = second.run([signal("persistent")])
            SimulationEngine.write_outputs(result, output)
            after = (output / "events.jsonl").read_text(encoding="utf-8")
            self.assertTrue(after.startswith(before))
            events = [json.loads(line) for line in after.splitlines()]
            self.assertEqual(sum(e["event_type"] == "order_filled" for e in events), 1)
            self.assertEqual(events[-1]["payload"]["reason"], "duplicate_order_id")

    def test_nonfinite_and_negative_inputs_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                signal(quantity=value)
            with self.assertRaises(ValueError):
                SimulationEngine({"DEMO": Decimal(value)})
        for kwargs in ({"commission_rate": Decimal("-1")}, {"slippage_bps": Decimal("-1")},
                       {"max_total_loss": Decimal("-1")}):
            with self.assertRaises(ValueError):
                RiskConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
