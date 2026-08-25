from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION, apply_migrations
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import AssetClass
from app.market_data.discovery import DiscoveryCoordinator, InstrumentDiscovery, classify_nasdaq_row, parse_nasdaq_directory


SOURCE_DIR = Path("docs/engineering/v1.6/company_master_sources")
NOW = datetime(2026, 8, 24, 20, tzinfo=timezone.utc)


@pytest.mark.parametrize("symbol,name,flag,expected", [
    ("PANW", "Palo Alto Networks, Inc. - Common Stock", "N", AssetClass.EQUITY),
    ("ACU", "Acme United Corporation. Common Stock", "N", AssetClass.EQUITY),
    ("PFBC", "Preferred Bank - Common Stock", "N", AssetClass.EQUITY),
    ("AMJB", "Alerian MLP Index ETN", "Y", AssetClass.ETN),
    ("BAVA", "Common Shares of Beneficial Interest of Bitwise Avalanche ETF", "Y", AssetClass.ETF),
    ("AACIW", "Armada Acquisition Corp. II - Warrant", "N", AssetClass.WARRANT),
    ("OTAI.U", "Starlink AI Acquisition Corporation Units, each consisting of one Ordinary Share, and one Right", "N", AssetClass.UNIT),
    ("BSBR", "Banco Santander Brasil SA American Depositary Shares, each representing one unit", "N", AssetClass.ADR),
])
def test_shared_classifier_uses_security_evidence(symbol, name, flag, expected) -> None:
    asset, _security_type = classify_nasdaq_row({"Symbol": symbol, "Security Name": name, "ETF": flag})
    assert asset is expected


def test_parser_normalizes_venues_and_excludes_official_test_issues() -> None:
    text = (
        "ACT Symbol|Security Name|Exchange|ETF|Test Issue\n"
        "AAPL|Apple Inc. Common Stock|Q|N|N\n"
        "ATEST|Tick Pilot Test Control Common Stock|N|N|Y\n"
    )
    rows, errors = parse_nasdaq_directory(text, "N")
    assert errors == 0
    assert [(row.canonical_symbol, row.primary_venue) for row in rows] == [("AAPL", "NASDAQ")]


@pytest.fixture(scope="module")
def refreshed_database(tmp_path_factory: pytest.TempPathFactory):
    path = tmp_path_factory.mktemp("r9-refresh") / "history.sqlite"
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    source_a = (SOURCE_DIR / "nasdaq_nasdaqlisted.txt").read_text(encoding="utf-8-sig", errors="replace")
    source_b = (SOURCE_DIR / "nasdaq_otherlisted.txt").read_text(encoding="utf-8-sig", errors="replace")
    rows_a, errors_a = parse_nasdaq_directory(source_a, "Q")
    rows_b, errors_b = parse_nasdaq_directory(source_b, "N")
    snapshot = rows_a + rows_b
    raw = (source_a + "\n" + source_b).encode("utf-8")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        ids_before = {row[0]: row[1] for row in connection.execute(
            "SELECT canonical_symbol,instrument_id FROM rs_instruments WHERE is_active=1"
        )}
        first = InstrumentDiscovery(connection).import_snapshot(
            DiscoveryCoordinator.SOURCE_ID, DiscoveryCoordinator.DISPLAY_NAME,
            DiscoveryCoordinator.OFFICIAL_URL, snapshot, raw, parse_errors=errors_a + errors_b, now=NOW,
        )
        ids_after_first = {row[0]: row[1] for row in connection.execute(
            "SELECT canonical_symbol,instrument_id FROM rs_instruments WHERE is_active=1"
        )}
        duplicate_first = connection.execute(
            "SELECT COUNT(*) FROM (SELECT canonical_symbol FROM rs_instruments WHERE is_active=1 GROUP BY canonical_symbol HAVING COUNT(*)>1)"
        ).fetchone()[0]
        second = InstrumentDiscovery(connection).import_snapshot(
            DiscoveryCoordinator.SOURCE_ID, DiscoveryCoordinator.DISPLAY_NAME,
            DiscoveryCoordinator.OFFICIAL_URL, snapshot, raw, parse_errors=errors_a + errors_b, now=NOW,
        )
        ids_after_second = {row[0]: row[1] for row in connection.execute(
            "SELECT canonical_symbol,instrument_id FROM rs_instruments WHERE is_active=1"
        )}
        test_issues = connection.execute(
            "SELECT COUNT(*) FROM rs_instruments WHERE is_active=1 AND canonical_symbol IN ('ZAZZT','ZBZZT','ATEST','ATEST.A')"
        ).fetchone()[0]
    return path, snapshot, first, second, ids_before, ids_after_first, ids_after_second, duplicate_first, test_issues


