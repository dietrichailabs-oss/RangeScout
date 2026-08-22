"""Normalized analyst outlook models with explicit provider/cache state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping

from app.research.models import ResearchValue


class AnalystState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    FRESH = "fresh"
    CACHED = "cached"
    STALE_CACHED = "stale_cached"
    UNAUTHORIZED = "unauthorized"
    ENTITLEMENT_UNAVAILABLE = "entitlement_unavailable"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


class AnalystProviderError(RuntimeError):
    """Sanitized provider failure that never contains credentials or request URLs."""

    def __init__(self, state: AnalystState, message: str) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True, slots=True)
class AnalystResult:
    symbol: str
    generation: int
    values: Mapping[str, ResearchValue]
    provider_states: Mapping[str, AnalystState]
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    messages: tuple[str, ...] = ()
