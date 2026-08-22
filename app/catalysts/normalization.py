from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Mapping

from app.catalysts.entities import CatalystEvent


def normalize_event(source: str, source_url: str, published_at: datetime, title: str, *, received_at: datetime | None = None, summary: str | None = None, body: str | None = None, retention: str = "metadata_only", metadata: Mapping[str, str] | None = None) -> CatalystEvent:
    clean_source = " ".join(source.split())
    clean_title = " ".join(title.split())
    if not clean_source or not clean_title or not source_url.startswith(("https://", "http://")):
        raise ValueError("Source, URL, and title are required.")
    published = _utc(published_at)
    received = _utc(received_at or datetime.now(timezone.utc))
    identity = hashlib.sha256(f"{clean_source.lower()}|{source_url.strip()}|{published.isoformat()}|{clean_title.lower()}".encode("utf-8")).hexdigest()
    event = CatalystEvent(identity, clean_source, source_url.strip(), published, received, clean_title, _clean(summary), _clean(body), retention=retention, metadata=dict(metadata or {}))
    return event.without_restricted_content()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
