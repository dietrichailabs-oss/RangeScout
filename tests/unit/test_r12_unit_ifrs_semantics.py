from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.company_data.instrument_intelligence import (
    InstrumentReferenceSeeder,
    InstrumentResolver,
    classify_unit_semantics,
)
from app.company_data.master import provision_company_master
from app.historical_store.migrations import apply_migrations
import sqlite3
from app.research.fundamentals import ResearchService
from app.research.models import Availability
from app.research.routing import ResearchRoute, plan_research


@pytest.mark.parametrize(("name", "issuer", "role"), [
    ("AllianceBernstein Holding L.P. Units", "operating_partnership", "primary_common"),
    ("Brookfield Renewable Partners L.P. Limited Partnership Units", "operating_partnership", "primary_common"),
    ("Brookfield Renewable Partners L.P. 5.25% Class A Preferred Limited Partnership Units, Series 17", "operating_partnership", "preferred_security"),
    ("Brookfield Infrastructure Partners LP Limited Partnership Units", "operating_partnership", "primary_common"),
    ("Brookfield Infrastructure Partners LP 5.125% Class A Preferred Limited Partnership Units, Series 13", "operating_partnership", "preferred_security"),
    ("Brookfield Property Partners L.P. 6.25% Class A Cumulative Redeemable Preferred Units, Series 1", "operating_partnership", "preferred_security"),
    ("Dynagas LNG Partners LP 9.00% Series A Cumulative Redeemable Preferred Units", "operating_partnership", "preferred_security"),
    ("Energy Transfer L.P. Series I Fixed Rate Perpetual Preferred Units", "operating_partnership", "preferred_security"),
    ("GasLog Partners LP 8.625% Series A Cumulative Redeemable Perpetual Preference Units", "operating_partnership", "preferred_security"),
    ("Belpointe PREP, LLC Class A Units", "operating_company", "primary_common"),
    ("SunocoCorp LLC Common Units, representing limited liability company interests", "operating_company", "primary_common"),
    ("Seapeak LLC 9.00% Series A Cumulative Redeemable Perpetual Preferred Units", "operating_company", "preferred_security"),
])
def test_generalized_legal_unit_semantics(name: str, issuer: str, role: str) -> None:
    actual_issuer, actual_role, reason = classify_unit_semantics(name)
    assert (actual_issuer, actual_role) == (issuer, role)
    assert reason
    plan = plan_research("unit", "unit", actual_issuer, actual_role)
    assert plan.route is ResearchRoute.CORPORATE
    assert plan.sec_applicable


@pytest.mark.parametrize(("name", "issuer", "role", "route"), [
    ("Acquisition Corp Units, each consisting of one share and one warrant", "unknown", "alternate_security", ResearchRoute.MARKET_INSTRUMENT),
    ("Example Statutory Trust Units of Beneficial Interest", "fund_vehicle", "fund", ResearchRoute.FUND),
])
def test_unit_exclusions_precede_legal_issuer_heuristics(name, issuer, role, route) -> None:
    actual_issuer, actual_role, _reason = classify_unit_semantics(name)
    assert (actual_issuer, actual_role) == (issuer, role)
    assert plan_research("unit", "unit", actual_issuer, actual_role).route is route


def test_corrected_unit_semantics_feed_search_without_false_family_resolution(tmp_path: Path) -> None:
    database = tmp_path / "r12-search.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES('schema_version','1')")
    connection.commit()
    apply_migrations(connection, 1)
    connection.close()
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    resolver = InstrumentResolver(database)
    assert resolver.search("AllianceBernstein Holding")[0].symbol == "AB"
    assert resolver.search("Brookfield Infrastructure Partners")[0].symbol == "BIP"
    assert resolver.search("Brookfield Renewable Partners")[0].symbol == "BEP"
    assert resolver.resolve_unique("Brookfield Infrastructure") is None
    assert resolver.resolve_unique("Brookfield Renewable") is None
    assert resolver.resolve_unique("Intel").symbol == "INTC"
    assert resolver.resolve_unique("JPMorgan").symbol == "JPM"
    assert resolver.resolve_unique("Coca Cola").symbol == "KO"
    assert resolver.resolve_unique("Coca-Cola").symbol == "KO"


