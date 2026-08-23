from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from tests.unit.test_ui_v12_composition import window


@pytest.fixture(scope="module")
def r6_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("r6-canonical") / "history.sqlite"
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    return path


@pytest.mark.parametrize("symbol", ["AGNCL", "AUB$A"])
def test_long_official_names_are_local_resolver_inputs_without_truncation(
    r6_database: Path, symbol: str,
) -> None:
    resolver = InstrumentResolver(r6_database)
    with sqlite3.connect(r6_database) as connection:
        official_name = connection.execute(
            "SELECT security_name FROM rs_instruments WHERE canonical_symbol=? AND is_active=1",
            (symbol,),
        ).fetchone()[0]
    assert len(official_name) > 160
    results = resolver.search(official_name, 10)
    assert results and results[0].symbol == symbol
    resolved = resolver.resolve_unique(official_name)
    assert resolved is not None and resolved.symbol == symbol


@pytest.mark.parametrize(
    ("query", "expected", "preferred"),
    [
        ("gold", {"GOLD", "XAU/USD"}, "XAU/USD"),
        ("Gold", {"GOLD", "XAU/USD"}, "XAU/USD"),
        ("Dow", {"DOW", "^DJI"}, "^DJI"),
        ("DJIA", {"DJIA", "^DJI"}, "DJIA"),
        ("BTC", {"BTC", "BTC/USD"}, "BTC"),
    ],
)
def test_collision_aware_search_exposes_all_high_confidence_meanings(
    r6_database: Path, query: str, expected: set[str], preferred: str,
) -> None:
    resolver = InstrumentResolver(r6_database)
    results = resolver.search(query, 10)
    assert results[0].symbol == preferred
    assert expected.issubset({item.symbol for item in results})
    assert resolver.resolve_unique(query) is None
    selected = resolver.by_id(next(item.instrument.instrument_id for item in results if item.symbol == preferred))
    assert selected is not None and selected.symbol == preferred


def test_noncolliding_exact_ticker_remains_fast_and_unique(r6_database: Path) -> None:
    resolver = InstrumentResolver(r6_database)
    results = resolver.search("NMI", 10)
    assert results[0].symbol == "NMI" and results[0].match_kind == "exact_symbol"
    assert resolver.resolve_unique("NMI").symbol == "NMI"


def test_research_cards_are_instrument_aware(window) -> None:
    window.set_active_symbol("AAPL", source="global-search")
    assert window.research_profile_card_title.text() == "COMPANY PROFILE"
    assert window.research_metrics_card_title.text() == "KEY METRICS & FUNDAMENTALS"

    window.set_active_symbol("BOE", source="global-search")
    assert window.research_profile_card_title.text() == "FUND PROFILE"
    assert window.research_metrics_card_title.text() == "FUND STRUCTURE & MARKET METRICS"
    assert "SEC fund filings" in window.research_metrics_card_subtitle.text()

    window.set_active_symbol("XAU/USD", source="global-search")
    assert window.research_profile_card_title.text() == "INSTRUMENT PROFILE"
    assert window.research_metrics_card_title.text() == "MARKET METRICS & AVAILABILITY"
    assert "not applicable" in window.research_metrics_card_subtitle.text().lower()


def test_traceable_source_detail_collapses_the_entire_card(window) -> None:
    assert window.research_provenance_card.maximumHeight() == 96
    assert window.research_provenance_table.isHidden()
    window.research_provenance_toggle.setChecked(True)
    assert window.research_provenance_card.maximumHeight() == 360
    assert not window.research_provenance_table.isHidden()
    window.research_provenance_toggle.setChecked(False)
    assert window.research_provenance_card.maximumHeight() == 96
    assert window.research_provenance_table.isHidden()


def test_chart_empty_states_do_not_require_manual_refresh(window) -> None:
    window.set_active_symbol("XAU/USD", source="global-search")
    assert "Refresh" not in window.research_chart._empty_state_text
    assert window.research_chart._empty_state_text in {
        "Loading XAU/USD price history…",
        "Offline/local mode — no cached price history is available.",
    }
    window._set_chart_empty_state("Price history unavailable from the configured provider.")
    assert window.research_chart._empty_state_text == "Price history unavailable from the configured provider."