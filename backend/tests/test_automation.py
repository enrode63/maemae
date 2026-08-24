from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from trading_automation.providers import UnconfiguredProvider
from trading_automation.schedule import schedule_state
from trading_automation.service import AutomationService


class FakeMarket:
    name = "fake-market"
    def symbols(self, index):
        return ["AAPL", "MSFT"] if index == "sp500" else ["AAPL", "NVDA"]
    def snapshot(self, symbols):
        return {symbol: {"close": 100} for symbol in symbols}


class FakeReporter:
    name = "fake-reporter"
    def generate(self, market, snapshots):
        return f"{market} report for {len(snapshots)} symbols"


class ScheduleTests(unittest.TestCase):
    def test_us_close_tracks_dst_and_crypto_is_kst(self):
        winter = schedule_state(datetime(2026, 1, 5, 21, 5, tzinfo=timezone.utc))
        summer = schedule_state(datetime(2026, 7, 6, 20, 5, tzinfo=timezone.utc))
        before = schedule_state(datetime(2026, 7, 6, 20, 4, tzinfo=timezone.utc))
        self.assertTrue(winter["us"]["due"])
        self.assertTrue(summer["us"]["due"])
        self.assertFalse(before["us"]["due"])
        self.assertTrue(schedule_state(datetime(2026, 7, 6, 0, 0, tzinfo=timezone.utc))["crypto"]["due"])


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_unconfigured_is_explicit_and_jobs_fail_closed(self):
        service = AutomationService(Path(self.temp.name))
        self.assertFalse(service.status()["providers"]["market_data"]["configured"])
        self.assertEqual(service.universe()["status"], "not_configured")
        result = service.run_due(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual({j["state"] for j in result["jobs"]}, {"blocked_not_configured"})
        self.assertEqual(service.reports()["kpis"]["cumulativePnl"], 0)

    def test_universe_deduplicates_and_report_is_idempotent(self):
        service = AutomationService(Path(self.temp.name), FakeMarket(), FakeReporter())
        self.assertEqual(service.universe()["us_equities"], ["AAPL", "MSFT", "NVDA"])
        now = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
        first, second = service.run_due(now), service.run_due(now)
        self.assertEqual([j["state"] for j in first["jobs"]], ["generated", "generated"])
        self.assertEqual([j["state"] for j in second["jobs"]],
                         ["not_due_or_already_completed", "not_due_or_already_completed"])

    def test_paper_trade_rationale_and_team_contract(self):
        service = AutomationService(Path(self.temp.name), FakeMarket())
        base = {"symbol": "AAPL", "team": "day", "side": "BUY", "quantity": "2", "price": "100"}
        with self.assertRaisesRegex(ValueError, "at least 60"):
            service.record_trade({**base, "rationale": "too short"})
        trade = service.record_trade({**base, "rationale": "x" * 60, "risk": "10", "reward": "25",
                                      "pnl": "5", "return_pct": "1.2"})
        self.assertEqual(trade["mode"], "simulation")
        performance = service.performance("day")
        self.assertEqual(performance["rr"], "2.5")
        self.assertEqual(performance["win_rate"], "1")
        self.assertEqual(performance["positions"], {"AAPL": "2"})
        evaluation = service.weekly_evaluation("day", "2026-W32", ["risk discipline"], ["timing"])
        self.assertEqual(evaluation["performance"]["asset_allocation"], {"AAPL": "200"})
        self.assertEqual(service.evaluations(), [evaluation])

        with self.assertRaisesRegex(ValueError, "at least 60"):
            service.record_trade({**base, "side": "SELL", "rationale": "short"})
        with self.assertRaisesRegex(ValueError, "invalid team"):
            service.record_trade({**base, "team": "all", "rationale": "x" * 60})
        with self.assertRaisesRegex(ValueError, "outside"):
            service.record_trade({**base, "symbol": "NOTREAL", "rationale": "x" * 60})

        report = service.reports("all")
        self.assertEqual(set(report), {"kpis", "teams", "assetAllocation", "weekly", "sentiment", "ensemble", "updatedAt"})
        self.assertEqual(report["kpis"]["cumulativePnl"], 5.0)

    def test_reporting_services_reject_invalid_team_and_accept_overall_scope(self):
        service = AutomationService(Path(self.temp.name))
        for call in (
            lambda: service.performance("bogus"),
            lambda: service.weekly_evaluation("bogus", "2026-W34", [], []),
        ):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, "invalid team"):
                call()
        self.assertEqual(service.performance(None)["scope"], "overall")
        self.assertEqual(service.performance("all")["scope"], "overall")
        self.assertEqual(service.weekly_evaluation(None, "2026-W34", [], [])["team"], "all")

    def test_provider_status_is_sanitized_and_provider_exception_is_contained(self):
        class UnsafeMarket(FakeMarket):
            status = {"configured": True, "name": "unsafe", "state": "ready", "api_key": "secret"}
            def snapshot(self, symbols):
                raise RuntimeError("secret provider failure")

        service = AutomationService(Path(self.temp.name), UnsafeMarket(), FakeReporter())
        self.assertNotIn("api_key", service.status()["providers"]["market_data"])
        result = service.run_due(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual({job["state"] for job in result["jobs"]}, {"provider_error"})
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
