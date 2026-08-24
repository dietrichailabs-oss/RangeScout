from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from app.application.active_symbol import ActiveSymbolController
from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION, apply_migrations
from app.historical_store.repository import HistoricalStore
from app.research.routing import ResearchRoute, plan_research


COMMON_CEF_SYMBOLS = ("DFP", "FFC", "HPS", "JPC", "LDP", "NPFD", "PFD", "PFO", "PSF", "RNP", "BOE", "PDI")
CEF_PREFERRED_SYMBOLS = ("CCID", "ECCC", "EICA", "EIIA", "OCCIM", "OCCIN", "OXLCM", "OXLCN", "OXLCO", "PDPA")


@pytest.fixture(scope="module")
def r8_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("r8-semantic") / "history.sqlite"
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    return path


def test_authoritative_cef_resource_is_independent_cik_evidence() -> None:
    payload = json.loads(Path("resources/RangeScout_Instrument_Classifications.json").read_text(encoding="utf-8"))
    assert len(payload["classifications"]) == 355
    assert all(row["asset_class"] == "closed_end_fund" for row in payload["classifications"])
    assert all(row["cik"] and "symbol" not in row for row in payload["classifications"])


@pytest.mark.parametrize("symbol", COMMON_CEF_SYMBOLS)
def test_primary_cef_shares_use_fund_security_and_issuer_semantics(r8_database: Path, symbol: str) -> None:
    match = InstrumentResolver(r8_database).resolve_unique(symbol)
    assert match is not None and match.symbol == symbol
    instrument = match.instrument
    assert instrument.asset_class == "closed_end_fund"
    assert instrument.issuer_type == "closed_end_fund"
    assert instrument.security_role == "primary_common"
    plan = plan_research(instrument.asset_class, instrument.subtype, instrument.issuer_type, instrument.security_role)
    assert plan.route is ResearchRoute.FUND
    assert not plan.analyst_applicable
    assert "Earnings" not in plan.visible_sections


@pytest.mark.parametrize("symbol", CEF_PREFERRED_SYMBOLS)
def test_cef_issued_preferred_keeps_security_identity_and_fund_research(r8_database: Path, symbol: str) -> None:
    match = InstrumentResolver(r8_database).resolve_unique(symbol)
    assert match is not None and match.symbol == symbol
    instrument = match.instrument
    assert instrument.asset_class == "preferred"
    assert instrument.issuer_type == "closed_end_fund"
    assert instrument.security_role == "preferred_security"
    plan = plan_research(instrument.asset_class, instrument.subtype, instrument.issuer_type, instrument.security_role)
    assert plan.route is ResearchRoute.FUND
    assert not plan.analyst_applicable
    assert "ordinary-company" in plan.message.lower()
    assert match.instrument.symbol == symbol


def test_all_authoritative_cef_rows_have_issuer_context_and_semantic_research(r8_database: Path) -> None:
    payload = json.loads(Path("resources/RangeScout_Instrument_Classifications.json").read_text(encoding="utf-8"))
    ciks = {str(row["cik"]).zfill(10) for row in payload["classifications"]}
    placeholders = ",".join("?" for _ in ciks)
    with sqlite3.connect(r8_database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""SELECT canonical_symbol,asset_class,instrument_subtype,issuer_entity_type,security_role
                FROM rs_instruments WHERE is_active=1 AND cik IN ({placeholders}) ORDER BY canonical_symbol""",
            tuple(sorted(ciks)),
        ).fetchall()
    assert rows
    failures = []
    for row in rows:
        plan = plan_research(row["asset_class"], row["instrument_subtype"], row["issuer_entity_type"], row["security_role"])
        if row["issuer_entity_type"] != "closed_end_fund" or plan.route is not ResearchRoute.FUND:
            failures.append(dict(row))
        if row["security_role"] == "primary_common" and row["asset_class"] != "closed_end_fund":
            failures.append(dict(row))
        if row["security_role"] == "preferred_security" and row["asset_class"] != "preferred":
            failures.append(dict(row))
    assert failures == []


def test_active_symbol_request_preserves_both_semantic_dimensions(r8_database: Path) -> None:
    instrument = InstrumentResolver(r8_database).resolve_unique("CCID").instrument
    controller = ActiveSymbolController("AAPL")
    state = controller.set(
        instrument.symbol, source="semantic-test", instrument_id=instrument.instrument_id,
        asset_class=instrument.asset_class, subtype=instrument.subtype,
        issuer_type=instrument.issuer_type, security_role=instrument.security_role,
    )
    request = controller.request()
    assert (state.issuer_type, state.security_role) == ("closed_end_fund", "preferred_security")
    assert (request.issuer_type, request.security_role) == ("closed_end_fund", "preferred_security")


def test_schema_v11_is_additive_and_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
           INSERT INTO meta VALUES('schema_version','10');
           CREATE TABLE rs_instruments(
             instrument_id INTEGER PRIMARY KEY,asset_class TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1
           );
           INSERT INTO rs_instruments VALUES(1,'preferred',1);"""
    )
    apply_migrations(connection, 10)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rs_instruments)")}
    assert CURRENT_SCHEMA_VERSION == 12
    assert {"issuer_entity_type", "security_role"}.issubset(columns)
    assert connection.execute(
        "SELECT issuer_entity_type,security_role FROM rs_instruments WHERE instrument_id=1"
    ).fetchone() == ("operating_company", "alternate_security")
    apply_migrations(connection, CURRENT_SCHEMA_VERSION)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_schema_v11_recovery_is_idempotent_when_columns_already_exist() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
           INSERT INTO meta VALUES('schema_version','10');
           CREATE TABLE rs_instruments(
             instrument_id INTEGER PRIMARY KEY,asset_class TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,
             issuer_entity_type TEXT NOT NULL DEFAULT 'unknown',
             security_role TEXT NOT NULL DEFAULT 'unknown'
           );
           INSERT INTO rs_instruments(instrument_id,asset_class) VALUES(1,'closed_end_fund');"""
    )
    apply_migrations(connection, 10)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rs_instruments)")}
    assert {"issuer_entity_type", "security_role"}.issubset(columns)
    assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "12"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
