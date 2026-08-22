"""Field-level market-data fusion with source provenance."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from app.models.schemas import OhlcvBar, QuoteSnapshot


@dataclass(frozen=True, slots=True)
class FusedPreviousClose:
    value: Decimal | None
    source: str


def previous_regular_close(quote: QuoteSnapshot | None, bars: Iterable[OhlcvBar]) -> FusedPreviousClose:
    if quote is not None and quote.previous_close not in (None, Decimal("0")):
        return FusedPreviousClose(quote.previous_close, "quote")
    completed = sorted(list(bars), key=lambda bar: bar.date)
    if not completed:
        return FusedPreviousClose(None, "unavailable")
    quote_day = quote.timestamp.date() if quote is not None else None
    candidates = [bar for bar in completed if quote_day is None or bar.date < quote_day]
    if not candidates and len(completed) >= 2:
        candidates = completed[:-1]
    if not candidates:
        candidates = completed
    return FusedPreviousClose(candidates[-1].close, "completed regular-session history")
