"""Deterministic analytical calculations for OHLCV histories."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Literal

from app.domain.errors import DataQualityError
from app.models.schemas import OhlcvBar


@dataclass(frozen=True)
class MetricPoint:
    label: str
    value: float | Decimal
    unit: str | None = None


def price_change_abs(bars: Sequence[OhlcvBar]) -> Decimal:
    if not bars:
        raise DataQualityError("No bars to calculate absolute change.")
    return bars[-1].close - bars[0].open


def percentage_change(bars: Sequence[OhlcvBar]) -> Decimal:
    if not bars:
        raise DataQualityError("No bars to calculate percentage change.")
    first = bars[0].open
    if first == 0:
        raise DataQualityError("Cannot compute percentage change from zero.")
    return ((bars[-1].close - first) / first) * Decimal("100")


def period_high(bars: Sequence[OhlcvBar]) -> tuple[Decimal, date]:
    if not bars:
        raise DataQualityError("No bars for period high.")
    max_bar = max(bars, key=lambda b: b.high)
    return max_bar.high, max_bar.date


def period_low(bars: Sequence[OhlcvBar]) -> tuple[Decimal, date]:
    if not bars:
        raise DataQualityError("No bars for period low.")
    min_bar = min(bars, key=lambda b: b.low)
    return min_bar.low, min_bar.date


def cumulative_range_position(bars: Sequence[OhlcvBar]) -> Decimal:
    if len(bars) < 2:
        raise DataQualityError("Need at least two bars for range position.")
    high, _ = period_high(bars)
    low, _ = period_low(bars)
    latest = bars[-1].close
    if high == low:
        return Decimal("100")
    return ((latest - low) / (high - low)) * Decimal("100")


def moving_average(bars: Sequence[OhlcvBar], window: int) -> list[tuple[date, Decimal]]:
    if window <= 0:
        raise DataQualityError("Window must be positive.")
    if len(bars) < window:
        raise DataQualityError("Insufficient data for requested moving average.")
    values: list[tuple[date, Decimal]] = []
    closes = [b.close for b in bars]
    for i in range(window - 1, len(closes)):
        window_slice = closes[i - window + 1 : i + 1]
        values.append((bars[i].date, sum(window_slice) / Decimal(window)))
    return values


def volatility(bars: Sequence[OhlcvBar], window: int = 20) -> Decimal:
    if len(bars) < 2 or window < 2:
        raise DataQualityError("Insufficient bars for volatility.")
    closes = [float(b.close) for b in bars[-window:]]
    returns = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev == 0:
            continue
        returns.append((closes[i] - prev) / prev)
    if not returns:
        raise DataQualityError("No valid return samples for volatility.")
    mean = sum(returns) / len(returns)
    variance = sum((x - mean) ** 2 for x in returns) / len(returns)
    return Decimal(variance ** 0.5 * (252**0.5) * 100)


def volume_average(bars: Sequence[OhlcvBar]) -> Decimal:
    if not bars:
        raise DataQualityError("No bars for average volume.")
    return Decimal(sum(b.volume for b in bars) / Decimal(len(bars)))


def volume_median(bars: Sequence[OhlcvBar]) -> Decimal:
    if not bars:
        raise DataQualityError("No bars for median volume.")
    vols = sorted(b.volume for b in bars)
    return Decimal(median(vols))


def drawdown_maximum(bars: Sequence[OhlcvBar]) -> tuple[Decimal, date, date]:
    if not bars:
        raise DataQualityError("No bars for drawdown.")
    max_drawdown = Decimal("0")
    peak_date: date | None = None
    trough_date: date | None = None
    peak = bars[0].close
    peak_index = 0
    for i, bar in enumerate(bars):
        if bar.close > peak:
            peak = bar.close
            peak_index = i
        dd = (bar.close - peak) / peak if peak != 0 else Decimal("0")
        if dd < max_drawdown:
            max_drawdown = dd
            peak_date = bars[peak_index].date
            trough_date = bar.date
    if peak_date is None:
        peak_date = bars[0].date
        trough_date = bars[0].date
    return max_drawdown * Decimal("100"), peak_date, trough_date


def drawdown_current(bars: Sequence[OhlcvBar]) -> tuple[Decimal, date | None]:
    if not bars:
        raise DataQualityError("No bars for current drawdown.")
    latest = bars[-1].close
    peak = max(b.close for b in bars)
    if peak == 0:
        raise DataQualityError("Cannot compute drawdown from zero peak.")
    return ((latest - peak) / peak) * Decimal("100"), bars[-1].date


def relative_volume(bars: Sequence[OhlcvBar]) -> Decimal:
    if len(bars) < 2:
        raise DataQualityError("Need at least two bars for relative volume.")
    recent = bars[-1].volume
    average = sum(b.volume for b in bars[:-1]) / Decimal(len(bars) - 1)
    if average == 0:
        raise DataQualityError("Zero average volume.")
    return Decimal(recent) / average
