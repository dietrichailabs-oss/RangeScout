"""Chart data preparation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.models.schemas import OhlcvBar
from app.analytics.calculations import period_high, period_low


@dataclass(frozen=True)
class ChartSeries:
    dates: list[str]
    closes: list[float]
    volumes: list[int]
    opens: list[float]
    highs: list[float]
    lows: list[float]
    markers: dict[str, Any]


def build_line_series(bars: list[OhlcvBar]) -> list[tuple[str, float]]:
    return [(bar.date.isoformat(), float(bar.close)) for bar in bars]


def build_candlestick_series(bars: list[OhlcvBar]) -> list[dict[str, float | int | str]]:
    return [
        {
            "x": bar.date.isoformat(),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": bar.volume,
        }
        for bar in bars
    ]


def prepare_chart_payload(bars: list[OhlcvBar]) -> ChartSeries:
    if not bars:
        return ChartSeries(dates=[], closes=[], volumes=[], opens=[], highs=[], lows=[], markers={})

    high, high_date = period_high(bars)
    low, low_date = period_low(bars)
    return ChartSeries(
        dates=[bar.date.isoformat() for bar in bars],
        closes=[float(bar.close) for bar in bars],
        volumes=[bar.volume for bar in bars],
        opens=[float(bar.open) for bar in bars],
        highs=[float(bar.high) for bar in bars],
        lows=[float(bar.low) for bar in bars],
        markers={
            "period_high": {"value": float(high), "date": high_date.isoformat()},
            "period_low": {"value": float(low), "date": low_date.isoformat()},
        },
    )
