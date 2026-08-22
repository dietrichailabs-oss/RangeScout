from __future__ import annotations

from dataclasses import replace

from app.catalysts.entities import CatalystEvent, Relevance


def rank(event: CatalystEvent, active_symbol: str, watchlist_symbols: set[str], watched_sectors: set[str]) -> CatalystEvent:
    active = active_symbol.strip().upper()
    symbols = set(event.symbols)
    if active and active in symbols:
        level = Relevance.HIGH
    elif symbols & {value.upper() for value in watchlist_symbols}:
        level = Relevance.HIGH
    elif set(event.sectors) & watched_sectors:
        level = Relevance.MEDIUM
    else:
        level = Relevance.LOW
    return replace(event, relevance=level)
