from datetime import datetime, timezone
import json
import unittest

from trading_automation.providers import (
    FreeEodMarketDataProvider, ProviderUnavailable, UnconfiguredProvider,
    market_provider_from_env,
)


def index_html(count, header="Symbol"):
    rows = "".join(f"<tr><td>SYM{i}</td><td>Company {i}</td></tr>" for i in range(count))
    return f"<table><tr><th>{header}</th><th>Company</th></tr>{rows}</table>".encode()


def chart_payload(last_timestamp=None):
    end = last_timestamp or int(datetime.now(timezone.utc).timestamp())
    timestamps = [end - (29 - i) * 86400 for i in range(30)]
    payload = {"chart": {"result": [{"timestamp": timestamps, "indicators": {"quote": [{
        "close": [100 + i for i in range(30)], "volume": [1000 + i for i in range(30)]
    }]}}]}}
    return json.dumps(payload).encode()


class FreeMarketProviderTests(unittest.TestCase):
    def test_opt_in_configuration_and_default_fail_closed(self):
        self.assertIsInstance(market_provider_from_env({}), UnconfiguredProvider)
        self.assertIsInstance(
            market_provider_from_env({"MARKET_DATA_PROVIDER": "free_eod"}),
            FreeEodMarketDataProvider,
        )
        self.assertIsInstance(
            market_provider_from_env({"MARKET_DATA_PROVIDER": "unknown"}),
            UnconfiguredProvider,
        )

    def test_universe_and_eod_snapshot_contract(self):
        calls = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            if "S%26P" in url:
                return index_html(500)
            if "Nasdaq-100" in url:
                return index_html(100, "Ticker")
            return chart_payload()

        provider = FreeEodMarketDataProvider(fetch=fetch)
        self.assertEqual(len(provider.symbols("sp500")), 500)
        self.assertEqual(len(provider.symbols("nasdaq100")), 100)
        snapshots = provider.snapshot(["SYM0", "BTC", "ETH"])
        self.assertEqual(set(snapshots), {"SYM0", "BTC", "ETH"})
        self.assertEqual({item["data_quality"] for item in snapshots.values()}, {"free"})
        self.assertTrue(all(item["price"] > 0 and item["volume"] >= 0 for item in snapshots.values()))
        self.assertEqual(set(snapshots["BTC"]["signals"]), {"scalping", "day", "swing", "longterm"})
        self.assertTrue(any("BTC-USD" in url for url, _ in calls))

    def test_incomplete_universe_stale_and_malformed_data_fail_closed(self):
        provider = FreeEodMarketDataProvider(fetch=lambda url, timeout: index_html(10))
        with self.assertRaises(ProviderUnavailable):
            provider.symbols("sp500")

        stale = int(datetime.now(timezone.utc).timestamp()) - 10 * 86400
        provider = FreeEodMarketDataProvider(fetch=lambda url, timeout: chart_payload(stale))
        with self.assertRaisesRegex(ProviderUnavailable, "stale"):
            provider.snapshot(["BTC"])

        provider = FreeEodMarketDataProvider(fetch=lambda url, timeout: b"not-json")
        with self.assertRaisesRegex(ProviderUnavailable, "unavailable"):
            provider.snapshot(["ETH"])

    def test_nonfinite_chart_values_fail_closed(self):
        for field, value in (("close", float("nan")), ("volume", float("inf")),
                             ("timestamp", float("-inf"))):
            payload = json.loads(chart_payload())
            chart = payload["chart"]["result"][0]
            target = chart["timestamp"] if field == "timestamp" else chart["indicators"]["quote"][0][field]
            target[-1] = value
            provider = FreeEodMarketDataProvider(
                fetch=lambda url, timeout, body=json.dumps(payload).encode(): body)
            with self.subTest(field=field), self.assertRaisesRegex(ProviderUnavailable, "non-finite"):
                provider.snapshot(["BTC"])


if __name__ == "__main__":
    unittest.main()
