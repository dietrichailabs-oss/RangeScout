from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class Relevance(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class CatalystEvent:
    event_id: str
    source: str
    source_url: str
    published_at: datetime
    received_at: datetime
    title: str
    summary: str | None = None
    body: str | None = None
    company_names: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    category: str = "other"
    relevance: Relevance = Relevance.LOW
    urgency: str = "normal"
    direction: str = "unclear"
    retention: str = "metadata_only"
    metadata: dict[str, str] = field(default_factory=dict)

    def without_restricted_content(self) -> "CatalystEvent":
        if self.retention == "full_text_allowed":
            return self
        return replace(self, body=None, summary=self.summary if self.retention == "summary_allowed" else None)
