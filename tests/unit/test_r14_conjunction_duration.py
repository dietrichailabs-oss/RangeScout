from __future__ import annotations

from decimal import Decimal
from itertools import permutations
from pathlib import Path
import sqlite3

import pytest

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.company_data.search_normalization import (
    normalize_search_compact,
    normalize_search_text,
    optional_conjunction_variants,
)
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION
from app.historical_store.repository import HistoricalStore
from app.research.fundamentals import ResearchService, SecFactSelector
from app.research.models import Availability


def row(
    value: int,
    *,
    start: str | None,
    end: str,
    filed: str,
    form: str,
    accession: str,
    fy: int,
    fp: str,
    frame: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "val": value,
        "end": end,
        "filed": filed,
        "form": form,
        "accn": accession,
        "fy": fy,
        "fp": fp,
    }
    if start:
        result["start"] = start
    if frame:
        result["frame"] = frame
    return result


@pytest.fixture(scope="module")
def r14_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("r14-search") / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    return database


def test_optional_conjunction_is_source_derived_not_global_query_deletion() -> None:
    assert optional_conjunction_variants("A & B Holdings") == ("a b", "abholdings")
    assert optional_conjunction_variants("Research and Development") == ()
    assert normalize_search_text("A & B") == normalize_search_text("A and B")


@pytest.mark.parametrize(("query", "expected"), [
    ("S P Global", "SPGI"),
    ("SP Global", "SPGI"),
    ("H R Block", "HRB"),
    ("HR Block", "HRB"),
    ("Brown Brown", "BRO"),
    ("Procter Gamble", "PG"),
    ("Johnson Johnson", "JNJ"),
    ("AT T", "T"),
    ("Light Wonder", "LNWO"),
    ("Smith Nephew", "SNNUF"),
    ("Zion Oil Gas", "ZNOG"),
])
def test_qa_optional_conjunction_queries_rank_expected_first(
    r14_database: Path, query: str, expected: str,
) -> None:
    matches = InstrumentResolver(r14_database).search(query, 20)
    assert matches and matches[0].symbol == expected


def test_every_active_ampersand_name_has_canonical_and_omission_documents(r14_database: Path) -> None:
    with sqlite3.connect(r14_database) as connection:
        rows = connection.execute(
            "SELECT instrument_id,canonical_symbol,security_name FROM rs_instruments "
            "WHERE is_active=1 AND security_name LIKE '%&%' ORDER BY canonical_symbol"
        ).fetchall()
        assert len(rows) >= 846
        for instrument_id, _symbol, name in rows:
            documents = {value[0] for value in connection.execute(
                "SELECT normalized_text FROM rs_instrument_search_fts WHERE instrument_id=?", (instrument_id,)
            )}
            assert normalize_search_text(name) in documents
            assert normalize_search_compact(name) in documents
            assert set(optional_conjunction_variants(name)).issubset(documents)


def select_revenue(rows: list[dict[str, object]], *, mode: str, taxonomy: str = "us-gaap"):
    forms = SecFactSelector.ANNUAL_FORMS if mode == "annual" else SecFactSelector.QUARTERLY_FORMS
    return SecFactSelector().select(
        {"Revenue": {"units": {"USD": rows}}},
        ("Revenue",),
        ("USD",),
        forms=forms,
        metric_type="duration",
        period_mode=mode,
        taxonomy=taxonomy,
    )


def test_annual_q4_selection_is_correct_for_every_row_permutation() -> None:
    annual = row(1200, start="2025-01-01", end="2025-12-31", filed="2026-02-20", form="10-K", accession="annual", fy=2025, fp="FY", frame="CY2025")
    q4 = row(300, start="2025-10-01", end="2025-12-31", filed="2026-02-20", form="10-K", accession="annual", fy=2025, fp="FY", frame="CY2025Q4")
    results = [select_revenue(list(order), mode="annual") for order in permutations((annual, q4))]
    assert {item.value for item in results} == {Decimal(1200)}
    assert {item.duration_days for item in results} == {365}
    assert {item.period_semantics for item in results} == {"annual"}


def test_quarter_ytd_selection_is_correct_for_every_row_permutation() -> None:
    quarter = row(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="q3", fy=2025, fp="Q3", frame="CY2025Q3")
    ytd = row(1100, start="2025-01-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="q3", fy=2025, fp="Q3", frame="CY2025Q3YTD")
    results = [select_revenue(list(order), mode="quarterly") for order in permutations((quarter, ytd))]
    assert {item.value for item in results} == {Decimal(400)}
    assert {item.duration_days for item in results} == {92}
    assert {item.period_semantics for item in results} == {"quarterly"}


def test_ytd_is_not_silently_presented_as_quarter() -> None:
    ytd = row(1100, start="2025-01-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="q3", fy=2025, fp="Q3", frame="CY2025Q3YTD")
    selected = select_revenue([ytd], mode="quarterly")
    assert selected.availability is Availability.NOT_AVAILABLE
    assert "YTD" in selected.selection_reason


@pytest.mark.parametrize(("start", "end", "expected_days"), [
    ("2024-01-01", "2024-12-31", 366),
    ("2025-01-05", "2026-01-03", 364),
    ("2025-01-05", "2026-01-10", 371),
])
def test_annual_duration_supports_leap_52_and_53_week_years(start: str, end: str, expected_days: int) -> None:
    selected = select_revenue([
        row(1200, start=start, end=end, filed="2026-02-20", form="10-K", accession="annual", fy=2025, fp="FY")
    ], mode="annual")
    assert selected.value == Decimal(1200)
    assert selected.duration_days == expected_days


def test_explicit_annual_frame_supports_transition_period() -> None:
    selected = select_revenue([
        row(500, start="2025-07-01", end="2025-12-31", filed="2026-02-20", form="10-K", accession="transition", fy=2025, fp="FY", frame="CY2025")
    ], mode="annual")
    assert selected.value == Decimal(500)
    assert selected.period_semantics == "annual_transition"


def test_ifrs_annual_and_amendment_are_order_independent() -> None:
    original = row(900, start="2025-01-01", end="2025-12-31", filed="2026-03-01", form="20-F", accession="original", fy=2025, fp="FY", frame="CY2025")
    amended = row(950, start="2025-01-01", end="2025-12-31", filed="2026-04-01", form="20-F/A", accession="amended", fy=2025, fp="FY", frame="CY2025")
    results = [select_revenue(list(order), mode="annual", taxonomy="ifrs-full") for order in permutations((original, amended))]
    assert {item.value for item in results} == {Decimal(950)}
    assert {item.taxonomy for item in results} == {"ifrs-full"}
    assert {item.form for item in results} == {"20-F/A"}


class FixtureClient:
    def __init__(self, facts: dict[str, object]) -> None:
        self.facts = facts

    def company_map(self):
        return {"TEST": {"cik": "0000000001", "name": "Duration Test"}}

    def companyfacts(self, _cik):
        return {"entityName": "Duration Test", "facts": self.facts}

    def submissions(self, _cik):
        return {"exchanges": ["NYSE"], "sic": "1000", "sicDescription": "Test"}


def snapshot_for(rows: list[dict[str, object]], *, mode: str = "annual"):
    facts = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}}}}
    return ResearchService(FixtureClient(facts)).load("TEST", period_mode=mode)


