from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from app.company_data.instrument_intelligence import InstrumentResolver
from app.historical_store.migrations import apply_migrations
from app.market_data.contracts import AssetClass
from app.market_data.discovery import (
    DiscoveryCoordinator,
    InstrumentDiscovery,
    OfficialNasdaqDirectorySource,
    SourceCompleteness,
    _validate_official_directory,
)
from app.market_data.instruments import DiscoveredInstrument
from app.research.routing import plan_research


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def item(
    symbol: str,
    name: str,
    venue: str = "NASDAQ",
    asset: AssetClass = AssetClass.EQUITY,
    aliases: tuple[tuple[str, str], ...] = (),
) -> DiscoveredInstrument:
    return DiscoveredInstrument(
        symbol,
        name,
        asset,
        "Listed Security",
        venue,
        provider_symbol=symbol,
        official_aliases=aliases,
    )


def complete(subsource: str, count: int) -> SourceCompleteness:
    return SourceCompleteness(
        subsource, count, 0, True, True, None, True, "complete", None, f"{subsource}-hash"
    )


def incomplete(subsource: str, count: int, reason: str, status: str = "incomplete") -> SourceCompleteness:
    return SourceCompleteness(
        subsource, count, 0, True, False, 5_500, False, status, reason, f"{subsource}-bad"
    )


def migrated(path=None) -> sqlite3.Connection:
    connection = sqlite3.connect(path or ":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES('schema_version','1')")
    connection.commit()
    apply_migrations(connection, 1)
    return connection


def nasdaq_text(count: int, *, footer: bool = True, header: str | None = None) -> str:
    fields = header or "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"
    rows = [
        f"Q{i:04d}|Quality {i} Common Stock|Q|N|N|100|N|N"
        for i in range(count)
    ]
    if footer:
        rows.append("File Creation Time: 0825202621:00|||||||")
    return "\n".join([fields, *rows, ""])


def other_text(count: int, *, footer: bool = True) -> str:
    rows = [
        f"O{i:04d}|Other {i} Common Stock|N|O{i:04d}|N|100|N|O{i:04d}"
        for i in range(count)
    ]
    if footer:
        rows.append("File Creation Time: 0825202621:00|||||||")
    return "\n".join([
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol",
        *rows,
        "",
    ])


def test_state_d_incomplete_snapshot_is_non_destructive_and_retries_soon() -> None:
    connection = migrated()
    discovery = InstrumentDiscovery(connection)
    first = discovery.import_snapshot(
        DiscoveryCoordinator.SOURCE_ID,
        DiscoveryCoordinator.DISPLAY_NAME,
        DiscoveryCoordinator.OFFICIAL_URL,
        [item("AAA", "Alpha Common Stock"), item("BBB", "Beta Common Stock")],
        b"complete",
        now=NOW,
        source_validations=(complete("nasdaqlisted", 1), complete("otherlisted", 1)),
        reconciliation_complete=True,
    )
    assert first.status == "complete" and first.added == 2
    before = [tuple(row) for row in connection.execute(
        "SELECT instrument_id,canonical_symbol,is_active FROM rs_instruments ORDER BY instrument_id"
    )]
    last_success = connection.execute(
        "SELECT last_success_utc FROM rs_discovery_sources WHERE source_id=?",
        (DiscoveryCoordinator.SOURCE_ID,),
    ).fetchone()[0]

    report = discovery.import_snapshot(
        DiscoveryCoordinator.SOURCE_ID,
        DiscoveryCoordinator.DISPLAY_NAME,
        DiscoveryCoordinator.OFFICIAL_URL,
        [item("AAA", "Alpha Common Stock")],
        b"truncated",
        now=NOW + timedelta(days=7),
        source_validations=(
            incomplete("nasdaqlisted", 100, "implausible_drop_from_last_success"),
            complete("otherlisted", 1),
        ),
        reconciliation_complete=False,
    )

    after = [tuple(row) for row in connection.execute(
        "SELECT instrument_id,canonical_symbol,is_active FROM rs_instruments ORDER BY instrument_id"
    )]
    source = connection.execute(
        "SELECT last_success_utc,next_due_utc FROM rs_discovery_sources WHERE source_id=?",
        (DiscoveryCoordinator.SOURCE_ID,),
    ).fetchone()
    assert report.status == "incomplete"
    assert report.added == report.removed_inactive == report.changed == 0
    assert after == before
    assert source[0] == last_success
    retry = datetime.fromisoformat(source[1])
    assert retry <= NOW + timedelta(days=7, hours=7)


