"""Immutable events exchanged by the streaming subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class StreamState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STALE = "stale"
    ERROR = "error"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class TradeEvent:
    provider: str
    symbol: str
    price: Decimal
    size: Decimal
    timestamp: datetime
    event_id: str | None = None
    conditions: tuple[str, ...] = ()
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StreamStatus:
    provider: str
    state: StreamState
    message: str
    changed_at: datetime
    attempt: int = 0


class StreamingError(RuntimeError):
    """Sanitized error safe for logs and UI presentation."""