def test_previous_annual_fact_requires_same_duration_semantics() -> None:
    current = row(1200, start="2025-01-01", end="2025-12-31", filed="2026-02-20", form="10-K", accession="current", fy=2025, fp="FY", frame="CY2025")
    prior_annual = row(1000, start="2024-01-01", end="2024-12-31", filed="2025-02-20", form="10-K", accession="prior", fy=2024, fp="FY", frame="CY2024")
    prior_q4 = row(250, start="2024-10-01", end="2024-12-31", filed="2025-02-20", form="10-K", accession="prior", fy=2024, fp="FY", frame="CY2024Q4")
    values = snapshot_for([current, prior_q4, prior_annual]).sections
    assert values["Growth"]["Revenue growth"].value == Decimal(20)
    assert values["Overview"]["Revenue"].value == Decimal(1200)


def test_previous_quarter_rejects_ytd_and_missing_comparable_is_unavailable() -> None:
    current = row(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="current", fy=2025, fp="Q3", frame="CY2025Q3")
    prior_ytd = row(900, start="2024-01-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="prior", fy=2024, fp="Q3", frame="CY2024Q3YTD")
    growth = snapshot_for([current, prior_ytd], mode="quarterly").sections["Growth"]["Revenue growth"]
    assert growth.availability is Availability.NOT_AVAILABLE


def test_taxonomy_transition_is_payload_order_independent() -> None:
    current_ifrs = row(
        600, start="2025-01-01", end="2025-12-31", filed="2026-04-01",
        form="20-F", accession="ifrs-current", fy=2025, fp="FY", frame="CY2025",
    )
    older_gaap = row(
        400, start="2024-01-01", end="2024-12-31", filed="2025-03-01",
        form="10-K", accession="gaap-old", fy=2024, fp="FY", frame="CY2024",
    )
    nodes = {
        "ifrs-full": {"Revenue": {"units": {"EUR": [current_ifrs]}}},
        "us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [older_gaap]}}},
    }
    payloads = [nodes, dict(reversed(tuple(nodes.items())))]
    selected = [ResearchService(FixtureClient(payload)).load("TEST").sections["Overview"]["Revenue"] for payload in payloads]
    assert {item.value for item in selected} == {Decimal(600)}
    assert {item.taxonomy for item in selected} == {"ifrs-full"}
    assert {item.units for item in selected} == {"EUR"}


def test_reporting_currency_transition_is_unit_and_row_order_independent() -> None:
    current = row(
        700, start="2025-01-01", end="2025-12-31", filed="2026-04-01",
        form="20-F", accession="twd-current", fy=2025, fp="FY", frame="CY2025",
    )
    prior_same_unit = row(
        650, start="2024-01-01", end="2024-12-31", filed="2025-04-01",
        form="20-F", accession="twd-prior", fy=2024, fp="FY", frame="CY2024",
    )
    older_usd = row(
        100, start="2024-01-01", end="2024-12-31", filed="2025-03-01",
        form="20-F", accession="usd-old", fy=2024, fp="FY", frame="CY2024",
    )
    units_a = {"USD": [older_usd], "TWD": [prior_same_unit, current]}
    units_b = {"TWD": [current, prior_same_unit], "USD": [older_usd]}
    payloads = [
        {"ifrs-full": {"Revenue": {"units": units_a}}},
        {"ifrs-full": {"Revenue": {"units": units_b}}},
    ]
    selected = [ResearchService(FixtureClient(payload)).load("TEST").sections["Overview"]["Revenue"] for payload in payloads]
    assert {item.value for item in selected} == {Decimal(700)}
    assert {item.taxonomy for item in selected} == {"ifrs-full"}
    assert {item.units for item in selected} == {"TWD"}


def test_schema_16_backfills_optional_documents_transactionally(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite"
    with HistoricalStore(database) as store:
        assert CURRENT_SCHEMA_VERSION == 16
        version = store._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        document_version = store._con.execute(
            "SELECT value FROM rs_schema_meta WHERE key='instrument_search_document_version'"
        ).fetchone()[0]
    assert version == "16"
    assert document_version == "2"
