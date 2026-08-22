from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.catalysts.correlation import CatalystCorrelator, DIRECTION_DISCLOSURE
from app.catalysts.entities import Relevance
from app.catalysts.normalization import normalize_event
from app.catalysts.symbol_mapping import SymbolCatalog


NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)


def event(url: str, title: str, minutes: int = 0):
    return normalize_event("Official", url, NOW + timedelta(minutes=minutes), title, received_at=NOW + timedelta(minutes=minutes))


def test_active_watchlist_sector_and_market_order() -> None:
    catalog = SymbolCatalog(); catalog.register("AAA", "Alpha Corp", "Energy", "Alpha"); catalog.register("BBB", "Beta Corp", "Technology", "Beta")
    values = [event("https://example.gov/market", "Broad market update"), event("https://example.gov/sector", "Energy regulation"), event("https://example.gov/b", "Beta Corp files 8-K"), event("https://example.gov/a", "Alpha Corp halted")]
    correlated = CatalystCorrelator(catalog).correlate(values, "AAA", {"BBB"}, {"Energy"})
    assert correlated[0].event.symbols == ("AAA",)
    assert correlated[0].event.relevance == Relevance.HIGH
    assert correlated[1].event.symbols == ("BBB",)
    assert correlated[-1].event.relevance == Relevance.LOW


def test_exact_duplicates_suppressed_and_equivalent_events_grouped() -> None:
    catalog = SymbolCatalog(); catalog.register("AAA", "Alpha Corp", "Energy", "Alpha")
    first = event("https://one.gov/a", "Alpha Corp halt")
    second = replace(first, event_id="different", source="Second official source", source_url="https://two.gov/a")
    result = CatalystCorrelator(catalog).correlate([first, first, second], "AAA", set(), set())
    assert len(result) == 1
    assert result[0].duplicate_count == 2
    assert "not price prediction" in DIRECTION_DISCLOSURE
