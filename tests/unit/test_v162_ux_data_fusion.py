from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

from app.alerts.presentation import humanize_event_code, market_event_filter
from app.catalysts.presentation import human_duration, human_event_title, safe_source_url
from app.company_data.search import LocalInstrumentSearch
from app.market_data.contracts import Capability, FabricRequest
from app.market_data.fusion import previous_regular_close
from app.market_data.providers.legacy_bridge import _normalize_finnhub_news, finnhub_descriptor
from app.models.schemas import AssetType, DataDelay, Instrument, InstrumentIdentifier, OhlcvBar, QuoteSnapshot
from app.notes.store import NoteStore
from app.scanner.engine import ScannerRow, aggregate_scanner_rows
from app.ui.formatting import format_financial_value


def _search_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE rs_instruments (
                instrument_id TEXT PRIMARY KEY, canonical_symbol TEXT, security_name TEXT,
                primary_venue TEXT, asset_class TEXT, is_active INTEGER
            );
            CREATE TABLE rs_instrument_aliases (instrument_id TEXT, alias_symbol TEXT);
            INSERT INTO rs_instruments VALUES
              ('equity:WMT','WMT','Walmart Inc.','NYSE','equity',1),
              ('equity:WAL','WAL','Walden Corporation','NASDAQ','equity',1),
              ('equity:TSLA','TSLA','Tesla, Inc.','NASDAQ','equity',1);
            INSERT INTO rs_instrument_aliases VALUES ('equity:WMT','WALMART');
            """
        )


def test_local_search_ranks_symbols_names_aliases_and_keeps_ambiguity(tmp_path) -> None:
    path = tmp_path / "search.sqlite"
    _search_database(path)
    search = LocalInstrumentSearch(path)
    assert search.resolve_unique("WMT").symbol == "WMT"
    assert search.resolve_unique("Walmart Inc").symbol == "WMT"
    assert search.resolve_unique("WALMART").symbol == "WMT"
    assert [row.symbol for row in search.search("Wal")][:2] == ["WAL", "WMT"]
    assert search.resolve_unique("a company that does not exist") is None


def test_financial_formatter_is_semantic_and_preserves_raw_values() -> None:
    raw = Decimal("1234567.89")
    assert format_financial_value(raw, "money").text == "$1,234,568"
    assert format_financial_value(raw, "money").raw is raw
    assert format_financial_value("12.5", "percent").text == "12.50%"
    assert format_financial_value("2026", "year").text == "2026"
    assert format_financial_value(None, "money").text == "N/A"


def test_news_capability_normalizes_deduplicates_and_rejects_unsafe_links() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 1, "headline": "Company files update", "url": "https://example.com/story", "source": "Wire", "datetime": int(now.timestamp())},
        {"id": 2, "headline": "Company files update", "url": "https://example.com/story", "source": "Wire", "datetime": int(now.timestamp())},
        {"id": 3, "headline": "Unsafe", "url": "http://example.com/no", "datetime": int(now.timestamp())},
    ]
    events = _normalize_finnhub_news(rows, "WMT", received_at=now)
    assert Capability.NEWS in finnhub_descriptor().capabilities
    assert len(events) == 1
    assert events[0].symbols == ("WMT",)
    assert events[0].body is None and events[0].summary is None
    assert safe_source_url(events[0].source_url) == "https://example.com/story"


def test_catalyst_and_market_event_presentation_is_human_readable() -> None:
    now = datetime.now(timezone.utc)
    assert human_event_title("Insider filed 4") == "Insider filed Form 4"
    assert human_duration(now - timedelta(minutes=1511), now) == "1 day ago"
    assert safe_source_url("https://www.sec.gov/Archives/test", official_only=True)
    assert safe_source_url("https://example.com/test", official_only=True) is None
    assert humanize_event_code("TRADE_HALT") == "Trading Halted"
    assert humanize_event_code("some_new_code") == "Some New Code"
    assert market_event_filter("REGULATORY_HALT") == "Regulatory"


def test_scanner_progressively_merges_sources_without_losing_fields() -> None:
    now = datetime.now(timezone.utc)
    first = ScannerRow("wmt", "Walmart Inc.", Decimal("100"), volume=10, sources=("Yahoo",), updated_at=now)
    second = ScannerRow("WMT", "", Decimal("101"), day_high=Decimal("102"), sources=("Finnhub",), updated_at=now + timedelta(seconds=1))
    result = aggregate_scanner_rows([first, second])
    assert len(result) == 1
    assert result[0].price == Decimal("101")
    assert result[0].volume == 10
    assert result[0].day_high == Decimal("102")
    assert result[0].sources == ("Yahoo", "Finnhub")


def _quote(previous_close=None) -> QuoteSnapshot:
    return QuoteSnapshot(
        Instrument(InstrumentIdentifier("WMT", "NYSE"), "Walmart Inc.", AssetType.STOCK, "USD", "fake"),
        Decimal("101"), previous_close, None, datetime(2026, 8, 21, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 21, 15, tzinfo=timezone.utc), DataDelay.DELAYED, 0, "USD",
    )


def test_previous_close_prefers_quote_then_completed_history() -> None:
    bars = [
        OhlcvBar(InstrumentIdentifier("WMT", "NYSE"), date(2026, 8, 19), Decimal("99"), Decimal("101"), Decimal("98"), Decimal("100"), 100, "fake"),
        OhlcvBar(InstrumentIdentifier("WMT", "NYSE"), date(2026, 8, 20), Decimal("100"), Decimal("102"), Decimal("99"), Decimal("100.5"), 100, "fake"),
    ]
    assert previous_regular_close(_quote(Decimal("100.75")), bars).source == "quote"
    fallback = previous_regular_close(_quote(), bars)
    assert fallback.value == Decimal("100.5")
    assert fallback.source == "completed regular-session history"


def test_notes_create_edit_reload_delete_and_restart(tmp_path) -> None:
    path = tmp_path / "notes.json"
    store = NoteStore(path)
    created = store.add("wmt", "full persisted contents", "Research Notes")
    updated = store.update(created.id, symbol="WMT", text="edited contents", category="Earnings Notes")
    assert updated.id == created.id
    assert len(store.list_for()) == 1
    restarted = NoteStore(path)
    assert restarted.get(created.id).text == "edited contents"
    assert restarted.get(created.id).category == "Earnings Notes"
    assert restarted.delete(created.id)
    restarted.reload()
    assert restarted.list_for() == []
