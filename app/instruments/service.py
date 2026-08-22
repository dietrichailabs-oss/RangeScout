"""Instrument service APIs."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.errors import ValidationError
from app.models.schemas import Instrument
from app.providers.base import MarketDataProvider


@dataclass(frozen=True)
class LookupFailure(ValidationError):
    symbol: str


def lookup_symbol(provider: MarketDataProvider, symbol: str) -> Instrument:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValidationError("symbol is required")
    return provider.resolve_instrument(normalized)
