from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CompanyLogoStatus(str, Enum):
    AVAILABLE = "available"
    UNCONFIGURED = "unconfigured"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CompanyLogoAsset:
    symbol: str
    exchange: str | None
    provider_id: str
    status: CompanyLogoStatus
    image_bytes: bytes | None = None
    content_type: str | None = None
    content_sha256: str | None = None
    fetched_at: datetime | None = None
    retry_after: datetime | None = None
    message: str = ""
    source_url: str | None = None
    lookup_identifier: str | None = None
    license_metadata: str | None = None
    persistent_local_copy: bool = False

    @property
    def has_image(self) -> bool:
        return bool(self.image_bytes) and self.status is CompanyLogoStatus.AVAILABLE
