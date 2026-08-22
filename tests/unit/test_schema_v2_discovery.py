from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from app.historical_store.migrations import CURRENT_SCHEMA_VERSION, apply_migrations
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import AssetClass
from app.market_data.discovery import DiscoveryScheduler, InstrumentDiscovery, parse_nasdaq_directory
from app.market_data.instruments import (
    CryptoProduct,
    DiscoveredInstrument,
    OptionContract,
    parse_futures_symbol,
    select_continuous_contract,
)


NOW = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)


def item(symbol: str, name: str, venue: str = "Q", asset=AssetClass.EQUITY, security_type="Common Stock"):
    return DiscoveredInstrument(symbol, name, asset, security_type, venue, provider_symbol=symbol)


def test_populated_legacy_database_upgrades_additively_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "history.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES('schema_version','1');
        CREATE TABLE instruments(symbol TEXT,exchange TEXT,provider TEXT,currency TEXT,PRIMARY KEY(symbol,provider));
        CREATE TABLE ohlcv_bars(symbol TEXT,provider TEXT,bar_date TEXT,open REAL,high REAL,low REAL,close REAL,
          volume INTEGER,adjusted INTEGER,exchange TEXT,source TEXT,provider_timestamp TEXT,source_timezone TEXT,
          PRIMARY KEY(symbol,provider,bar_date));
        INSERT INTO instruments VALUES('AAPL','NASDAQ','yahoo','USD');
        INSERT INTO ohlcv_bars VALUES('AAPL','yahoo','2026-08-17',1,2,1,2,10,0,'NASDAQ','fixture',NULL,'UTC');"""
    )
    connection.commit()
    connection.close()
    store = HistoricalStore(path)
    assert store._con.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 1
    assert store._con.execute("SELECT COUNT(*) FROM ohlcv_bars").fetchone()[0] == 1
    assert store._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
    assert store.database_checks() == {"integrity_check": "ok", "foreign_key_violations": []}
    apply_migrations(store._con, CURRENT_SCHEMA_VERSION)
    assert store.database_checks()["integrity_check"] == "ok"
    store.close()


def test_failed_migration_rolls_back_without_half_updated_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES('schema_version','1')")
    connection.commit()
    connection.execute("CREATE TABLE rs_instruments(collision TEXT)")
    connection.commit()
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(connection, 1)
    assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='rs_schema_meta'").fetchone() is None


def migrated_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES('schema_version','1')")
    connection.commit()
    apply_migrations(connection, 1)
    return connection


def test_discovery_initial_identical_add_remove_rename_and_alias_preservation() -> None:
    connection = migrated_connection()
    discovery = InstrumentDiscovery(connection)
    first = discovery.import_snapshot("nasdaq", "Nasdaq", "https://example.invalid", [item("AAA", "Alpha"), item("BBB", "Beta")], b"v1", now=NOW)
    assert (first.before_count, first.after_count, first.added) == (0, 2, 2)
    second = discovery.import_snapshot("nasdaq", "Nasdaq", "https://example.invalid", [item("AAA", "Alpha"), item("BBB", "Beta")], b"v1", now=NOW + timedelta(days=1))
    assert (second.added, second.removed_inactive, second.changed) == (0, 0, 0)
    third = discovery.import_snapshot("nasdaq", "Nasdaq", "https://example.invalid", [item("AAC", "Alpha"), item("CCC", "Gamma")], b"v2", now=NOW + timedelta(days=8))
    assert (third.added, third.removed_inactive, third.changed) == (1, 1, 1)
    aliases = {row[0] for row in connection.execute("SELECT alias_symbol FROM rs_instrument_aliases")}
    assert {"AAA", "AAC", "BBB", "CCC"} <= aliases
    assert connection.execute("SELECT is_active FROM rs_instruments WHERE canonical_symbol='BBB'").fetchone()[0] == 0


def test_discovery_transaction_rollback_and_due_schedule() -> None:
    connection = migrated_connection()
    discovery = InstrumentDiscovery(connection)
    assert discovery.is_due("nasdaq", NOW)
    discovery.import_snapshot("nasdaq", "Nasdaq", "https://example.invalid", [item("AAA", "Alpha")], b"v1", now=NOW)
    assert not discovery.is_due("nasdaq", NOW + timedelta(days=6))
    assert discovery.is_due("nasdaq", NOW + timedelta(days=7))
    with pytest.raises(RuntimeError):
        discovery.import_snapshot(
            "nasdaq", "Nasdaq", "https://example.invalid", [item("BBB", "Beta")], b"bad", now=NOW + timedelta(days=8),
            failpoint=lambda: (_ for _ in ()).throw(RuntimeError("stop")),
        )
    assert connection.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0] == 1


def test_discovery_scheduler_is_nonblocking() -> None:
    connection = migrated_connection()
    discovery = InstrumentDiscovery(connection)
    scheduler = DiscoveryScheduler(discovery)
    try:
        future = scheduler.refresh_nonblocking(
            lambda: discovery.import_snapshot("nasdaq", "Nasdaq", "https://example.invalid", [item("AAA", "Alpha")], b"v1", now=NOW)
        )
        assert future.result(timeout=2).added == 1
    finally:
        scheduler.shutdown()


def test_nasdaq_parser_classification_unicode_and_malformed_rows() -> None:
    text = "Symbol|Security Name|ETF|Exchange\nAAA|Acme Corp|N|Q\nFUND|Acme ETF|Y|Q\nBAD|missing\n"
    parsed, errors = parse_nasdaq_directory(text, "Q")
    assert [value.asset_class for value in parsed] == [AssetClass.EQUITY, AssetClass.ETF]
    assert errors == 1
    preferred, preferred_errors = parse_nasdaq_directory(
        "ACT Symbol|Security Name|ETF|Exchange\nABR$D|Arbor Realty Trust Preferred|N|N\n", "N"
    )
    assert preferred_errors == 0 and preferred[0].canonical_symbol == "ABR$D"
    with pytest.raises(ValueError, match="unsupported characters"):
        item("A\u0000B", "Unsafe")


def test_crypto_futures_and_options_ready_models() -> None:
    pair = CryptoProduct("xbt", "usd", "Kraken", provider_product_id="XXBTZUSD")
    assert pair.canonical_symbol == "XBT-USD"
    march = parse_futures_symbol("ESH27", "CME", date(2027, 3, 19))
    june = parse_futures_symbol("ESM27", "CME", date(2027, 6, 18))
    assert march.contract_month == 3 and march.contract_year == 2027
    assert select_continuous_contract([march, june], date(2027, 3, 10), roll_days=5) is march
    assert select_continuous_contract([march, june], date(2027, 3, 15), roll_days=5) is june
    assert OptionContract("aapl", "call", Decimal("200"), date(2027, 1, 15)).underlying == "AAPL"
