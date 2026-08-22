from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.providers.base import ProviderUnavailable
from app.providers.live_provider import YahooFinanceProvider


class TestYahooSymbolValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = YahooFinanceProvider()

    def test_valid_symbols_are_normalized(self) -> None:
        self.assertEqual(self.provider.normalize_symbol("AAPL"), "AAPL")
        self.assertEqual(self.provider.normalize_symbol("brk.b"), "BRK.B")
        self.assertEqual(self.provider.normalize_symbol("russ-4"), "RUSS-4")

    def test_invalid_symbols_are_rejected(self) -> None:
        for value in [
            "AAPL&x=y",
            "AAPL?x",
            "AAPL#x",
            "AAPL/../../x",
            "AAPL%2FTEST",
            " AAPL",
            "AAPL ",
            "AAPL\t",
            "",
            "X" * 128,
        ]:
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    self.provider.normalize_symbol(value)


class TestLiveProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = YahooFinanceProvider()

    def test_fetch_quote_uses_live_chart_payload(self) -> None:
        payload = {
            "chart": {
                "error": None,
                "result": [{
                    "meta": {
                        "currency": "USD",
                        "regularMarketPrice": 213.45,
                        "previousClose": 210.00,
                        "regularMarketVolume": 123456,
                        "regularMarketTime": 1786737601,
                    }
                }],
            }
        }
        with patch.object(YahooFinanceProvider, "_query_json", return_value=payload) as query:
            result = self.provider.fetch_quote("AAPL")
        self.assertEqual(query.call_args.kwargs["timeout_seconds"], 2.5)
        self.assertIn("/v8/finance/chart/AAPL", query.call_args.args[0])
        self.assertIn("interval=1m", query.call_args.args[0])
        self.assertEqual(result.payload.last, Decimal("213.45"))
        self.assertEqual(result.payload.previous_close, Decimal("210.0"))
        self.assertEqual(result.payload.volume, 123456)
        self.assertEqual(result.payload.provider_timestamp, datetime.fromtimestamp(1786737601, tz=timezone.utc))

    def test_fetch_historical_builds_bars_from_live_chart_payload(self) -> None:
        payload = {
            "chart": {
                "error": None,
                "result": [{
                    "meta": {"currency": "USD"},
                    "timestamp": [1786665600, 1786752000],
                    "indicators": {"quote": [{
                        "open": [210.0, 212.0],
                        "high": [214.0, 216.0],
                        "low": [209.0, 211.0],
                        "close": [213.0, 215.0],
                        "volume": [1000, 1200],
                    }]},
                }],
            }
        }
        with patch.object(YahooFinanceProvider, "_query_json", return_value=payload) as query:
            result = self.provider.fetch_historical(self.provider.resolve_instrument("AAPL").identifier)
        self.assertIn("/v8/finance/chart/AAPL", query.call_args.args[0])
        bars, actions = result.payload
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1].close, Decimal("215.0"))
        self.assertEqual(bars[-1].provider, "yahoo")
        self.assertEqual(actions, [])

    def test_upstream_error_is_exposed_without_mock_fallback(self) -> None:
        payload = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}
        with patch.object(YahooFinanceProvider, "_query_json", return_value=payload):
            with self.assertRaisesRegex(ProviderUnavailable, "No data found"):
                self.provider.fetch_quote("ZZZZ")

    def test_fetch_actions_is_explicitly_unsupported(self) -> None:
        with self.assertRaises(ProviderUnavailable):
            _ = self.provider.fetch_actions(self.provider.resolve_instrument("AAPL"))


@pytest.mark.parametrize(
    "bad_symbol",
    [
        " AAPL",
        "AAPL?",
        "AAPL#x",
        "AAPL&x",
        "aapl",
        "AAPL/../../x",
        "AAPL%2FTEST",
        "AAPL\\x",
    ],
)
def test_symbol_validation_rejects_adversarial_inputs(bad_symbol: str) -> None:
    provider = YahooFinanceProvider()
    with pytest.raises(Exception):
        provider.fetch_quote(bad_symbol)
