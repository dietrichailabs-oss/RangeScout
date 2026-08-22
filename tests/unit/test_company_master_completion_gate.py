from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import random
import sqlite3
from time import perf_counter

from app.company_data.master import company_master_path, provision_company_master
from app.historical_store.repository import HistoricalStore
from app.application.local_snapshot import LocalSnapshotRepository


EXPECTED_SOURCES = {
    "sec_company_tickers_exchange": "DC94346047679508512CC7384CAA89B5A5309D7AA80309E8BB1EB22E82C6BEDD",
    "nasdaq_trader_nasdaqlisted": "B2FD1D68CAAF80458A62C88C3A3020630E98675085E601E9AABBB820E6A7D240",
    "nasdaq_trader_otherlisted": "B56266057A944866F7F74E02189030E457A0BF4AD9DF79D3CF31185B43FA7E1A",
}


def _master_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(company_master_path())
    connection.row_factory = sqlite3.Row
    return connection


def test_frozen_sources_and_broad_master_are_exact_and_auditable() -> None:
    report_path = Path("docs/engineering/v1.6/COMPANY_MASTER_GENERATION_REPORT.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["final_unique_instrument_count"] == 16_377
    assert report["master_sha256"] == sha256(company_master_path().read_bytes()).hexdigest().upper()
    assert report["integrity_check"] == "ok"
    assert report["foreign_key_violations"] == 0
    assert {item["source_id"]: item["sha256"] for item in report["input_snapshots"]} == EXPECTED_SOURCES
    for item in report["input_snapshots"]:
        source_path = Path("docs/engineering/v1.6/company_master_sources") / item["filename"]
        assert source_path.is_file()
        assert sha256(source_path.read_bytes()).hexdigest().upper() == item["sha256"]


def test_master_has_broad_exchange_etf_and_outside_legacy_seed_coverage() -> None:
    with _master_connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM seed_instruments").fetchone()[0]
        venues = dict(connection.execute("SELECT primary_venue,COUNT(*) FROM seed_instruments GROUP BY primary_venue"))
        etfs = connection.execute("SELECT COUNT(*) FROM seed_instruments WHERE asset_class='etf'").fetchone()[0]
        present = {
            row[0] for row in connection.execute(
                "SELECT canonical_symbol FROM seed_instruments WHERE canonical_symbol IN ('AAPL','BA','NVDA','GOOGL','JPM','IBM','KO','XOM')"
            )
        }
    assert count == 16_377
    assert etfs >= 5_000
    assert {"NASDAQ", "NYSE", "NYSE Arca", "OTC"}.issubset(venues)
    assert present == {"AAPL", "BA", "NVDA", "GOOGL", "JPM", "IBM", "KO", "XOM"}


def test_clean_install_provisions_random_offline_sample_under_one_second(tmp_path: Path) -> None:
    target = tmp_path / "RangeScout.sqlite"
    store = HistoricalStore(target)
    store.close()
    began = perf_counter()
    report = provision_company_master(target)
    elapsed = perf_counter() - began
    assert report.available == 16_377 and report.added == 16_377
    assert elapsed < 1.0
    with _master_connection() as connection:
        symbols = [row[0] for row in connection.execute("SELECT canonical_symbol FROM seed_instruments ORDER BY canonical_symbol")]
    sample = random.Random(1600).sample(symbols, 100)
    local = LocalSnapshotRepository(target)
    for symbol in sample:
        snapshot = local.load(symbol)
        assert snapshot.identity.symbol == symbol
        assert snapshot.elapsed_ms < 250


def test_upgrade_is_additive_idempotent_and_retains_source_provenance(tmp_path: Path) -> None:
    target = tmp_path / "RangeScout.sqlite"
    store = HistoricalStore(target)
    store.close()
    first = provision_company_master(target)
    assert not first.already_current
    with sqlite3.connect(target) as connection:
        connection.execute("UPDATE rs_instruments SET security_name='User Curated Apple' WHERE canonical_symbol='AAPL'")
        connection.execute("UPDATE rs_schema_meta SET value='1' WHERE key='company_master_seed_version'")
        connection.commit()
    second = provision_company_master(target)
    third = provision_company_master(target)
    assert not second.already_current and third.already_current
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT security_name FROM rs_instruments WHERE canonical_symbol='AAPL'").fetchone()[0] == "User Curated Apple"
        assert connection.execute("SELECT COUNT(*) FROM rs_instrument_reference_sources").fetchone()[0] >= 20_000
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
