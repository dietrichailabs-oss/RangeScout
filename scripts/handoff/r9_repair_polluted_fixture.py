"""Verify safe R9 repair of a real R8-polluted database copy."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def scalar(connection: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> object:
    row = connection.execute(query, params).fetchone()
    return row[0] if row else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pristine_database", type=Path)
    parser.add_argument("user_state", type=Path)
    parser.add_argument("output_database", type=Path)
    parser.add_argument("record", type=Path)
    args = parser.parse_args()

    args.output_database.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.pristine_database, args.output_database)
    before_sha = sha256(args.output_database)
    before_rows: int
    with sqlite3.connect(args.output_database) as connection:
        before_rows = int(scalar(connection, "SELECT COUNT(*) FROM rs_instruments"))

    with HistoricalStore(args.output_database) as store:
        migration_checks = store.database_checks()
    provision = provision_company_master(args.output_database)
    reference_changes = InstrumentReferenceSeeder(args.output_database).apply()

    external_hashes = {path.name: sha256(path) for path in sorted(args.user_state.iterdir()) if path.is_file()}
    with sqlite3.connect(args.output_database) as connection:
        connection.row_factory = sqlite3.Row
        merged = int(scalar(connection, "SELECT value FROM meta WHERE key='r9_discovery_repair_merged'") or 0)
        conflicts = int(scalar(connection, "SELECT value FROM meta WHERE key='r9_discovery_repair_conflicts'") or 0)
        test_issues = int(scalar(connection, "SELECT value FROM meta WHERE key='r9_test_issues_deactivated'") or 0)
        active = int(scalar(connection, "SELECT COUNT(*) FROM rs_instruments WHERE is_active=1"))
        total = int(scalar(connection, "SELECT COUNT(*) FROM rs_instruments"))
        duplicate_groups = int(scalar(
            connection,
            """SELECT COUNT(*) FROM (
                 SELECT canonical_symbol FROM rs_instruments WHERE is_active=1
                 GROUP BY canonical_symbol HAVING COUNT(*)>1)""",
        ))
        mappings = int(scalar(connection, "SELECT COUNT(*) FROM rs_instrument_identity_merges"))
        old_id = 16405
        survivor = int(scalar(
            connection,
            "SELECT survivor_instrument_id FROM rs_instrument_identity_merges WHERE old_instrument_id=?",
            (old_id,),
        ))
        old_active = int(scalar(connection, "SELECT is_active FROM rs_instruments WHERE instrument_id=?", (old_id,)))
        active_aapl = dict(connection.execute(
            """SELECT instrument_id,asset_class,security_type,primary_venue
               FROM rs_instruments WHERE canonical_symbol='AAPL' AND is_active=1"""
        ).fetchone())
        quote_rows = [dict(row) for row in connection.execute(
            "SELECT instrument_id,last_price,provider_id FROM rs_last_quotes WHERE last_price=123.45"
        )]
        cache_rows = [dict(row) for row in connection.execute(
            """SELECT instrument_id,capability,applicability,reason
               FROM rs_instrument_capabilities WHERE reason='r8-user-cache-fixture'"""
        )]
        integrity = str(scalar(connection, "PRAGMA integrity_check"))
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())

    resolved_old = InstrumentResolver(args.output_database).by_id(old_id)
    pass_gate = all((
        before_rows == 29_551,
        total == before_rows,
        active == 16_382,
        duplicate_groups == 0,
        merged == 13_136,
        mappings == 13_136,
        test_issues == 33,
        old_active == 0,
        survivor == int(active_aapl["instrument_id"]),
        resolved_old is not None and resolved_old.instrument_id == survivor,
        any(int(row["instrument_id"]) == survivor for row in quote_rows),
        len(cache_rows) == 1,
        integrity == "ok",
        foreign_keys == 0,
        migration_checks["integrity_check"] == "ok",
        not migration_checks["foreign_key_violations"],
    ))
    record = {
        "schema": "rangescout.r9-polluted-database-repair.v1",
        "pass": pass_gate,
        "pristine_database": str(args.pristine_database),
        "pristine_copy_sha256_before_migration": before_sha,
        "output_database": str(args.output_database),
        "instrument_rows_before": before_rows,
        "instrument_rows_after": total,
        "active_after": active,
        "duplicate_groups_after": duplicate_groups,
        "identity_merges": mappings,
        "exact_r8_clones_deactivated": merged,
        "reference_conflicts_preserved": conflicts,
        "official_test_issues_deactivated": test_issues,
        "old_clone_id": old_id,
        "old_clone_active_after": old_active,
        "survivor_id": survivor,
        "resolver_old_id_follows_survivor": resolved_old is not None and resolved_old.instrument_id == survivor,
        "active_aapl": active_aapl,
        "last_quote_fixture_rows": quote_rows,
        "capability_fixture_rows": cache_rows,
        "company_master_provision": asdict(provision),
        "instrument_reference_changes": reference_changes,
        "external_user_state_hashes_after": external_hashes,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "credentials_inspected": False,
        "credentials_exposed": False,
    }
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if pass_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())