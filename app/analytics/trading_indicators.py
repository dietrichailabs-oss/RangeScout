"""Pure local intraday indicators and position-risk calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal, ROUND_FLOOR
from typing import Sequence

from app.domain.errors import DataQualityError
from app.market_calendar.us_equities import NEW_YORK
from app.streaming.candle_aggregator import LiveCandle


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    vwap: Decimal
    ema9: Decimal
    ema20: Decimal
    rsi: Decimal
    macd: Decimal
    macd_signal: Decimal
    atr: Decimal
    rvol: Decimal
    volume_spike: bool
    gap_percent: Decimal
    day_high: Decimal
    day_low: Decimal
    premarket_high: Decimal | None
    premarket_low: Decimal | None
    opening_range_1m: tuple[Decimal, Decimal] | None
    opening_range_5m: tuple[Decimal, Decimal] | None
    opening_range_15m: tuple[Decimal, Decimal] | None
    distance_from_vwap: Decimal
    distance_from_day_high: Decimal
    distance_from_day_low: Decimal


@dataclass(frozen=True, slots=True)
class RiskResult:
    share_count: int
    actual_risk: Decimal
    distance_to_stop: Decimal


def calculate_indicators(candles: Sequence[LiveCandle], previous_close: Decimal) -> IndicatorSnapshot:
    if not candles:
        raise DataQualityError("At least one candle is required.")
    closes = [item.close for item in candles]
    typical_volume = [(item.high + item.low + item.close) / Decimal(3) * item.volume for item in candles]
    total_volume = sum((item.volume for item in candles), Decimal(0))
    if total_volume <= 0:
        raise DataQualityError("Positive volume is required for VWAP.")
    vwap = sum(typical_volume, Decimal(0)) / total_volume
    ema9_values = _ema(closes, 9)
    ema20_values = _ema(closes, 20)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_series = [left - right for left, right in zip(ema12, ema26)]
    volumes = [item.volume for item in candles]
    average_prior_volume = sum(volumes[:-1], Decimal(0)) / Decimal(max(1, len(volumes) - 1)) if len(volumes) > 1 else volumes[-1]
    rvol = volumes[-1] / average_prior_volume if average_prior_volume else Decimal(0)
    day_high = max(item.high for item in candles)
    day_low = min(item.low for item in candles)
    premarket = [item for item in candles if time(4, 0) <= item.started_at.astimezone(NEW_YORK).time() < time(9, 30)]
    latest = candles[-1].close
    return IndicatorSnapshot(
        vwap=vwap,
        ema9=ema9_values[-1],
        ema20=ema20_values[-1],
        rsi=_rsi(closes, 14),
        macd=macd_series[-1],
        macd_signal=_ema(macd_series, 9)[-1],
        atr=_atr(candles, 14),
        rvol=rvol,
        volume_spike=rvol >= Decimal("2"),
        gap_percent=((candles[0].open - previous_close) / previous_close * Decimal(100)) if previous_close else Decimal(0),
        day_high=day_high,
        day_low=day_low,
        premarket_high=max((item.high for item in premarket), default=None),
        premarket_low=min((item.low for item in premarket), default=None),
        opening_range_1m=_opening_range(candles, 60),
        opening_range_5m=_opening_range(candles, 300),
        opening_range_15m=_opening_range(candles, 900),
        distance_from_vwap=latest - vwap,
        distance_from_day_high=latest - day_high,
        distance_from_day_low=latest - day_low,
    )


def calculate_risk(entry: Decimal, stop: Decimal, max_dollar_risk: Decimal) -> RiskResult:
    distance = abs(entry - stop)
    if entry <= 0 or stop <= 0 or max_dollar_risk <= 0 or distance == 0:
        raise DataQualityError("Entry, stop, and max risk must be positive and entry must differ from stop.")
    shares = int((max_dollar_risk / distance).to_integral_value(rounding=ROUND_FLOOR))
    return RiskResult(shares, distance * shares, distance)


def _ema(values: Sequence[Decimal], period: int) -> list[Decimal]:
    if not values:
        raise DataQualityError("EMA requires values.")
    alpha = Decimal(2) / Decimal(period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append((value * alpha) + (output[-1] * (Decimal(1) - alpha)))
    return output


def _rsi(values: Sequence[Decimal], period: int) -> Decimal:
    if len(values) < 2:
        return Decimal(50)
    changes = [values[index] - values[index - 1] for index in range(1, len(values))][-period:]
    gains = sum((max(change, Decimal(0)) for change in changes), Decimal(0)) / Decimal(len(changes))
    losses = sum((abs(min(change, Decimal(0))) for change in changes), Decimal(0)) / Decimal(len(changes))
    if losses == 0:
        return Decimal(100) if gains > 0 else Decimal(50)
    return Decimal(100) - (Decimal(100) / (Decimal(1) + gains / losses))


def _atr(candles: Sequence[LiveCandle], period: int) -> Decimal:
    ranges: list[Decimal] = []
    previous = candles[0].close
    for candle in candles:
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous)))
        previous = candle.close
    selected = ranges[-period:]
    return sum(selected, Decimal(0)) / Decimal(len(selected))


def _opening_range(candles: Sequence[LiveCandle], seconds: int) -> tuple[Decimal, Decimal] | None:
    regular = [item for item in candles if item.started_at.astimezone(NEW_YORK).time() >= time(9, 30)]
    if not regular:
        return None
    start = regular[0].started_at
    selected = [item for item in regular if (item.started_at - start).total_seconds() < seconds]
    return max(item.high for item in selected), min(item.low for item in selected)