def test_first_and_second_production_refresh_are_canonical_and_idempotent(refreshed_database) -> None:
    path, snapshot, first, second, ids_before, ids_after_first, ids_after_second, duplicates, test_issues = refreshed_database
    assert len(ids_before) == 16382
    assert len(snapshot) == 13136
    assert first.added == 0 and first.after_count == 13136
    assert len(ids_after_first) == 16382 and duplicates == 0 and test_issues == 0
    assert ids_after_first == ids_before
    assert second.added == 0 and second.changed == 0 and second.removed_inactive == 0
    assert ids_after_second == ids_after_first
    resolver = InstrumentResolver(path)
    assert all(resolver.resolve_unique(symbol) is not None for symbol in ("AAPL", "MSFT", "BOE", "PDI"))
    assert {item.symbol for item in resolver.search("DOW", 10)} >= {"DOW", "^DJI"}
    assert {item.symbol for item in resolver.search("GOLD", 10)} >= {"GOLD", "XAU/USD"}


def test_schema_v12_repairs_only_exact_r8_clone_and_preserves_references(tmp_path) -> None:
    path = tmp_path / "polluted.sqlite"
    with HistoricalStore(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','11')")
        stamp = NOW.isoformat()
        master = connection.execute(
            """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,security_type,primary_venue,
               is_active,first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
               VALUES('AAPL','Apple Inc. Common Stock','equity','Common Stock','NASDAQ',1,?,?,?,?)""",
            (stamp, stamp, stamp, stamp),
        ).lastrowid
        clone = connection.execute(
            """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,security_type,primary_venue,
               is_active,first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
               VALUES('AAPL','Apple Incorporated - Common Stock','equity','Common Stock','Q',1,?,?,?,?)""",
            (stamp, stamp, stamp, stamp),
        ).lastrowid
        for source in ("rangescout_public_master", DiscoveryCoordinator.SOURCE_ID):
            connection.execute(
                "INSERT OR IGNORE INTO rs_discovery_sources(source_id,display_name,source_kind,enabled,created_at_utc,updated_at_utc) VALUES(?,?,?,1,?,?)",
                (source, source, "fixture", stamp, stamp),
            )
        connection.execute(
            "INSERT INTO rs_instrument_reference_sources VALUES(?,?,?,?,?,?,?)",
            (master, "rangescout_public_master", "AAPL", "Apple Inc. Common Stock", "NASDAQ", "master", stamp),
        )
        connection.execute(
            "INSERT INTO rs_instrument_aliases(instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc) VALUES(?,?,?,?,?,?)",
            (clone, "AAPL", "Q", "source_symbol", DiscoveryCoordinator.SOURCE_ID, stamp),
        )
        connection.execute(
            "INSERT INTO rs_instrument_capabilities VALUES(?,?,?,?,?)",
            (clone, "quote", "applicable", "fixture", stamp),
        )
        connection.execute(
            """INSERT INTO rs_last_quotes(instrument_id,last_price,currency,provider_id,received_at_utc,delay_label)
               VALUES(?, '100', 'USD', 'yahoo', ?, 'Delayed')""", (clone, stamp),
        )
        connection.commit()
        apply_migrations(connection, 11)
        assert CURRENT_SCHEMA_VERSION == 14
        assert connection.execute("SELECT is_active FROM rs_instruments WHERE instrument_id=?", (clone,)).fetchone()[0] == 0
        assert connection.execute("SELECT survivor_instrument_id FROM rs_instrument_identity_merges WHERE old_instrument_id=?", (clone,)).fetchone()[0] == master
        assert connection.execute("SELECT instrument_id FROM rs_last_quotes").fetchone()[0] == master
        assert connection.execute("SELECT instrument_id FROM rs_instrument_capabilities").fetchone()[0] == master
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert InstrumentResolver(path).by_id(clone).instrument_id == master
