"""Safe, human-readable catalyst/news presentation helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlparse


_OFFICIAL_HOSTS = {
    "sec.gov", "www.sec.gov", "nasdaqtrader.com", "www.nasdaqtrader.com",
    "congress.gov", "www.congress.gov", "api.congress.gov",
    "whitehouse.gov", "www.whitehouse.gov",
}


def human_duration(published_at: datetime, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    stamp = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return "just now" if seconds < 10 else f"{seconds} seconds ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def human_event_title(title: str) -> str:
    value = re.sub(r"\bfiled\s+(?=(?:4|8-K|10-K|10-Q|S-\d|DEF\s+14A)\b)", "filed Form ", str(title), flags=re.IGNORECASE)
    return value.replace("_", " ").strip()


def safe_source_url(url: str, *, official_only: bool = False) -> str | None:
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    if official_only and host not in _OFFICIAL_HOSTS:
        return None
    return parsed.geturl()


def source_link_label(source: str) -> str:
    normalized = str(source).lower()
    if "sec" in normalized:
        return "Open SEC filing"
    if "nasdaq" in normalized:
        return "Open official notice"
    if "congress" in normalized or "white house" in normalized:
        return "Open official source"
    return "Open article"
