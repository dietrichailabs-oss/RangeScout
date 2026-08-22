from __future__ import annotations

import unittest
from decimal import Decimal
from datetime import date

from app.alerts import AlertRule, evaluate_alerts
from app.models.schemas import InstrumentIdentifier, OhlcvBar


class TestAlerts(unittest.TestCase):
    def test_percent_change_alert_triggers(self) -> None:
        bars = [
            OhlcvBar(
                instrument=InstrumentIdentifier(symbol="AAPL"),
                date=date(2026, 1, 1),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("150"),
                volume=100,
                provider="mock",
            )
        ]
        rules = [AlertRule(id="a1", symbol="AAPL", mode="percent_change", threshold=Decimal("10"))]
        events = evaluate_alerts(rules, {"AAPL": bars})
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
