"""Benchmark comparison helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.schemas import OhlcvBar
from app.analytics.calculations import percentage_change


@dataclass(frozen=True)
class ComparisonResult:
    symbol: str
    benchmark: str
    symbol_change_pct: Decimal
    benchmark_change_pct: Decimal
    relative_outperformance_pct: Decimal


def compare_symbols(symbol_bars: list[OhlcvBar], benchmark_bars: list[OhlcvBar], symbol: str, benchmark: str) -> ComparisonResult:
    symbol_pct = percentage_change(symbol_bars)
    benchmark_pct = percentage_change(benchmark_bars)
    return ComparisonResult(
        symbol=symbol,
        benchmark=benchmark,
        symbol_change_pct=symbol_pct,
        benchmark_change_pct=benchmark_pct,
        relative_outperformance_pct=symbol_pct - benchmark_pct,
    )
