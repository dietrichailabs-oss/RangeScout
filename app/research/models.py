"""Typed research values with provenance and availability metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping


class Availability(str, Enum):
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    STALE = "stale"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    PROVIDER_NOT_SUPPORTED = "provider_not_supported"
    LOOKUP_FAILED = "lookup_failed"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResearchValue:
    value: Decimal | str | None
    source: str
    period: str | None = None
    units: str | None = None
    filing_date: date | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    calculated_at: datetime | None = None
    availability: Availability = Availability.AVAILABLE
    selection_reason: str = ""
    taxonomy: str | None = None
    concept: str | None = None
    accession: str | None = None
    form: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    frame: str | None = None
    duration_days: int | None = None
    period_semantics: str | None = None
    period_mode: str | None = None
    comparability_result: str | None = None

    @classmethod
    def unavailable(cls, source: str, reason: str) -> "ResearchValue":
        return cls(None, source, availability=Availability.NOT_AVAILABLE, selection_reason=reason)


@dataclass(frozen=True, slots=True)
class CompanyProfile:
    symbol: str
    cik: str | None
    name: str | None
    exchange: str | None
    sic: str | None
    sic_description: str | None
    source: str = "SEC submissions"


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    symbol: str
    generation: int
    profile: CompanyProfile
    sections: Mapping[str, Mapping[str, ResearchValue]]
    retrieved_at: datetime
    warnings: tuple[str, ...] = ()

