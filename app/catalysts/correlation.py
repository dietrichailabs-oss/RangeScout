"""Watchlist-focused event matching, grouping, and relevance ordering."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.catalysts.classification import classify
from app.catalysts.dedupe import EventDeduplicator
from app.catalysts.entities import CatalystEvent, Relevance
from app.catalysts.relevance import rank
from app.catalysts.symbol_mapping import SymbolCatalog


DIRECTION_DISCLOSURE = "Automated event classification only — not price prediction or investment advice."


@dataclass(frozen=True, slots=True)
class CorrelatedEvent:
    event: CatalystEvent
    group_id: str
    duplicate_count: int = 1
    priority: int = 3


class CatalystCorrelator:
    def __init__(self, catalog: SymbolCatalog) -> None:
        self.catalog = catalog
        self.deduplicator = EventDeduplicator()

    def correlate(self, events: list[CatalystEvent], active_symbol: str, watchlist_symbols: set[str], watched_sectors: set[str]) -> list[CorrelatedEvent]:
        grouped: dict[str, CorrelatedEvent] = {}
        for incoming in events:
            if not self.deduplicator.accept(incoming):
                continue
            event = rank(classify(self.catalog.match(incoming)), active_symbol, watchlist_symbols, watched_sectors)
            group_id = _group_identity(event)
            priority = _priority(event, active_symbol, watchlist_symbols, watched_sectors)
            previous = grouped.get(group_id)
            if previous is None:
                grouped[group_id] = CorrelatedEvent(event, group_id, priority=priority)
            else:
                # Prefer the newer official event while preserving the number of corroborating items.
                chosen = event if event.published_at >= previous.event.published_at else previous.event
                grouped[group_id] = CorrelatedEvent(chosen, group_id, previous.duplicate_count + 1, min(priority, previous.priority))
        return sorted(grouped.values(), key=lambda item: (item.priority, -item.event.published_at.timestamp(), item.group_id))


def _group_identity(event: CatalystEvent) -> str:
    words = sorted(set(re.findall(r"[a-z0-9]+", event.title.lower())) - {"the", "a", "an", "new", "update"})
    basis = f"{event.category}|{','.join(event.symbols)}|{' '.join(words)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _priority(event: CatalystEvent, active_symbol: str, watchlist_symbols: set[str], watched_sectors: set[str]) -> int:
    symbols = set(event.symbols)
    if active_symbol.strip().upper() in symbols:
        return 0
    if symbols & {symbol.upper() for symbol in watchlist_symbols}:
        return 1
    if set(event.sectors) & watched_sectors:
        return 2
    return 3
