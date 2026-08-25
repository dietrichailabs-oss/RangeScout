from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from app.company_data.instrument_intelligence import (
    classify_security_role,
    default_issuer_entity_type,
)
from app.historical_store.migrations import apply_migrations
from app.market_data.contracts import AssetClass
from app.market_data.discovery import DiscoveryCoordinator, InstrumentDiscovery, SourceCompleteness
from app.market_data.instruments import DiscoveredInstrument
from app.research.routing import ResearchRoute, plan_research


NOW = datetime(2026, 8, 25, 16, tzinfo=timezone.utc)
SOURCE = DiscoveryCoordinator.SOURCE_ID


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO meta VALUES('schema_version','1')")
    connection.commit()
    apply_migrations(connection, 1)
    return connection


def security(
    symbol: str,
    name: str | None = None,
    *,
    venue: str = "NASDAQ",
    asset: AssetClass = AssetClass.EQUITY,
    partition: str = "nasdaqlisted",
    previous: tuple[str, ...] = (),
) -> DiscoveredInstrument:
    return DiscoveredInstrument(
        symbol,
        name or f"{symbol} Common Stock",
        asset,
        "Listed Security",
        venue,
        provider_symbol=symbol,
        official_aliases=((symbol, "official_directory_symbol"),),
        source_partition=partition,
        verified_previous_symbols=previous,
    )


def complete(count: int, digest: str, partition: str = "nasdaqlisted") -> SourceCompleteness:
    return SourceCompleteness(partition, count, 0, True, True, None, True, "complete", None, digest)


def refresh(
    connection: sqlite3.Connection,
    rows: list[DiscoveredInstrument],
    *,
    digest: str,
    day: int = 0,
):
    return InstrumentDiscovery(connection).import_snapshot(
        SOURCE,
        DiscoveryCoordinator.DISPLAY_NAME,
        DiscoveryCoordinator.OFFICIAL_URL,
        rows,
        digest.encode("ascii"),
        now=NOW + timedelta(days=day),
        source_validations=(complete(len(rows), digest),),
        reconciliation_complete=True,
    )


def active_symbols(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT canonical_symbol FROM rs_instruments WHERE is_active=1 ORDER BY canonical_symbol"
    )]


def test_small_missing_set_is_pending_then_requires_independent_confirmation() -> None:
    connection = database()
    baseline = [security(f"R{i:03d}") for i in range(12)]
    assert refresh(connection, baseline, digest="full").added == 12
    candidate = baseline[:-1]

    first = refresh(connection, candidate, digest="partial-a", day=7)
    repeated = refresh(connection, candidate, digest="partial-a", day=8)
    assert first.removed_inactive == repeated.removed_inactive == 0
    assert "R011" in active_symbols(connection)
    pending = connection.execute(
        "SELECT state,missing_observations FROM rs_discovery_subsource_members "
        "WHERE source_id=? AND instrument_id=(SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='R011')",
        (SOURCE,),
    ).fetchone()
    assert tuple(pending) == ("pending", 1)

    confirmed = refresh(connection, candidate, digest="partial-b", day=9)
    assert confirmed.removed_inactive == 1
    assert "R011" not in active_symbols(connection)


def test_returning_symbol_clears_pending_removal() -> None:
    connection = database()
    baseline = [security(f"C{i:03d}") for i in range(10)]
    refresh(connection, baseline, digest="full")
    refresh(connection, baseline[:-1], digest="drop", day=7)
    refresh(connection, baseline, digest="return", day=8)
    state = connection.execute(
        "SELECT state,missing_observations FROM rs_discovery_subsource_members "
        "WHERE instrument_id=(SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='C009')"
    ).fetchone()
    assert tuple(state) == ("present", 0)
    assert "C009" in active_symbols(connection)


@pytest.mark.parametrize("missing", [26, 30, 100])
def test_bulk_partial_source_is_rejected_without_catalog_mutation(missing: int) -> None:
    connection = database()
    baseline = [security(f"B{i:03d}") for i in range(130)]
    refresh(connection, baseline, digest="full")
    identities = list(connection.execute(
        "SELECT instrument_id,canonical_symbol,is_active FROM rs_instruments ORDER BY instrument_id"
    ))
    report = refresh(connection, baseline[:-missing], digest=f"drop-{missing}", day=7)
    after = list(connection.execute(
        "SELECT instrument_id,canonical_symbol,is_active FROM rs_instruments ORDER BY instrument_id"
    ))
    assert report.status == "incomplete"
    assert report.removed_inactive == report.added == report.changed == 0
    assert [tuple(row) for row in after] == [tuple(row) for row in identities]
    assert f"missing={missing}" in (report.error_summary or "")