def test_official_completeness_adversarial_cases() -> None:
    rows, valid = _validate_official_directory("nasdaqlisted", nasdaq_text(1_000), "Q", 1_050)
    assert len(rows) == 1_000 and valid.complete

    _rows, missing_footer = _validate_official_directory(
        "nasdaqlisted", nasdaq_text(1_000, footer=False), "Q", 1_000
    )
    assert not missing_footer.complete and "missing_official_footer" in (missing_footer.reason or "")

    _rows, malformed_header = _validate_official_directory(
        "nasdaqlisted", nasdaq_text(1_000, header="Symbol|Bad"), "Q", 1_000
    )
    assert not malformed_header.complete and "malformed_header" in (malformed_header.reason or "")

    _rows, truncated = _validate_official_directory(
        "nasdaqlisted", nasdaq_text(100), "Q", 5_500
    )
    assert not truncated.complete
    assert "implausibly_small_source" in (truncated.reason or "")
    assert "implausible_drop_from_last_success" in (truncated.reason or "")

    _rows, empty = _validate_official_directory("nasdaqlisted", "", "Q", 5_500)
    assert not empty.complete and "malformed_header" in (empty.reason or "")


def test_one_source_network_failure_marks_combined_snapshot_failed() -> None:
    def fetch(url: str) -> str:
        if url.endswith("nasdaqlisted.txt"):
            raise OSError("offline")
        return other_text(1_000)

    snapshot = OfficialNasdaqDirectorySource(fetch).fetch(
        {"nasdaqlisted": 5_500, "otherlisted": 1_000}
    )
    assert not snapshot.complete
    by_name = {item.subsource_id: item for item in snapshot.validations}
    assert by_name["nasdaqlisted"].status == "failed"
    assert "fetch_failed:OSError" in (by_name["nasdaqlisted"].reason or "")
    assert by_name["otherlisted"].complete


def test_new_preferred_series_gets_alias_crosswalk_and_provider_mapping_before_restart(tmp_path) -> None:
    path = tmp_path / "history.sqlite"
    connection = migrated(path)
    discovery = InstrumentDiscovery(connection)
    preferred = item(
        "ZZQA$A",
        "QA Preferred Series A",
        "NYSE",
        AssetClass.PREFERRED,
        (
            ("ZZQA$A", "official_directory_symbol"),
            ("ZZQApA", "official_source_symbol_variant"),
            ("ZZQA-A", "official_source_symbol_variant"),
        ),
    )
    report = discovery.import_snapshot(
        DiscoveryCoordinator.SOURCE_ID,
        DiscoveryCoordinator.DISPLAY_NAME,
        DiscoveryCoordinator.OFFICIAL_URL,
        [preferred],
        b"new-series",
        now=NOW,
        source_validations=(complete("nasdaqlisted", 1), complete("otherlisted", 1)),
        reconciliation_complete=True,
    )
    assert report.added == 1
    instrument_id = connection.execute(
        "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='ZZQA$A'"
    ).fetchone()[0]
    aliases = {row[0] for row in connection.execute(
        "SELECT alias_symbol FROM rs_instrument_aliases WHERE instrument_id=?", (instrument_id,)
    )}
    assert {"ZZQA$A", "ZZQAPA", "ZZQA-A"} <= aliases
    mapping = connection.execute(
        "SELECT provider_symbol,mapping_status FROM rs_provider_symbols WHERE provider_id='yahoo' AND instrument_id=?",
        (instrument_id,),
    ).fetchone()
    assert tuple(mapping) == ("ZZQA-PA", "derived_official_aliases")
    support = {
        row[0]: row[1] for row in connection.execute(
            "SELECT capability,support_status FROM rs_provider_instrument_support WHERE provider_id='yahoo' AND instrument_id=?",
            (instrument_id,),
        )
    }
    assert support == {"candles": "supported", "historical": "supported", "quote": "supported"}
    connection.close()

    restarted = InstrumentResolver(path).resolve_unique("ZZQA$A")
    assert restarted is not None and restarted.symbol == "ZZQA$A"
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT provider_symbol FROM rs_provider_symbols WHERE instrument_id=?", (instrument_id,)
    ).fetchone()[0] == "ZZQA-PA"
    connection.close()


