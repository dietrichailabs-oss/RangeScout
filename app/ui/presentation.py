"""Accessible directional and freshness presentation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DirectionalPrice:
    text: str
    direction: str
    arrow: str
    color: str


def directional_price(price: Decimal | None, previous_regular_close: Decimal | None, currency: str = "USD") -> DirectionalPrice:
    if price is None:
        return DirectionalPrice("— Price unavailable", "unavailable", "—", "neutral")
    if previous_regular_close in (None, Decimal("0")):
        return DirectionalPrice(f"— {price:,.2f} {currency}  Change N/A", "flat", "—", "neutral")
    change = price - previous_regular_close
    percent = change / previous_regular_close * Decimal(100)
    if change > 0:
        return DirectionalPrice(f"▲ {price:,.2f} {currency}  +{change:,.2f} (+{percent:.2f}%)", "up", "▲", "#22c55e")
    if change < 0:
        return DirectionalPrice(f"▼ {price:,.2f} {currency}  {change:,.2f} ({percent:.2f}%)", "down", "▼", "#f05252")
    return DirectionalPrice(f"— {price:,.2f} {currency}  0.00 (0.00%)", "flat", "—", "neutral")


def freshness_label(*, freshness: object, delay: object, received_at: datetime, now: datetime | None = None) -> str:
    fresh = str(getattr(freshness, "value", freshness)).lower()
    delayed = str(getattr(delay, "value", delay)).lower()
    current = now or datetime.now(timezone.utc)
    stamp = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()))
    if fresh == "offline":
        return "Offline"
    if fresh in {"cached", "stale"}:
        return "Cached" if seconds < 60 else f"Cached {seconds // 60}m"
    if delayed in {"delayed", "end_of_day"}:
        return "Delayed"
    return "Live" if seconds < 2 else f"{seconds}s ago"