def test_same_name_same_venue_siblings_get_distinct_stable_identities() -> None:
    connection = database()
    common_name = "Federal Agricultural Mortgage Corporation Common Stock"
    first = [security("AGM", common_name), security("AGM.A", common_name)]
    refresh(connection, first, digest="siblings-a")
    before = dict(connection.execute(
        "SELECT canonical_symbol,instrument_id FROM rs_instruments WHERE canonical_symbol LIKE 'AGM%'"
    ))
    expanded = [*first, security("AGM.B", common_name)]
    report = refresh(connection, expanded, digest="siblings-b", day=7)
    after = dict(connection.execute(
        "SELECT canonical_symbol,instrument_id FROM rs_instruments WHERE canonical_symbol LIKE 'AGM%'"
    ))
    assert report.added == 1
    assert after["AGM"] == before["AGM"]
    assert after["AGM.A"] == before["AGM.A"]
    assert after["AGM.B"] not in set(before.values())
    assert refresh(connection, expanded, digest="siblings-c", day=8).added == 0
    assert dict(connection.execute(
        "SELECT canonical_symbol,instrument_id FROM rs_instruments WHERE canonical_symbol LIKE 'AGM%'"
    )) == after


def test_verified_security_level_rename_preserves_identity_and_records_evidence() -> None:
    connection = database()
    refresh(connection, [security("OLDX", "Verified Issuer Common Stock")], digest="rename-a")
    old_id = connection.execute(
        "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='OLDX'"
    ).fetchone()[0]
    renamed = security(
        "NEWX", "Verified Issuer Common Stock", previous=("OLDX",),
    )
    report = refresh(connection, [renamed], digest="rename-b", day=7)
    new_id = connection.execute(
        "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='NEWX'"
    ).fetchone()[0]
    assert new_id == old_id
    assert report.added == 0 and report.changed == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM rs_instrument_identity_evidence WHERE old_symbol='OLDX' AND new_symbol='NEWX'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM rs_instrument_aliases WHERE instrument_id=? AND alias_symbol='OLDX' "
        "AND alias_kind='previous_symbol'", (old_id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("symbol,name", [
    ("ET", "Energy Transfer LP Common Units"),
    ("MPLX", "MPLX LP Common Units Representing Limited Partner Interests"),
    ("PAA", "Plains All American Pipeline, L.P. Common Units"),
    ("SUN", "Sunoco LP Common Units representing limited partner interests"),
    ("WES", "Western Midstream Partners, LP Common Units"),
])
def test_operating_partnership_units_receive_issuer_aware_sec_research(symbol: str, name: str) -> None:
    issuer = default_issuer_entity_type("unit", name)
    role = classify_security_role("unit", "unit", "Unit", symbol, name, issuer)
    plan = plan_research("unit", "unit", issuer, role)
    assert (issuer, role) == ("operating_partnership", "primary_common")
    assert plan.route is ResearchRoute.CORPORATE
    assert plan.sec_applicable and plan.analyst_applicable


@pytest.mark.parametrize("asset,name,expected_issuer,expected_role,expected_route", [
    ("unit", "Acquisition Corp Units, each consisting of one share and one-half warrant", "unknown", "alternate_security", ResearchRoute.MARKET_INSTRUMENT),
    ("unit", "Example Statutory Trust Units of Beneficial Interest", "fund_vehicle", "fund", ResearchRoute.FUND),
    ("warrant", "Example Common Stock Purchase Warrants", "unknown", "alternate_security", ResearchRoute.MARKET_INSTRUMENT),
    ("right", "Example Common Stock Subscription Rights", "unknown", "alternate_security", ResearchRoute.MARKET_INSTRUMENT),
])
def test_asset_aware_roles_prevent_impossible_primary_common(
    asset: str, name: str, expected_issuer: str, expected_role: str, expected_route: ResearchRoute,
) -> None:
    issuer = default_issuer_entity_type(asset, name)
    role = classify_security_role(asset, asset, asset, "QA.X", name, issuer)
    plan = plan_research(asset, asset, issuer, role)
    assert issuer == expected_issuer
    assert role == expected_role
    assert plan.route is expected_route
