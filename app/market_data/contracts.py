"""Stable provider-fabric contracts with complete provenance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4


class AssetClass(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    ETN = "etn"
    ADR = "adr"
    PREFERRED = "preferred"
    WARRANT = "warrant"
    RIGHT = "right"
    UNIT = "unit"
    MUTUAL_FUND = "mutual_fund"
    OTC = "otc"
    INDEX = "index"
    FUTURE = "future"
    OPTION = "option"
    FX = "fx"
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERPETUAL = "crypto_perpetual"
    CRYPTO_FUTURE = "crypto_future"
    MACRO = "macro"
    UNKNOWN = "unknown"


class Capability(str, Enum):
    QUOTE = "quote"
    BID_ASK = "bid_ask"
    HISTORICAL = "historical"
    INTRADAY = "intraday"
    STREAMING = "streaming"
    TRADES = "trades"
    CANDLES = "candles"
    FUNDAMENTALS = "fundamentals"
    SYMBOL_SEARCH = "symbol_search"
    UNIVERSE = "universe"
    CORPORATE_ACTIONS = "corporate_actions"
    MARKET_STATUS = "market_status"
    MACRO_SERIES = "macro_series"
    NEWS = "news"


class CredentialKind(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    TOKEN = "token"


class DelayClass(str, Enum):
    REALTIME = "realtime"
    DELAYED = "delayed"
    END_OF_DAY = "end_of_day"
    REFERENCE = "reference"


@dataclass(frozen=True)
class FreshnessPolicy:
    max_age: timedelta
    allow_delayed: bool = True
    allow_end_of_day: bool = False

    def accepts(self, timestamp: datetime, delay: DelayClass, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        normalized = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        if normalized > current + timedelta(minutes=5):
            return False
        if current - normalized > self.max_age:
            return False
        if delay == DelayClass.DELAYED and not self.allow_delayed:
            return False
        if delay in {DelayClass.END_OF_DAY, DelayClass.REFERENCE} and not self.allow_end_of_day:
            return False
        return True


@dataclass(frozen=True)
class ProviderTerms:
    documentation_url: str
    reviewed_on: str
    automated_access: str
    attribution: str = ""
    caching: str = ""
    redistribution: str = ""
    decision: str = "disabled"
    reason: str = ""


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    asset_classes: frozenset[AssetClass]
    capabilities: frozenset[Capability]
    requires_credentials: bool
    credential_kind: CredentialKind
    delay_class: DelayClass
    terms: ProviderTerms
    enabled: bool = False
    max_concurrency: int = 2
    minimum_request_interval_seconds: float = 0.0


@dataclass(frozen=True)
class FabricRequest:
    canonical_instrument_id: str
    canonical_symbol: str
    asset_class: AssetClass
    capability: Capability
    venue: str | None = None
    requested_fields: tuple[str, ...] = ()
    start: datetime | None = None
    end: datetime | None = None
    interval: str | None = None
    adjustment: str = "raw"
    freshness: FreshnessPolicy | None = None
    caller_context: str = "application"
    request_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class FabricResult:
    request_id: str
    provider_id: str
    provider_symbol: str
    canonical_instrument_id: str
    canonical_symbol: str
    capability: Capability
    provider_timestamp: datetime
    received_at: datetime
    delay_class: DelayClass
    currency: str
    venue: str | None
    payload: Any
    attribution: str
    cache_ttl_seconds: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RateLimitState:
    limited: bool = False
    retry_after_seconds: float | None = None
    remaining: int | None = None
    reset_at: datetime | None = None


class FabricProviderError(RuntimeError):
    """A provider request failed without exposing credentials or raw secret URLs."""


class RateLimited(FabricProviderError):
    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("Provider rate limit reached.")
        self.retry_after_seconds = retry_after_seconds


@runtime_checkable
class MarketDataAdapter(Protocol):
    descriptor: ProviderDescriptor

    def normalize_symbol(self, symbol: str) -> str: ...

    def provider_symbol_for(self, request: FabricRequest) -> str: ...

    def request(self, request: FabricRequest) -> FabricResult: ...

    def health_check(self) -> bool: ...

    def rate_limit_state(self) -> RateLimitState: ...

    def list_instruments(self) -> list[dict[str, Any]]: ...
