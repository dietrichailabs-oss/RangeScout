from __future__ import annotations

from datetime import date
from decimal import Decimal
import unittest

from app.comparisons import compare_symbols
from app.models.schemas import InstrumentIdentifier, OhlcvBar


class TestComparisons(unittest.TestCase):
    def test_compare_symbols(self) -> None:
        sym = [
            OhlcvBar(
                instrument=InstrumentIdentifier(symbol="AAPL"),
                date=date(2026, 1, 1),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("110"),
                volume=100,
                provider="mock",
            )
        ]
        bench = [
            OhlcvBar(
                instrument=InstrumentIdentifier(symbol="SPY"),
                date=date(2026, 1, 1),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("105"),
                volume=100,
                provider="mock",
            )
        ]
        result = compare_symbols(sym, bench, "AAPL", "SPY")
        self.assertEqual(result.symbol, "AAPL")
        self.assertGreater(result.relative_outperformance_pct, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
