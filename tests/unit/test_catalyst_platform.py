from __future__ import annotations

from datetime import datetime, timezone

from app.catalysts.classification import classify
from app.catalysts.dedupe import EventDeduplicator
from app.catalysts.entities import Relevance
from app.catalysts.normalization import normalize_event
from dataclasses import replace
from app.catalysts.relevance import rank
from app.catalysts.storage import CatalystStore
from app.catalysts.symbol_mapping import SymbolCatalog


NOW = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)


def test_normalize_map_classify_rank_and_dedupe(tmp_path) -> None:
    event = normalize_event(" SEC ", "https://www.sec.gov/example", NOW, " Acme files 8-K ", summary="  Acme Corp update  ", body="restricted full text", retention="summary_allowed", received_at=NOW)
    assert event.body is None and event.summary == "Acme Corp update"
    catalog = SymbolCatalog(); catalog.register("ACME", "Acme Corp", "Industrials", "Acme")
    event = rank(classify(catalog.match(event)), "ACME", set(), set())
    assert event.symbols == ("ACME",)
    assert event.category == "sec_filing"
    assert event.relevance == Relevance.HIGH
    dedupe = EventDeduplicator()
    assert dedupe.accept(event) is True
    assert dedupe.accept(event) is False
    store = CatalystStore(tmp_path / "events.json"); store.save([event])
    raw = (tmp_path / "events.json").read_text(encoding="utf-8")
    assert "restricted full text" not in raw
    assert "Acme Corp update" in raw


def test_relevance_order_active_watchlist_sector_market() -> None:
    base = normalize_event("Source", "https://example.gov/a", NOW, "Event", received_at=NOW)
    catalog = SymbolCatalog(); catalog.register("AAA", "Alpha Inc", "Energy", "Alpha")
    matched = catalog.match(base.__class__(**{field: getattr(base, field) for field in base.__dataclass_fields__}.__or__({"title": "Alpha update"})))
    assert rank(matched, "AAA", set(), set()).relevance == Relevance.HIGH
    assert rank(matched, "BBB", {"AAA"}, set()).relevance == Relevance.HIGH
    assert rank(matched, "BBB", set(), {"Energy"}).relevance == Relevance.MEDIUM
    assert rank(matched, "BBB", set(), set()).relevance == Relevance.LOW


def test_catalog_preserves_authoritative_symbols_and_enriches_without_duplicates() -> None:
    catalog = SymbolCatalog()
    catalog.register("AAPL", "Apple Inc", "Technology", "Apple")
    catalog.register("MSFT", "Microsoft Corp", "Technology", "Microsoft")
    base = normalize_event("SEC", "https://www.sec.gov/a", NOW, "Apple and Microsoft update", received_at=NOW)
    event = replace(base, symbols=("aapl", "AAPL"), company_names=("Apple Inc",), sectors=("Technology",))
    matched = catalog.match(event)
    assert matched.symbols == ("AAPL", "MSFT")
    assert matched.company_names == ("Apple Inc", "Microsoft Corp")
    assert matched.sectors == ("Technology",)


def test_catalog_never_erases_explicit_sec_or_nasdaq_symbol() -> None:
    empty = SymbolCatalog()
    sec = replace(normalize_event("SEC", "https://www.sec.gov/a", NOW, "8-K", received_at=NOW), symbols=("AAPL",))
    halt = replace(normalize_event("Nasdaq Trader", "https://www.nasdaqtrader.com/x", NOW, "HALTED", received_at=NOW), symbols=("XYZ",))
    assert empty.match(sec).symbols == ("AAPL",)
    assert empty.match(halt).symbols == ("XYZ",)
