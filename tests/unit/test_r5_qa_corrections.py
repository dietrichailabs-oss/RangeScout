from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.research.analyst.models import AnalystResult, AnalystState
from app.research.models import CompanyProfile, ResearchSnapshot, ResearchValue
from app.research.routing import ResearchRoute, plan_research
from tests.unit.test_ui_v12_composition import window


@pytest.fixture(scope="module")
def r5_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("r5-canonical") / "history.sqlite"
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    return path


def test_exact_symbol_identity_tier_cannot_be_outscored_by_issuer_name(r5_database: Path) -> None:
    resolver = InstrumentResolver(r5_database)
    results = resolver.search("NMI", 10)
    assert results[0].symbol == "NMI"
    assert results[0].match_kind == "exact_symbol"
    assert resolver.resolve_unique("NMI").symbol == "NMI"
    assert all(item.symbol != "NMIH" for item in results)


def test_all_active_canonical_symbols_keep_exact_identity_tier(r5_database: Path) -> None:
    resolver = InstrumentResolver(r5_database)
    with sqlite3.connect(r5_database) as connection:
        symbols = [row[0] for row in connection.execute(
            "SELECT DISTINCT canonical_symbol FROM rs_instruments WHERE is_active=1 ORDER BY canonical_symbol"
        )]
    failures = []
    for symbol in symbols:
        results = resolver.search(symbol, 5)
        if not results or results[0].symbol != symbol or results[0].match_kind != "exact_symbol":
            failures.append((symbol, [(item.symbol, item.match_kind) for item in results]))
    assert len(symbols) >= 16_000
    assert failures == []


@pytest.mark.parametrize(
    ("symbol", "asset_class", "route"),
    [
        ("PDI", "closed_end_fund", ResearchRoute.FUND),
        ("NUV", "closed_end_fund", ResearchRoute.FUND),
        ("NMZ", "closed_end_fund", ResearchRoute.FUND),
        ("RCS", "closed_end_fund", ResearchRoute.FUND),
        ("PFL", "closed_end_fund", ResearchRoute.FUND),
        ("PFN", "closed_end_fund", ResearchRoute.FUND),
        ("UTF", "closed_end_fund", ResearchRoute.FUND),
        ("BST", "closed_end_fund", ResearchRoute.FUND),
        ("BME", "closed_end_fund", ResearchRoute.FUND),
        ("ETO", "closed_end_fund", ResearchRoute.FUND),
        ("EOS", "closed_end_fund", ResearchRoute.FUND),
        ("BOE", "closed_end_fund", ResearchRoute.FUND),
        ("HAVAR", "right", ResearchRoute.MARKET_INSTRUMENT),
        ("BDMDW", "warrant", ResearchRoute.MARKET_INSTRUMENT),
        ("XSLLU", "unit", ResearchRoute.MARKET_INSTRUMENT),
        ("OTAI.U", "unit", ResearchRoute.MARKET_INSTRUMENT),
        ("TRIB", "adr", ResearchRoute.CORPORATE),
    ],
)
def test_fixed_r4_classification_and_research_route(
    r5_database: Path, symbol: str, asset_class: str, route: ResearchRoute,
) -> None:
    match = InstrumentResolver(r5_database).resolve_unique(symbol)
    assert match is not None
    assert match.instrument.asset_class == asset_class
    assert plan_research(match.instrument.asset_class, match.instrument.subtype).route is route


@pytest.mark.parametrize("symbol", ["HAVAR", "MTB$J", "BATRB", "BDMDW", "XSLLU", "OTAI.U", "TRIB"])
def test_exact_official_names_resolve_to_the_same_security(r5_database: Path, symbol: str) -> None:
    resolver = InstrumentResolver(r5_database)
    with sqlite3.connect(r5_database) as connection:
        name = connection.execute(
            "SELECT security_name FROM rs_instruments WHERE canonical_symbol=? AND is_active=1 ORDER BY instrument_id DESC",
            (symbol,),
        ).fetchone()[0]
    resolved = resolver.resolve_unique(name)
    assert resolved is not None
    assert resolved.symbol == symbol


def test_sec_classification_overlay_is_provenance_carrying_and_persisted(r5_database: Path) -> None:
    payload = json.loads(Path("resources/RangeScout_Instrument_Classifications.json").read_text(encoding="utf-8"))
    assert payload["candidates_checked"] == 522
    assert len(payload["classifications"]) >= 350
    assert all("symbol" not in row and row["cik"] for row in payload["classifications"])
    with sqlite3.connect(r5_database) as connection:
        row = connection.execute(
            """SELECT i.asset_class,i.instrument_subtype,c.source_id,c.authority_level,c.evidence_type,c.source_url
               FROM rs_instruments i JOIN rs_instrument_classifications c USING(instrument_id)
               WHERE i.canonical_symbol='PDI' AND c.is_active=1"""
        ).fetchone()
    assert row == (
        "closed_end_fund", "closed_end_fund", "sec_company_submissions_investment_company_forms",
        "official", "sec_form_history", "https://data.sec.gov/submissions/CIK0001510599.json",
    )


def test_fund_overview_removes_corporate_na_metrics(window) -> None:
    window.set_active_symbol("BOE", source="global-search")
    state = window.active_symbol.state
    snapshot = ResearchSnapshot(
        "BOE", state.generation, CompanyProfile("BOE", "0001320375", "BlackRock Enhanced Global Dividend Trust", "NYSE", None, None),
        {"Overview": {
            "Instrument structure": ResearchValue("Fund / closed-end fund", "SEC"),
            "SEC registrant": ResearchValue("BlackRock Enhanced Global Dividend Trust", "SEC"),
            "Latest fund filing": ResearchValue("N-CSR", "SEC"),
        }}, datetime.now(timezone.utc),
    )
    window._apply_research_snapshot(snapshot)
    text = window.research_key_metrics_text.text()
    assert "Structure" in text and "Latest fund filing" in text
    assert "Revenue" not in text and "Net income" not in text and "Equity  N/A" not in text


def test_analyst_no_key_state_is_compact_and_has_no_empty_table_row(window) -> None:
    state = window.active_symbol.state
    result = AnalystResult(
        state.symbol, state.generation, {},
        {"finnhub": AnalystState.NOT_CONFIGURED, "alpha_vantage": AnalystState.NOT_CONFIGURED},
        messages=("Optional analyst providers are not configured.",),
    )
    window._apply_analyst_result(result)
    assert not window.research_analyst_empty_state.isHidden()
    assert window.research_tables["Analyst Outlook"].isHidden()
    assert window.research_tables["Analyst Outlook"].rowCount() == 0
    assert "Not Configured" in window.research_analyst_empty_state.text()
