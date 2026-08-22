from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.models.schemas import InstrumentIdentifier, OhlcvBar
from app.research.models import Availability
from app.research.performance import calculate_price_performance


def _bar(day: date, close: str) -> OhlcvBar:
    value = Decimal(close)
    return OhlcvBar(InstrumentIdentifier("ACME"), day, value, value, value, value, 100, "yahoo")


def test_price_performance_is_deterministic_and_marks_missing_long_periods() -> None:
    end = date(2026, 8, 17)
    bars = [_bar(end - timedelta(days=120), "100"), _bar(end - timedelta(days=30), "80"), _bar(end, "120")]
    result = calculate_price_performance(bars, as_of=end)
    assert result["1M"].value == Decimal(50)
    assert result["Maximum drawdown"].value == Decimal(-20)
    assert result["1Y"].availability is Availability.NOT_AVAILABLE
    assert result["Annualized volatility"].availability is Availability.AVAILABLE
    assert result["Benchmark-relative performance"].availability is Availability.NOT_AVAILABLE