def fact(value: int, *, end: str, filed: str, form: str = "20-F", fy: int = 2025) -> dict[str, object]:
    return {"val": value, "end": end, "filed": filed, "form": form, "accn": f"{fy}-1", "fy": fy, "fp": "FY"}


class FixtureClient:
    def __init__(self, facts: dict[str, object], *, symbol: str = "TSM", cik: str = "0001046179") -> None:
        self.facts = facts
        self.symbol = symbol
        self.cik = cik

    def company_map(self):
        return {self.symbol: {"cik": self.cik, "name": "Foreign Reporting Issuer"}}

    def companyfacts(self, _cik):
        return {"entityName": "Foreign Reporting Issuer", "facts": self.facts}

    def submissions(self, _cik):
        return {"exchanges": ["NYSE"], "sic": "3674", "sicDescription": "Semiconductors"}


def ifrs_facts(currency: str = "TWD", *, include_equity: bool = True) -> dict[str, object]:
    current = dict(end="2025-12-31", filed="2026-04-15", form="20-F", fy=2025)
    previous = dict(end="2024-12-31", filed="2025-04-15", form="20-F", fy=2024)
    facts = {
        "Revenue": {"units": {currency: [fact(120, **current), fact(100, **previous)]}},
        "ProfitLoss": {"units": {currency: [fact(24, **current), fact(20, **previous)]}},
        "Assets": {"units": {currency: [fact(900, **current)]}},
    }
    if include_equity:
        facts["Equity"] = {"units": {currency: [fact(500, **current)]}}
    return facts


def test_tsm_style_annual_ifrs_20f_preserves_twd_and_populates_core_metrics() -> None:
    snapshot = ResearchService(FixtureClient({"ifrs-full": ifrs_facts()})).load("TSM", generation=12)
    for metric in ("Revenue", "Net income", "Assets", "Equity"):
        value = snapshot.sections["Overview"][metric]
        assert value.availability is Availability.AVAILABLE
        assert value.units == "TWD"
        assert "ifrs-full" in value.source
    assert snapshot.sections["Growth"]["Revenue growth"].value == Decimal(20)
    assert "no currency conversion" in snapshot.warnings[0]


def test_foreign_partnership_unit_uses_issuer_cik_and_ifrs_cad() -> None:
    issuer, role, _reason = classify_unit_semantics("Brookfield Infrastructure Partners LP Limited Partnership Units")
    assert plan_research("unit", "unit", issuer, role).sec_applicable
    snapshot = ResearchService(FixtureClient({"ifrs-full": ifrs_facts("CAD")}, symbol="OTHER", cik="0001406234")).load(
        "BIP", generation=3, cik="0001406234",
    )
    assert snapshot.profile.cik == "0001406234"
    assert snapshot.sections["Overview"]["Revenue"].units == "CAD"


def test_mixed_taxonomy_selects_one_native_filing_taxonomy_without_currency_mixing() -> None:
    us = {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
        fact(999, end="2025-12-31", filed="2026-02-01", form="10-K", fy=2025),
    ]}}}
    snapshot = ResearchService(FixtureClient({"us-gaap": us, "ifrs-full": ifrs_facts("TWD")})).load("TSM")
    revenue = snapshot.sections["Overview"]["Revenue"]
    assert revenue.value == Decimal(120)
    assert revenue.units == "TWD"
    assert "ifrs-full" in revenue.source


def test_missing_ifrs_concept_remains_unavailable() -> None:
    snapshot = ResearchService(FixtureClient({"ifrs-full": ifrs_facts(include_equity=False)})).load("TSM")
    equity = snapshot.sections["Overview"]["Equity"]
    assert equity.availability is Availability.NOT_AVAILABLE
    assert equity.value is None


def test_previous_period_never_crosses_reporting_currency() -> None:
    facts = ifrs_facts("TWD")
    facts["Revenue"]["units"]["TWD"] = [fact(120, end="2025-12-31", filed="2026-04-15")]
    facts["Revenue"]["units"]["USD"] = [fact(100, end="2024-12-31", filed="2025-04-15", fy=2024)]
    snapshot = ResearchService(FixtureClient({"ifrs-full": facts})).load("TSM")
    assert snapshot.sections["Overview"]["Revenue"].units == "TWD"
    assert snapshot.sections["Growth"]["Revenue growth"].availability is Availability.NOT_AVAILABLE
