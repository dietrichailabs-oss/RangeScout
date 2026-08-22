from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.analytics.trading_indicators import calculate_indicators, calculate_risk
from app.domain.errors import DataQualityError
from app.streaming.candle_aggregator import LiveCandle


def candle(index: int, close: int, volume: int = 100) -> LiveCandle:
    start = datetime(2026, 8, 17, 13, 30, tzinfo=timezone.utc) + timedelta(minutes=index)
    value = Decimal(close)
    return LiveCandle("AAPL", 60, start, start + timedelta(minutes=1), value - 1, value + 2, value - 2, value, Decimal(volume), 2, start, start)


def test_required_indicators_are_deterministic() -> None:
    candles = [candle(index, 100 + index, 100 if index < 20 else 300) for index in range(21)]
    result = calculate_indicators(candles, Decimal("99"))
    assert result.ema9 > result.ema20
    assert result.rsi == Decimal("100")
    assert result.macd > 0
    assert result.atr > 0
    assert result.rvol == Decimal("3")
    assert result.volume_spike is True
    assert result.gap_percent == 0
    assert result.day_high == Decimal("122")
    assert result.day_low == Decimal("98")
    assert result.opening_range_1m == (Decimal("102"), Decimal("98"))
    assert result.opening_range_5m == (Decimal("106"), Decimal("98"))
    assert result.opening_range_15m == (Decimal("116"), Decimal("98"))
    assert result.distance_from_day_high == Decimal("-2")
    assert result.distance_from_day_low == Decimal("22")


def test_risk_calculator_rounds_down_and_reports_actual_risk() -> None:
    result = calculate_risk(Decimal("10.25"), Decimal("9.80"), Decimal("100"))
    assert result.share_count == 222
    assert result.distance_to_stop == Decimal("0.45")
    assert result.actual_risk == Decimal("99.90")


@pytest.mark.parametrize("entry,stop,risk", [("0", "9", "100"), ("10", "10", "100"), ("10", "9", "0")])
def test_risk_calculator_rejects_unsafe_inputs(entry: str, stop: str, risk: str) -> None:
    with pytest.raises(DataQualityError):
        calculate_risk(Decimal(entry), Decimal(stop), Decimal(risk))
