from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import unittest

from app.analytics.calculations import (
    drawdown_current,
    drawdown_maximum,
    moving_average,
    period_high,
    period_low,
    percentage_change,
    volume_average,
)
from app.domain.errors import DataQualityError
from app.models.schemas import OhlcvBar, InstrumentIdentifier


def _bar(day: int, close: int = 100) -> OhlcvBar:
    return OhlcvBar(
        instrument=InstrumentIdentifier(symbol="AAPL"),
        date=date(2026, 1, 1) + timedelta(days=day),
        open=Decimal(close),
        high=Decimal(close + 2),
        low=Decimal(close - 2),
        close=Decimal(close),
        volume=1000 + day,
        provider="mock",
    )


class TestAnalytics(unittest.TestCase):
    def test_percentage_change_basic(self) -> None:
        bars = [_bar(0, 100), _bar(1, 110)]
        self.assertEqual(percentage_change(bars), Decimal("10"))

    def test_period_high_and_low_dates(self) -> None:
        bars = [_bar(0, 100), _bar(1, 90), _bar(2, 105)]
        high, high_date = period_high(bars)
        low, low_date = period_low(bars)
        self.assertEqual(high, Decimal("107"))
        self.assertEqual(high_date, date(2026, 1, 3))
        self.assertEqual(low, Decimal("88"))
        self.assertEqual(low_date, date(2026, 1, 2))

    def test_moving_average_needs_enough_data(self) -> None:
        bars = [_bar(0, 100)]
        with self.assertRaises(DataQualityError):
            moving_average(bars, 20)

    def test_drawdown_functions(self) -> None:
        bars = [_bar(0, 100), _bar(1, 90), _bar(2, 95), _bar(3, 80), _bar(4, 120)]
        dd, peak_date, trough_date = drawdown_maximum(bars)
        self.assertLessEqual(dd, 0)
        self.assertEqual(peak_date, date(2026, 1, 1))
        self.assertEqual(trough_date, date(2026, 1, 4))
        self.assertEqual(volume_average(bars), Decimal("1002"))

        current_dd, latest = drawdown_current(bars)
        self.assertLessEqual(current_dd, 0)
        self.assertEqual(latest, date(2026, 1, 5))


if __name__ == "__main__":
    unittest.main()
