"""Normalization and validation helpers for provider data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from app.domain.errors import DataQualityError
from app.models.schemas import OhlcvBar


@dataclass
class ValidationResult:
    valid: bool
    removed: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    warnings: list[str] | None = None


def normalize_bar(bar: OhlcvBar) -> OhlcvBar:
    if bar.volume < 0:
        raise DataQualityError(f"Negative volume in bar {bar.date}")
    if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
        raise DataQualityError(f"Non-positive OHLC value in bar {bar.date}")
    if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
        raise DataQualityError(f"Inconsistent OHLC ordering in bar {bar.date}")
    if bar.open > bar.high or bar.open < bar.low or bar.close > bar.high or bar.close < bar.low:
        raise DataQualityError(f"OHLC out of range in bar {bar.date}")
    return bar


def normalize_histories(bars: Sequence[OhlcvBar]) -> tuple[list[OhlcvBar], ValidationResult]:
    cleaned = []
    removed = duplicates = out_of_order = 0
    warnings = []
    seen_dates: set[date] = set()

    for bar in sorted(bars, key=lambda b: b.date):
        try:
            bar = normalize_bar(bar)
        except DataQualityError as exc:
            warnings.append(str(exc))
            removed += 1
            continue

        if bar.date in seen_dates:
            duplicates += 1
            continue
        if cleaned and bar.date < cleaned[-1].date:
            out_of_order += 1
            continue
        seen_dates.add(bar.date)
        cleaned.append(bar)

    return cleaned, ValidationResult(
        valid=bool(cleaned),
        removed=removed,
        duplicates=duplicates,
        out_of_order=out_of_order,
        warnings=warnings,
    )
