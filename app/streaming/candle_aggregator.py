"""Deterministic tick-driven OHLCV candle aggregation."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.streaming.events import TradeEvent


SUPPORTED_INTERVALS_SECONDS = (1, 5, 15, 30, 60, 300)


@dataclass(frozen=True, slots=True)
class LiveCandle:
    symbol: str
    interval_seconds: int
    started_at: datetime
    ended_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int
    first_trade_at: datetime
    last_trade_at: datetime
    complete: bool = False


@dataclass(frozen=True, slots=True)
class CandleUpdate:
    current: LiveCandle
    completed: LiveCandle | None = None
    accepted: bool = True
    reason: str | None = None


class CandleAggregator:
    def __init__(self, interval_seconds: int = 1, *, duplicate_window: int = 10000) -> None:
        if interval_seconds not in SUPPORTED_INTERVALS_SECONDS:
            raise ValueError("Unsupported candle interval.")
        self.interval_seconds = interval_seconds
        self._current: dict[str, LiveCandle] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._duplicate_window = max(100, duplicate_window)

    def set_interval(self, interval_seconds: int) -> None:
        if interval_seconds not in SUPPORTED_INTERVALS_SECONDS:
            raise ValueError("Unsupported candle interval.")
        if interval_seconds != self.interval_seconds:
            self.interval_seconds = interval_seconds
            self._current.clear()
            self._seen.clear()

    def current(self, symbol: str) -> LiveCandle | None:
        return self._current.get(symbol.strip().upper())

    def process(self, trade: TradeEvent) -> CandleUpdate:
        identity = self._identity(trade)
        existing = self._current.get(trade.symbol)
        if identity in self._seen:
            if existing is None:
                raise RuntimeError("Duplicate state is inconsistent.")
            return CandleUpdate(existing, accepted=False, reason="duplicate")
        self._remember(identity)
        bucket = _bucket_start(trade.timestamp, self.interval_seconds)
        if existing is None:
            created = _new_candle(trade, bucket, self.interval_seconds)
            self._current[trade.symbol] = created
            return CandleUpdate(created)
        if bucket < existing.started_at:
            return CandleUpdate(existing, accepted=False, reason="out_of_order_closed_bucket")
        if bucket > existing.started_at:
            completed = replace(existing, complete=True)
            created = _new_candle(trade, bucket, self.interval_seconds)
            self._current[trade.symbol] = created
            return CandleUpdate(created, completed=completed)
        updated = replace(
            existing,
            open=trade.price if trade.timestamp < existing.first_trade_at else existing.open,
            high=max(existing.high, trade.price),
            low=min(existing.low, trade.price),
            close=trade.price if trade.timestamp >= existing.last_trade_at else existing.close,
            volume=existing.volume + trade.size,
            trade_count=existing.trade_count + 1,
            first_trade_at=min(existing.first_trade_at, trade.timestamp),
            last_trade_at=max(existing.last_trade_at, trade.timestamp),
        )
        self._current[trade.symbol] = updated
        return CandleUpdate(updated)

    def _identity(self, trade: TradeEvent) -> str:
        if trade.event_id:
            return f"{trade.provider}|{trade.symbol}|id:{trade.event_id}"
        return f"{trade.provider}|{trade.symbol}|{trade.timestamp.isoformat()}|{trade.price}|{trade.size}"

    def _remember(self, identity: str) -> None:
        self._seen[identity] = None
        while len(self._seen) > self._duplicate_window:
            self._seen.popitem(last=False)


def _bucket_start(value: datetime, interval_seconds: int) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    utc = value.astimezone(timezone.utc)
    epoch_seconds = int(utc.timestamp())
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % interval_seconds, tz=timezone.utc)


def _new_candle(trade: TradeEvent, bucket: datetime, interval: int) -> LiveCandle:
    return LiveCandle(
        symbol=trade.symbol,
        interval_seconds=interval,
        started_at=bucket,
        ended_at=bucket + timedelta(seconds=interval),
        open=trade.price,
        high=trade.price,
        low=trade.price,
        close=trade.price,
        volume=trade.size,
        trade_count=1,
        first_trade_at=trade.timestamp,
        last_trade_at=trade.timestamp,
    )
