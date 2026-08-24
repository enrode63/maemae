from pathlib import Path
import tempfile
import unittest

from trading_automation.scoring import TEAM_SCORE_CONFIGS, score_signals
from trading_automation.service import AutomationService


class Market:
    name = "test-market"
    def symbols(self, index):
        return ["AAPL"]
    def snapshot(self, symbols):
        return {}


class EnsembleScoreTests(unittest.TestCase):
    def test_every_team_has_normalized_weights_threshold_and_timeframe(self):
        self.assertEqual(set(TEAM_SCORE_CONFIGS), {"scalping", "day", "swing", "longterm"})
        for team, config in TEAM_SCORE_CONFIGS.items():
            with self.subTest(team=team):
                self.assertAlmostEqual(sum(config.weights.values()), 1)
                self.assertTrue(0 <= config.threshold <= 100)
                self.assertTrue(config.timeframe)
                result = score_signals(team, {name: 80 for name in config.weights})
                self.assertEqual(result["score"], 80)
                self.assertEqual(result["decision"], "PAPER_TRADE")
                self.assertEqual(result["threshold"], config.threshold)

    def test_threshold_and_missing_or_invalid_data_fail_closed(self):
        names = TEAM_SCORE_CONFIGS["day"].weights
        self.assertEqual(score_signals("day", {name: 10 for name in names})["decision"], "NO_TRADE")
        missing = score_signals("day", {"trend": 100})
        self.assertEqual((missing["decision"], missing["score"]), ("NO_TRADE", 0))
        invalid = score_signals("day", {name: float("nan") for name in names})
        self.assertEqual(invalid["decision"], "NO_TRADE")

    def test_decision_persists_contract_and_never_places_live_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AutomationService(Path(tmp), Market())
            signals = {name: 90 for name in TEAM_SCORE_CONFIGS["scalping"].weights}
            decision = service.decide({"team": "scalping", "symbol": "AAPL",
                                       "signals": signals, "data_quality": "delayed"})
            self.assertEqual(decision["decision"], "PAPER_TRADE")
            self.assertFalse(decision["live_ordering"])
            self.assertEqual(decision["mode"], "simulation")
            self.assertGreaterEqual(len(decision["rationale"]), 60)
            for field in ("score", "signal_breakdown", "threshold", "timeframe", "rationale"):
                self.assertIn(field, decision)
            self.assertEqual(service.decisions("scalping"), [decision])
            self.assertEqual(service.performance()["trade_count"], 0)

    def test_unconfigured_provider_is_no_trade_even_with_high_claimed_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AutomationService(Path(tmp))
            signals = {name: 100 for name in TEAM_SCORE_CONFIGS["longterm"].weights}
            result = service.decide({"team": "longterm", "symbol": "BTC", "signals": signals})
            self.assertEqual(result["decision"], "NO_TRADE")
            self.assertIn("market_data_provider", result["missing_signals"])
            self.assertFalse(result["live_ordering"])

    def test_reports_exposes_latest_paper_decision_per_selected_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AutomationService(Path(tmp), Market())
            day_signals = {name: 80 for name in TEAM_SCORE_CONFIGS["day"].weights}
            old_day = service.decide({"team": "day", "symbol": "AAPL", "signals": day_signals,
                                      "created_at": "2026-08-24T01:00:00+00:00"})
            latest_day = service.decide({"team": "day", "symbol": "AAPL", "signals": day_signals,
                                         "data_quality": "delayed",
                                         "created_at": "2026-08-24T02:00:00+00:00"})
            swing_signals = {name: 90 for name in TEAM_SCORE_CONFIGS["swing"].weights}
            swing = service.decide({"team": "swing", "symbol": "AAPL", "signals": swing_signals,
                                    "created_at": "2026-08-24T03:00:00+00:00"})

            report = service.reports("day")
            self.assertEqual(report["ensemble"]["updatedAt"], latest_day["created_at"])
            self.assertEqual(len(report["ensemble"]["teams"]), 1)
            item = report["ensemble"]["teams"][0]
            self.assertEqual(item["id"], latest_day["id"])
            self.assertNotEqual(item["id"], old_day["id"])
            self.assertEqual(item["status"], "delayed")
            for field in ("symbol", "team", "score", "decision", "threshold", "timeframe",
                          "signal_breakdown", "rationale", "missing_signals"):
                self.assertIn(field, item)

            overall = service.reports("all")["ensemble"]
            self.assertEqual({item["id"] for item in overall["teams"]}, {latest_day["id"], swing["id"]})
            self.assertEqual(overall["updatedAt"], swing["created_at"])

    def test_reports_keeps_ensemble_empty_when_provider_is_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AutomationService(Path(tmp))
            signals = {name: 100 for name in TEAM_SCORE_CONFIGS["longterm"].weights}
            service.decide({"team": "longterm", "symbol": "BTC", "signals": signals})
            self.assertEqual(service.reports()["ensemble"], {"updatedAt": None, "teams": []})


if __name__ == "__main__":
    unittest.main()
