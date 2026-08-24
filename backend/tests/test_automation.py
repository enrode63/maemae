from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from trading_automation.providers import UnconfiguredProvider
from trading_automation.schedule import schedule_state
from trading_automation.service import AutomationService
from trading_automation.scoring import TEAM_SCORE_CONFIGS


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


class ScoredMarket(FakeMarket):
    def snapshot(self, symbols):
        result = {}
        for index, symbol in enumerate(symbols):
            result[symbol] = {"close": 100 + index, "side": "BUY", "data_quality": "realtime",
                              "signals": {team: {name: 90 - index for name in config.weights}
                                          for team, config in TEAM_SCORE_CONFIGS.items()}}
        return result


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

    def test_scheduled_ensemble_flows_to_linked_paper_ledger_and_report(self):
        service = AutomationService(Path(self.temp.name), ScoredMarket(), FakeReporter())
        now = datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc)
        first = service.run_due(now)
        self.assertEqual(sum(job.get("trades", 0) for job in first["jobs"]), 8)
        trades = service.trades()
        decisions = service.decisions()
        self.assertEqual(len(trades), 8)
        self.assertEqual(len(decisions), 8)
        self.assertEqual({trade["symbol"] for trade in trades}, {"AAPL", "BTC"})
        by_id = {decision["id"]: decision for decision in decisions}
        for trade in trades:
            decision = by_id[trade["decision_id"]]
            self.assertEqual(trade["score"], decision["score"])
            self.assertEqual(trade["signal_breakdown"], decision["signal_breakdown"])
            self.assertGreaterEqual(len(trade["rationale"]), 60)
            self.assertFalse(trade["live_ordering"])
        report = service.reports()
        self.assertEqual(report["trades"], trades)
        self.assertEqual(report["decisions"], decisions)
        self.assertEqual(report["performance"]["trade_count"], 8)
        service.run_due(now)
        self.assertEqual(len(service.trades()), 8)

    def test_delayed_snapshot_records_no_trade_decisions(self):
        market = ScoredMarket()
        original = market.snapshot
        market.snapshot = lambda symbols: {
            symbol: {**value, "data_quality": "delayed"}
            for symbol, value in original(symbols).items()
        }
        service = AutomationService(Path(self.temp.name), market, FakeReporter())
        service.run_due(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(service.trades(), [])
        self.assertTrue(service.decisions())
        self.assertEqual({item["decision"] for item in service.decisions()}, {"NO_TRADE"})

    def test_direct_free_data_decision_fails_closed_even_with_high_signals(self):
        service = AutomationService(Path(self.temp.name), FakeMarket())
        signals = {name: 100 for name in TEAM_SCORE_CONFIGS["day"].weights}
        decision = service.decide({"symbol": "AAPL", "team": "day", "signals": signals,
                                   "data_quality": "free"})
        self.assertEqual(decision["decision"], "NO_TRADE")
        self.assertIn("realtime_data", decision["missing_signals"])
        report_team = service.reports("day")["ensemble"]["teams"][0]
        self.assertEqual(report_team["status"], "free")

    def test_market_snapshot_still_drives_ledger_without_report_provider(self):
        service = AutomationService(Path(self.temp.name), ScoredMarket())
        result = service.run_due(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(sum(job.get("trades", 0) for job in result["jobs"]), 8)
        self.assertEqual(len(service.trades()), 8)

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
        self.assertEqual(set(report), {"kpis", "teams", "assetAllocation", "weekly", "sentiment", "ensemble",
                                       "trades", "decisions", "performance", "updatedAt"})
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

    def test_configured_but_unavailable_provider_is_distinct_and_fail_closed(self):
        class UnavailableMarket(FakeMarket):
            def snapshot(self, symbols):
                from trading_automation.providers import ProviderUnavailable
                raise ProviderUnavailable("free feed is stale")

        service = AutomationService(Path(self.temp.name), UnavailableMarket(), FakeReporter())
        result = service.run_due(datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual({job["state"] for job in result["jobs"]}, {"provider_unavailable"})
        self.assertEqual({job["decision"] for job in result["jobs"]}, {"NO_TRADE"})
        self.assertEqual(service.trades(), [])


if __name__ == "__main__":
    unittest.main()
