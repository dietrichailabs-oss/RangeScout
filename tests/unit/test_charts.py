from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.charts import prepare_chart_payload
from app.models.schemas import InstrumentIdentifier, OhlcvBar


class TestCharts(unittest.TestCase):
    def test_prepare_payload_contains_markers(self) -> None:
        bars = [
            OhlcvBar(
                instrument=InstrumentIdentifier(symbol="AAPL"),
                date=date(2026, 1, 1),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("99"),
                close=Decimal("101"),
                volume=1000,
                provider="mock",
            ),
            OhlcvBar(
                instrument=InstrumentIdentifier(symbol="AAPL"),
                date=date(2026, 1, 2),
                open=Decimal("101"),
                high=Decimal("104"),
                low=Decimal("100"),
                close=Decimal("103"),
                volume=1100,
                provider="mock",
            ),
        ]
        payload = prepare_chart_payload(bars)
        self.assertEqual(payload.markers["period_high"]["value"], 104.0)
        self.assertEqual(payload.markers["period_low"]["value"], 99.0)


if __name__ == "__main__":
    unittest.main()
