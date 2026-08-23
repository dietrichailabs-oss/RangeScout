"""Canonical internal market-data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Sequence


class DataDelay(str, Enum):
    REALTIME = "realtime"
    DELAYED = "delayed"
    END_OF_DAY = "end_of_day"
    OFFLINE = "offline"


class DataFreshnessState(str, Enum):
    LIVE = "live"
    CACHED = "cached"
    STALE = "stale"
    OFFLINE = "offline"


class AdjustmentMode(str, Enum):
    RAW = "raw"
    ADJUSTED = "adjusted"
    TOTAL_RETURN = "total_return"


class AssetType(str, Enum):
    STOCK = "stock"
    PREFERRED = "preferred"
    ETF = "etf"
    CLOSED_END_FUND = "closed_end_fund"
    INDEX = "index"
    COMMODITY_SPOT = "commodity_spot"
    FUTURE = "future"
    FOREX = "forex"
    CRYPTO = "crypto"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InstrumentIdentifier:
    symbol: str
    exchange: Optional[str] = None
    isin: Optional[str] = None
    figi: Optional[str] = None


@dataclass(frozen=True)
class Instrument:
    identifier: InstrumentIdentifier
    name: str
    asset_type: AssetType
    currency: str = "USD"
    provider: str = "unspecified"
    country: str | None = None
    sector: str | None = None


@dataclass(frozen=True)
class ProviderMetadata:
    provider_id: str
    provider_name: str
    supports_real_time: bool = False
    supports_adjusted: bool = False
    delay_label: DataDelay = DataDelay.DELAYED
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuoteSnapshot:
    instrument: Instrument
    last: Decimal
    previous_close: Optional[Decimal]
    volume: Optional[int]
    timestamp: datetime
    provider_timestamp: Optional[datetime]
    delay_label: DataDelay
    delay_seconds: Optional[int]
    source_timezone: str = "UTC"
    currency: str = "USD"
    freshness: DataFreshnessState = DataFreshnessState.LIVE
    day_low: Optional[Decimal] = None
    day_high: Optional[Decimal] = None
    fifty_two_week_low: Optional[Decimal] = None
    fifty_two_week_high: Optional[Decimal] = None
    average_volume: Optional[int] = None
    market_cap: Optional[Decimal] = None
    dividend_rate: Optional[Decimal] = None
    dividend_yield: Optional[Decimal] = None
    pre_market_price: Optional[Decimal] = None
    pre_market_change: Optional[Decimal] = None
    pre_market_change_percent: Optional[Decimal] = None
    after_hours_price: Optional[Decimal] = None
    after_hours_change: Optional[Decimal] = None
    after_hours_change_percent: Optional[Decimal] = None


@dataclass(frozen=True)
class OhlcvBar:
    instrument: InstrumentIdentifier
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    provider: str
    adjusted: bool = False
    source: str | None = None
    provider_timestamp: Optional[datetime] = None
    source_timezone: str = "UTC"


@dataclass(frozen=True)
class CorporateAction:
    instrument: InstrumentIdentifier
    action_type: str
    effective_at: date
    details: dict[str, Any]


@dataclass(frozen=True)
class Split(CorporateAction):
    pass


@dataclass(frozen=True)
class Dividend(CorporateAction):
    pass


@dataclass(frozen=True)
class SymbolChange(CorporateAction):
    pass


@dataclass(frozen=True)
class Alert:
    id: str
    watchlist: str
    instrument: InstrumentIdentifier
    condition: str
    payload: dict[str, Any]
    created_at: datetime
    enabled: bool = True


@dataclass(frozen=True)
class AlertEvent:
    alert_id: str
    instrument: InstrumentIdentifier
    message: str
    triggered_at: datetime
    severity: str = "info"


@dataclass(frozen=True)
class WatchlistEntry:
    instrument: InstrumentIdentifier
    added_at: datetime
    note: str | None = None


@dataclass(frozen=True)
class Watchlist:
    id: str
    title: str
    entries: list[WatchlistEntry] = field(default_factory=list)


@dataclass(frozen=True)
class ExportMetadata:
    exported_by: str
    instrument: InstrumentIdentifier
    started_at: datetime
    ended_at: datetime
    row_count: int
    path: str
    schema_version: str = "1.0"


def sanitize_bar_sequence(bars: Sequence[OhlcvBar]) -> list[OhlcvBar]:
    return sorted(bars, key=lambda x: x.date)
