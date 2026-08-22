import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from app.models.schemas import InstrumentIdentifier

from tests.fakes.mock_provider import MockMarketDataProvider

class TestMockProviderContract(unittest.TestCase):
    def test_mock_provider_contract(self) -> None:
        p = MockMarketDataProvider()
        inst = p.resolve_instrument("AAPL")
        quote_result = p.fetch_quote(inst.identifier.symbol)
        hist_result = p.fetch_historical(inst.identifier)

        self.assertEqual(quote_result.kind, "quote")
        self.assertEqual(hist_result.kind, "historical")
        self.assertIsInstance(hist_result.payload, tuple)
        bars, actions = hist_result.payload
        self.assertGreater(len(bars), 0)
        self.assertIsInstance(actions, list)

    def test_mock_provider_downtrend_invariants_long_ranges(self) -> None:
        p = MockMarketDataProvider()
        inst = p.resolve_instrument("AAPLDN")
        start = datetime.now(timezone.utc) - timedelta(days=3650 - 1)
        end = datetime.now(timezone.utc)
        bars, _ = p.fetch_historical(inst.identifier, start=start, end=end).payload
        self.assertEqual(len(bars), 3650)
        for bar in bars:
            self.assertGreater(bar.open, Decimal("0"))
            self.assertGreater(bar.high, Decimal("0"))
            self.assertGreater(bar.low, Decimal("0"))
            self.assertGreater(bar.close, Decimal("0"))
            self.assertLessEqual(bar.low, min(bar.open, bar.close))
            self.assertGreaterEqual(bar.high, max(bar.open, bar.close))

    def test_mock_provider_downtrend_deterministic(self) -> None:
        p = MockMarketDataProvider()
        symbol = p.resolve_instrument("AAPLDN")
        start = datetime.now(timezone.utc) - timedelta(days=30 - 1)
        end = datetime.now(timezone.utc)
        first, _ = p.fetch_historical(symbol.identifier, start=start, end=end).payload
        second, _ = p.fetch_historical(symbol.identifier, start=start, end=end).payload
        self.assertEqual([(bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in first], [
            (bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in second
        ])

    def test_mock_provider_range_cases(self) -> None:
        p = MockMarketDataProvider()
        start_30 = datetime.now(timezone.utc) - timedelta(days=30 - 1)
        start_3650 = datetime.now(timezone.utc) - timedelta(days=3650 - 1)
        end = datetime.now(timezone.utc)
        for symbol in [
            "AAPL",
            "AAPLDN",
            "ZZZDN",
            "ABCUP",
            "AAPLFLAT",
            "AAPPLV",
            "AAPPTTL",
            "AAPLHV",
        ]:
            inst = p.resolve_instrument(symbol)
            rows_30, _ = p.fetch_historical(inst.identifier, start=start_30, end=end).payload
            rows_3650, _ = p.fetch_historical(inst.identifier, start=start_3650, end=end).payload
            self.assertEqual(len(rows_30), 30)
            self.assertGreaterEqual(len(rows_3650), 3650 if symbol.endswith(("DN", "UP", "FLAT", "LV", "HV")) else 365)
            for bar in rows_30:
                self.assertGreater(bar.low, Decimal("0"))
                self.assertLessEqual(bar.low, min(bar.open, bar.close))
                self.assertGreaterEqual(bar.high, max(bar.open, bar.close))


if __name__ == "__main__":
    unittest.main()