def test_existing_closed_end_fund_semantics_take_precedence_over_generic_refresh() -> None:
    connection = migrated()
    stamp = NOW.isoformat()
    result = connection.execute(
        """INSERT INTO rs_instruments(
           canonical_symbol,security_name,asset_class,security_type,primary_venue,is_active,
           first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc,instrument_subtype,
           issuer_entity_type,security_role)
           VALUES('HPF','John Hancock Preferred Income Fund','closed_end_fund','Closed-End Fund',
                  'NYSE',1,?,?,?,?,'closed_end_fund','closed_end_fund','primary_common')""",
        (stamp, stamp, stamp, stamp),
    )
    instrument_id = int(result.lastrowid)
    connection.commit()

    discovery = InstrumentDiscovery(connection)
    discovery.import_snapshot(
        DiscoveryCoordinator.SOURCE_ID,
        DiscoveryCoordinator.DISPLAY_NAME,
        DiscoveryCoordinator.OFFICIAL_URL,
        [item("HPF", "John Hancock Preferred Income Fund", "NYSE", AssetClass.EQUITY)],
        b"refresh",
        now=NOW,
        source_validations=(complete("nasdaqlisted", 1), complete("otherlisted", 1)),
        reconciliation_complete=True,
    )
    row = connection.execute(
        "SELECT asset_class,instrument_subtype,issuer_entity_type,security_role FROM rs_instruments WHERE instrument_id=?",
        (instrument_id,),
    ).fetchone()
    assert tuple(row) == ("closed_end_fund", "closed_end_fund", "closed_end_fund", "primary_common")


def test_all_future_listing_types_are_enriched_immediately_and_after_restart(tmp_path) -> None:
    path = tmp_path / "future-listings.sqlite"
    connection = migrated(path)
    future = [
        item("RQXAC", "R10 Common Stock", "NYSE", AssetClass.EQUITY),
        item(
            "RQXP$A", "R10 Series A Preferred Stock", "NYSE", AssetClass.PREFERRED,
            (("RQXP$A", "official_directory_symbol"), ("RQXPpA", "official_source_symbol_variant"),
             ("RQXP-A", "official_source_symbol_variant")),
        ),
        item("RQXAD", "R10 American Depositary Shares", "NYSE", AssetClass.ADR),
        item("RQXAW", "R10 Warrant", "NYSE", AssetClass.WARRANT),
        item("RQXAR", "R10 Subscription Right", "NYSE", AssetClass.RIGHT),
        item("RQXAU", "R10 Units, each consisting of one Common Share and one Right", "NYSE", AssetClass.UNIT),
        item("RQXAF", "R10 Exchange-Traded Fund ETF", "NYSE", AssetClass.ETF),
        item("RQXAN", "R10 Exchange-Traded Note ETN", "NYSE", AssetClass.ETN),
        item("RQXAX", "R10 Closed-End Fund Common Shares", "NYSE", AssetClass.CLOSED_END_FUND),
    ]
    report = InstrumentDiscovery(connection).import_snapshot(
        DiscoveryCoordinator.SOURCE_ID,
        DiscoveryCoordinator.DISPLAY_NAME,
        DiscoveryCoordinator.OFFICIAL_URL,
        future,
        b"future-listing-types",
        now=NOW,
        source_validations=(complete("nasdaqlisted", 5), complete("otherlisted", 4)),
        reconciliation_complete=True,
    )
    assert report.added == len(future)
    for expected in future:
        instrument_id = connection.execute(
            "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=?",
            (expected.canonical_symbol,),
        ).fetchone()[0]
        support = dict(connection.execute(
            "SELECT capability,support_status FROM rs_provider_instrument_support "
            "WHERE provider_id='yahoo' AND instrument_id=?",
            (instrument_id,),
        ))
        assert support["quote"] == support["historical"] == "supported"
        assert connection.execute(
            "SELECT provider_symbol FROM rs_provider_symbols "
            "WHERE provider_id='yahoo' AND instrument_id=? AND is_active=1",
            (instrument_id,),
        ).fetchone() is not None
    connection.close()

    resolver = InstrumentResolver(path)
    for expected in future:
        match = resolver.resolve_unique(expected.canonical_symbol)
        assert match is not None
        instrument = match.instrument
        assert plan_research(
            instrument.asset_class, instrument.subtype, instrument.issuer_type, instrument.security_role
        ).route.value in {"corporate", "fund", "market_instrument"}
        with sqlite3.connect(path) as restarted:
            raw = restarted.execute(
                "SELECT asset_class FROM rs_instruments WHERE instrument_id=?",
                (instrument.instrument_id,),
            ).fetchone()[0]
        assert raw == instrument.asset_class
