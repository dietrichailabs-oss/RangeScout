"""Provider contracts and deterministic response envelopes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

from app.models.schemas import (
    AdjustmentMode,
    CorporateAction,
    DataDelay,
    Instrument,
    InstrumentIdentifier,
    OhlcvBar,
    ProviderMetadata,
    QuoteSnapshot,
)


class ProviderResultType:
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


class ProviderError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class ProviderUnavailable(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderCapability:
    can_lookup_symbol: bool = True
    can_fetch_quote: bool = True
    can_fetch_historical: bool = True
    can_fetch_actions: bool = True
    delay: DataDelay = DataDelay.DELAYED
    supports_realtime: bool = False
    supports_adjusted: bool = True
    supports_indices: bool = True
    supports_etf: bool = True
    supports_stock: bool = True


@dataclass(frozen=True)
class ProviderResult:
    kind: str
    payload: Any
    timestamp: datetime
    metadata: ProviderMetadata
    warnings: list[str] = field(default_factory=list)


class MarketDataProvider(Protocol):
    provider_id: str
    provider_name: str
    capabilities: ProviderCapability

    def resolve_instrument(self, symbol: str) -> Instrument:
        """Resolve a symbol to a canonical instrument."""

    def fetch_quote(self, symbol: str) -> ProviderResult:
        """Return ProviderResult with payload=QuoteSnapshot."""

    def fetch_historical(
        self,
        identifier: InstrumentIdentifier,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResult:
        """Return ProviderResult with payload=Sequence[OhlcvBar]."""

    def fetch_actions(self, identifier: InstrumentIdentifier) -> ProviderResult:
        """Return ProviderResult with payload=Sequence[CorporateAction]."""

    def capabilities_report(self) -> ProviderMetadata:
        """Return provider capabilities and compliance details."""
