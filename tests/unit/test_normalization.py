from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.domain.errors import DataQualityError
from app.models.schemas import InstrumentIdentifier, OhlcvBar
from app.normalization.normalize import normalize_histories


class TestNormalization(unittest.TestCase):
    def test_removes_duplicates_and_invalid_bars(self) -> None:
        base = OhlcvBar(
            instrument=InstrumentIdentifier(symbol="AAPL"),
            date=date(2026, 1, 1),
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("103"),
            volume=1000,
            provider="mock",
        )
        dup = base
        invalid_volume = OhlcvBar(
            instrument=base.instrument,
            date=date(2026, 1, 2),
            open=Decimal("103"),
            high=Decimal("106"),
            low=Decimal("102"),
            close=Decimal("104"),
            volume=-1,
            provider="mock",
        )
        bars, result = normalize_histories([base, dup, invalid_volume])
        self.assertEqual(len(bars), 1)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.removed, 1)

    def test_negative_price_rejected(self) -> None:
        bad = OhlcvBar(
            instrument=InstrumentIdentifier(symbol="AAPL"),
            date=date(2026, 1, 1),
            open=Decimal("0"),
            high=Decimal("0"),
            low=Decimal("0"),
            close=Decimal("0"),
            volume=1,
            provider="mock",
        )
        bars, result = normalize_histories([bad])
        self.assertEqual(len(bars), 0)
        self.assertEqual(result.removed, 1)


if __name__ == "__main__":
    unittest.main()
