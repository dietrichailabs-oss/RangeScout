from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION
from app.historical_store.repository import HistoricalStore
from app.market_data.instruments import DiscoveredInstrument, normalize_symbol
from app.market_data.contracts import AssetClass
from app.market_data.provider_symbols import (
    derive_yahoo_provider_symbol,
    is_placeholder_symbol,
    normalize_yahoo_symbol,
)


@pytest.fixture(scope="module")
def r7_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("r7-provider-symbols") / "history.sqlite"
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    return path


def test_yahoo_series_crosswalk_requires_both_official_alias_forms() -> None:
    decision = derive_yahoo_provider_symbol(
        "ABR$D",
        (("ABR-D", "official_directory_symbol"), ("ABRPD", "official_source_symbol_variant")),
    )
    assert decision.supported and decision.provider_symbol == "ABR-PD"
    assert decision.canonical_symbol == "ABR$D"
    unsupported = derive_yahoo_provider_symbol(
        "ABR$D", (("ABR-D", "official_directory_symbol"),)
    )
    assert not unsupported.supported
    assert unsupported.reason == "missing_cross_source_series_alias_evidence"


def test_all_active_dollar_series_have_explicit_valid_yahoo_mappings(r7_database: Path) -> None:
    with sqlite3.connect(r7_database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT i.instrument_id,i.canonical_symbol,i.asset_class,p.provider_symbol,p.mapping_status
               FROM rs_instruments i LEFT JOIN rs_provider_symbols p
                 ON p.instrument_id=i.instrument_id AND p.provider_id='yahoo' AND p.is_active=1
               WHERE i.is_active=1 AND i.canonical_symbol LIKE '%$%'
               ORDER BY i.canonical_symbol"""
        ).fetchall()
        support_failures = connection.execute(
            """SELECT COUNT(*) FROM rs_provider_instrument_support s JOIN rs_instruments i
               ON i.instrument_id=s.instrument_id WHERE i.is_active=1 AND i.canonical_symbol LIKE '%$%'
               AND s.provider_id='yahoo' AND s.capability IN ('quote','historical')
               AND s.support_status!='supported'"""
        ).fetchone()[0]
    assert len(rows) == 384
    assert support_failures == 0
    for row in rows:
        assert row["provider_symbol"] and "$" not in row["provider_symbol"]
        assert normalize_yahoo_symbol(row["provider_symbol"]) == row["provider_symbol"]
        assert row["mapping_status"] == "derived_official_aliases"


@pytest.mark.parametrize(
    ("canonical", "expected"),
    (("ABR$D", "ABR-PD"), ("BAC$E", "BAC-PE"), ("BEP$A", "BEP-PA")),
)
def test_resolver_preserves_canonical_identity_with_provider_mapping(
    r7_database: Path, canonical: str, expected: str,
) -> None:
    match = InstrumentResolver(r7_database).resolve_unique(canonical)
    assert match is not None and match.symbol == canonical
    assert match.instrument.provider_symbols["yahoo"] == expected


@pytest.mark.parametrize("symbol", ["NONE.", "NONE", "N/A", "NULL", "NO-TICKER", "NOT_APPLICABLE"])
def test_placeholder_markers_are_rejected_generically(symbol: str) -> None:
    assert is_placeholder_symbol(symbol)
    with pytest.raises(ValueError):
        normalize_symbol(symbol)
    with pytest.raises(ValueError):
        DiscoveredInstrument(symbol, "Placeholder", AssetClass.EQUITY, "Common Stock", "NYSE")


def test_legitimate_na_ticker_is_not_a_placeholder() -> None:
    assert not is_placeholder_symbol("NA")


def test_existing_placeholder_is_deactivated_without_deleting_provenance(
    r7_database: Path, tmp_path: Path,
) -> None:
    migrated = tmp_path / "r6-history.sqlite"
    with sqlite3.connect(r7_database) as source, sqlite3.connect(migrated) as target:
        source.backup(target)
        target.execute(
            """INSERT INTO rs_instruments(
               canonical_symbol,security_name,asset_class,security_type,primary_venue,currency,is_active,
               first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc,metadata_source)
               VALUES('NONE.','Bogus source placeholder','stock','Listed Security','OTC','USD',1,
                      '2026-08-21T00:00:00+00:00','2026-08-21T00:00:00+00:00',
                      '2026-08-21T00:00:00+00:00','2026-08-21T00:00:00+00:00','r6_company_master')"""
        )
        target.execute(
            "UPDATE rs_schema_meta SET value='3' WHERE key='instrument_reference_version'"
        )
        target.commit()
    InstrumentReferenceSeeder(migrated).apply()
    with sqlite3.connect(migrated) as connection:
        row = connection.execute(
            "SELECT is_active,metadata_source FROM rs_instruments WHERE canonical_symbol='NONE.'"
        ).fetchone()
    assert row == (0, "source_placeholder_filtered")
    assert InstrumentResolver(migrated).search("NONE.") == []

@pytest.mark.parametrize("symbol", ["BCPC", "INNPF", "TPC"])
def test_provider_alias_cannot_make_an_exact_canonical_ticker_ambiguous(
    r7_database: Path, symbol: str,
) -> None:
    resolver = InstrumentResolver(r7_database)
    results = resolver.search(symbol, 10)
    assert results[0].symbol == symbol and results[0].match_kind == "exact_symbol"
    assert resolver.resolve_unique(symbol).symbol == symbol


@pytest.mark.parametrize(
    ("query", "expected"),
    (("GOLD", {"GOLD", "XAU/USD"}), ("DOW", {"DOW", "^DJI"}),
     ("DJIA", {"DJIA", "^DJI"}), ("BTC", {"BTC", "BTC/USD"})),
)
def test_legitimate_human_intent_collisions_remain_visible(
    r7_database: Path, query: str, expected: set[str],
) -> None:
    resolver = InstrumentResolver(r7_database)
    assert expected.issubset({item.symbol for item in resolver.search(query, 10)})
    assert resolver.resolve_unique(query) is None


def test_schema_v10_is_additive_and_idempotent(r7_database: Path) -> None:
    with HistoricalStore(r7_database) as store:
        assert store._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(
            CURRENT_SCHEMA_VERSION
        )
        before = store._con.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0]
        assert store._con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store._con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert InstrumentReferenceSeeder(r7_database).apply() == 0
    with HistoricalStore(r7_database) as reopened:
        assert reopened._con.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0] == before
