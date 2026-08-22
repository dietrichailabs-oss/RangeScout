from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from app.catalysts.entities import CatalystEvent, Relevance
from app.catalysts.normalization import normalize_event


FEED_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
MINIMUM_POLL_SECONDS = 60.0


def parse_halt_rss(payload: bytes, received_at: datetime) -> list[CatalystEvent]:
    root = ElementTree.fromstring(payload)
    events: list[CatalystEvent] = []
    for item in root.findall(".//item"):
        title = _text(item, "title") or "Trading halt"
        link = _text(item, "link") or "https://www.nasdaqtrader.com/Trader.aspx?id=TradingHaltSearch"
        description = _text(item, "description") or ""
        symbol = (_text(item, "IssueSymbol") or _extract_symbol(title)).upper()
        published = _date(_text(item, "pubDate"), received_at)
        resumed = "resume" in f"{title} {description}".lower()
        status = "RESUMED" if resumed else "RESUMPTION PENDING" if "resumption" in description.lower() else "HALTED"
        event = normalize_event("Nasdaq Trader", link, published, f"{status} — {symbol}", received_at=received_at, summary=description, retention="summary_allowed", metadata={"official_status": status})
        events.append(replace(event, symbols=(symbol,), category="halt", urgency="critical", relevance=Relevance.HIGH))
    return events


def _text(item, name: str) -> str | None:
    direct = item.find(name)
    if direct is not None and direct.text:
        return direct.text.strip()
    for child in item:
        if child.tag.rsplit("}", 1)[-1] == name and child.text:
            return child.text.strip()
    return None


def _extract_symbol(title: str) -> str:
    return title.split()[0] if title.split() else "UNKNOWN"


def _date(value: str | None, fallback: datetime) -> datetime:
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc) if value else fallback
    except (TypeError, ValueError):
        return fallback
