from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.streaming.candle_aggregator import CandleAggregator, SUPPORTED_INTERVALS_SECONDS
from app.streaming.events import TradeEvent


BASE = datetime(2026, 8, 17, 14, 30, 0, 100000, tzinfo=timezone.utc)


def trade(offset_ms: int, price: str, size: str = "1", event_id: str | None = None) -> TradeEvent:
    return TradeEvent("mock", "AAPL", Decimal(price), Decimal(size), BASE + timedelta(milliseconds=offset_ms), event_id)


@pytest.mark.parametrize("interval", SUPPORTED_INTERVALS_SECONDS)
def test_every_required_interval_accepts_first_trade(interval: int) -> None:
    candle = CandleAggregator(interval).process(trade(0, "10", "2")).current
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == tuple(map(Decimal, ("10", "10", "10", "10", "2")))


def test_multiple_trades_expand_high_low_close_and_volume() -> None:
    engine = CandleAggregator(5)
    engine.process(trade(0, "10", "2", "1"))
    engine.process(trade(100, "12", "3", "2"))
    candle = engine.process(trade(200, "9", "4", "3")).current
    assert candle.open == Decimal("10")
    assert candle.high == Decimal("12")
    assert candle.low == Decimal("9")
    assert candle.close == Decimal("9")
    assert candle.volume == Decimal("9")
    assert candle.trade_count == 3


def test_bucket_rollover_completes_previous_candle() -> None:
    engine = CandleAggregator(1)
    engine.process(trade(0, "10"))
    update = engine.process(trade(1000, "11"))
    assert update.completed is not None and update.completed.complete is True
    assert update.current.open == Decimal("11")


def test_out_of_order_inside_bucket_fixes_open_but_not_latest_close() -> None:
    engine = CandleAggregator(5)
    engine.process(trade(300, "11", event_id="later"))
    candle = engine.process(trade(100, "10", event_id="earlier")).current
    assert candle.open == Decimal("10")
    assert candle.close == Decimal("11")


def test_out_of_order_closed_bucket_is_rejected() -> None:
    engine = CandleAggregator(1)
    engine.process(trade(1000, "11", event_id="new"))
    update = engine.process(trade(0, "10", event_id="old"))
    assert update.accepted is False
    assert update.reason == "out_of_order_closed_bucket"


def test_duplicate_is_ignored_across_reconnect_boundary() -> None:
    engine = CandleAggregator(5)
    engine.process(trade(0, "10", "2", "provider-id-1"))
    duplicate = engine.process(trade(0, "10", "2", "provider-id-1"))
    assert duplicate.accepted is False
    assert duplicate.current.volume == Decimal("2")


def test_interval_switch_resets_forming_candle() -> None:
    engine = CandleAggregator(1)
    engine.process(trade(0, "10"))
    engine.set_interval(60)
    assert engine.current("AAPL") is None
    assert engine.process(trade(0, "12")).current.interval_seconds == 60
