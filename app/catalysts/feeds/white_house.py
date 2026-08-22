from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from app.catalysts.entities import CatalystEvent
from app.catalysts.normalization import normalize_event


FEED_URL = "https://www.whitehouse.gov/feed/"


def parse_feed(payload: bytes, received_at: datetime) -> list[CatalystEvent]:
    root = ElementTree.fromstring(payload)
    events: list[CatalystEvent] = []
    for item in root.findall(".//item"):
        title = _value(item, "title")
        link = _value(item, "link")
        if not title or not link:
            continue
        category = _value(item, "category") or "White House"
        published = _published(_value(item, "pubDate"), received_at)
        events.append(normalize_event("White House", link, published, title, received_at=received_at, summary=category, retention="metadata_only", metadata={"section": category}))
    return events


def _value(item, tag: str) -> str | None:
    node = item.find(tag)
    return node.text.strip() if node is not None and node.text else None


def _published(value: str | None, fallback: datetime) -> datetime:
    try: return parsedate_to_datetime(value).astimezone(timezone.utc) if value else fallback
    except (TypeError, ValueError): return fallback
