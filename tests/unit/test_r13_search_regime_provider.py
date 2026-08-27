from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.company_data.search_normalization import normalize_search_text
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION
from app.historical_store.repository import HistoricalStore
from app.market_data.provider_symbols import derive_yahoo_provider_symbol
from app.research.fundamentals import ResearchService
from app.research.models import Availability


@pytest.fixture(scope="module")
def r13_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("r13-search") / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    return database


@pytest.mark.parametrize(("left", "right"), [
    ("Papa John's International, Inc.", "Papa Johns International"),
    ("Lands' End, Inc.", "Lands End"),
    ("F/m High Yield 100 ETF", "F m High Yield 100 ETF"),
    ("Q/C Technologies, Inc.", "Q C Technologies"),
    ("Smith & Wesson", "Smith and Wesson"),
    ("Alpha-Beta Corp.", "alpha beta"),
    ("A.B.C., Holdings", "a b c"),
    ("Units, each representing 1/40th interest", "units each representing 1 40th interest"),
])
def test_one_canonical_natural_name_normalizer(left: str, right: str) -> None:
    assert normalize_search_text(left) == normalize_search_text(right)


@pytest.mark.parametrize(("query", "expected"), [
    ("Papa Johns", "PZZA"),
    ("Papa Johns International", "PZZA"),
    ("Ollies Bargain Outlet", "OLLI"),
    ("Lands End", "LE"),
    ("PT Bukit Asam ADR", "PBATF"),
    ("Q C Technologies", "QCLS"),
    ("Barrons 400 ETF", "BFOR"),
    ("F m High Yield 100 ETF", "ZTOP"),
])
def test_qa_punctuation_queries_retrieve_and_rank_expected(
    r13_database: Path, query: str, expected: str,
) -> None:
    results = InstrumentResolver(r13_database).search(query, 20)
    assert results and results[0].symbol == expected


def test_search_candidate_retrieval_uses_fts_index(r13_database: Path) -> None:
    with sqlite3.connect(r13_database) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT instrument_id FROM rs_instrument_search_fts "
            "WHERE rs_instrument_search_fts MATCH ?", ('"papa" AND "johns"*',),
        ).fetchall()
        version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        documents = connection.execute("SELECT COUNT(*) FROM rs_instrument_search_fts").fetchone()[0]
    assert int(version) == CURRENT_SCHEMA_VERSION == 15
    assert documents >= 16_382
    assert any("VIRTUAL TABLE INDEX" in str(row).upper() for row in plan)


def fact(
    value: int,
    *,
    end: str,
    filed: str,
    form: str,
    fy: int,
    accession: str,
) -> dict[str, object]:
    return {
        "val": value,
        "end": end,
        "filed": filed,
        "form": form,
        "accn": accession,
        "fy": fy,
        "fp": "FY",
    }


class FixtureClient:
    def __init__(self, facts: dict[str, object]) -> None:
        self.facts = facts

    def company_map(self):
        return {"TEST": {"cik": "0000000001", "name": "Transition Test"}}

    def companyfacts(self, _cik):
        return {"entityName": "Transition Test", "facts": self.facts}

    def submissions(self, _cik):
        return {"exchanges": ["NYSE"], "sic": "1000", "sicDescription": "Test"}


def monetary_taxonomy(
    taxonomy: str,
    currency: str,
    *,
    end: str,
    filed: str,
    form: str,
    fy: int,
    accession: str,
    value: int,
    include_revenue: bool = True,
) -> dict[str, object]:
    names = (
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "NetIncomeLoss", "Assets", "StockholdersEquity")
        if taxonomy == "us-gaap"
        else ("Revenue", "ProfitLoss", "Assets", "Equity")
    )
    result = {}
    for index, concept in enumerate(names):
        if index == 0 and not include_revenue:
            continue
        result[concept] = {"units": {currency: [fact(
            value + index,
            end=end,
            filed=filed,
            form=form,
            fy=fy,
            accession=accession,
        )]}}
    return result


@pytest.mark.parametrize(("current_taxonomy", "current_currency", "current_form", "old_taxonomy", "old_currency", "old_form"), [
    ("ifrs-full", "EUR", "20-F", "us-gaap", "USD", "10-K"),
    ("us-gaap", "USD", "10-K", "ifrs-full", "EUR", "20-F"),
])
def test_latest_reporting_regime_beats_larger_older_taxonomy_history(
    current_taxonomy: str,
    current_currency: str,
    current_form: str,
    old_taxonomy: str,
    old_currency: str,
    old_form: str,
) -> None:
    current = monetary_taxonomy(
        current_taxonomy, current_currency, end="2025-12-31", filed="2026-04-01",
        form=current_form, fy=2025, accession="current", value=500,
    )
    older = monetary_taxonomy(
        old_taxonomy, old_currency, end="2024-12-31", filed="2025-04-01",
        form=old_form, fy=2024, accession="old", value=100,
    )
    # Historical abundance must not outweigh the current period.
    for node in older.values():
        rows = next(iter(node["units"].values()))
        rows.extend([dict(rows[0], end="2023-12-31", filed="2024-04-01", accn="older", fy=2023)])
    snapshot = ResearchService(FixtureClient({current_taxonomy: current, old_taxonomy: older})).load("TEST")
    revenue = snapshot.sections["Overview"]["Revenue"]
    assert revenue.availability is Availability.AVAILABLE
    assert revenue.value == Decimal(500)
    assert revenue.units == current_currency
    assert revenue.period == "2025-12-31"
    assert current_taxonomy in snapshot.warnings[0]


def test_current_currency_wins_and_cross_currency_growth_is_unavailable() -> None:
    current = monetary_taxonomy(
        "ifrs-full", "TWD", end="2025-12-31", filed="2026-04-01",
        form="20-F", fy=2025, accession="current", value=500,
    )
    older = monetary_taxonomy(
        "ifrs-full", "USD", end="2024-12-31", filed="2025-04-01",
        form="20-F", fy=2024, accession="old", value=100,
    )
    merged = dict(current)
    for concept, node in older.items():
        merged.setdefault(concept, {"units": {}})["units"].update(node["units"])
    snapshot = ResearchService(FixtureClient({"ifrs-full": merged})).load("TEST")
    assert snapshot.sections["Overview"]["Revenue"].units == "TWD"
    assert snapshot.sections["Overview"]["Revenue"].period == "2025-12-31"
    assert snapshot.sections["Growth"]["Revenue growth"].availability is Availability.NOT_AVAILABLE


def test_latest_amendment_wins_and_missing_current_concept_stays_unavailable() -> None:
    original = monetary_taxonomy(
        "us-gaap", "USD", end="2025-12-31", filed="2026-02-01",
        form="10-K", fy=2025, accession="original", value=100,
    )
    amended = monetary_taxonomy(
        "us-gaap", "USD", end="2025-12-31", filed="2026-03-01",
        form="10-K/A", fy=2025, accession="amended", value=200, include_revenue=False,
    )
    merged = dict(original)
    for concept, node in amended.items():
        merged.setdefault(concept, {"units": {}})["units"].setdefault("USD", []).extend(node["units"]["USD"])
    snapshot = ResearchService(FixtureClient({"us-gaap": merged})).load("TEST")
    assert snapshot.sections["Overview"]["Revenue"].availability is Availability.NOT_AVAILABLE
    assert snapshot.sections["Overview"]["Net income"].value == Decimal(201)
    assert "10-K/A" in snapshot.warnings[0]


@pytest.mark.parametrize(("canonical", "dash"), [
    ("BRK.B", "BRK-B"),
    ("BRK.A", "BRK-A"),
    ("BF.B", "BF-B"),
    ("BF.A", "BF-A"),
    ("MOG.A", "MOG-A"),
    ("AGM.A", "AGM-A"),
])
def test_dot_share_class_requires_and_uses_official_provider_crosswalk(canonical: str, dash: str) -> None:
    decision = derive_yahoo_provider_symbol(
        canonical, ((canonical, "official_directory_symbol"), (dash, "official_source_symbol_variant")),
    )
    assert decision.supported
    assert decision.provider_symbol == dash
    assert decision.reason == "official_source_dot_dash_crosswalk"


def test_unverified_dot_instrument_is_truthfully_unsupported() -> None:
    decision = derive_yahoo_provider_symbol(
        "GCTS.W", (("GCTS.W", "official_directory_symbol"),),
    )
    assert not decision.supported
    assert decision.provider_symbol is None
    assert decision.reason == "unverified_dot_provider_identity"



def test_all_155_dot_symbols_are_crosswalked_or_truthfully_unsupported(r13_database: Path) -> None:
    with sqlite3.connect(r13_database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT i.instrument_id,i.canonical_symbol,i.primary_venue,
                      p.provider_symbol,p.mapping_status,
                      s.support_status,s.reason,s.mapping_source
               FROM rs_instruments i
               LEFT JOIN rs_provider_symbols p ON p.instrument_id=i.instrument_id
                 AND p.provider_id='yahoo' AND p.is_active=1
               LEFT JOIN rs_provider_instrument_support s ON s.instrument_id=i.instrument_id
                 AND s.provider_id='yahoo' AND s.capability='quote'
               WHERE i.is_active=1 AND i.canonical_symbol LIKE '%.%'
               ORDER BY i.canonical_symbol"""
        ).fetchall()
        aliases = {
            int(row["instrument_id"]): {str(value[0]).upper() for value in connection.execute(
                "SELECT alias_symbol FROM rs_instrument_aliases WHERE instrument_id=?",
                (int(row["instrument_id"]),),
            )}
            for row in rows
        }
    assert len(rows) == 155
    crosswalked = 0
    for row in rows:
        expected_dash = str(row["canonical_symbol"]).replace(".", "-")
        if expected_dash in aliases[int(row["instrument_id"])]:
            crosswalked += 1
            assert row["provider_symbol"] == expected_dash
            assert row["support_status"] == "supported"
            assert row["reason"] == "official_source_dot_dash_crosswalk"
        else:
            assert row["provider_symbol"] is None
            assert row["support_status"] == "unsupported"
            assert row["reason"] == "unverified_dot_provider_identity"
    assert crosswalked == 24

def test_canonical_ticker_identity_is_not_punctuation_normalized() -> None:
    dotted = derive_yahoo_provider_symbol("AB.C", (("AB-C", "official_source_symbol_variant"),))
    dashed = derive_yahoo_provider_symbol("AB-C", ())
    assert dotted.canonical_symbol == "AB.C" and dotted.provider_symbol == "AB-C"
    assert dashed.canonical_symbol == "AB-C" and dashed.provider_symbol == "AB-C"
