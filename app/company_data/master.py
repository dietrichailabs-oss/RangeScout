"""Additive provisioning for the redistributable RangeScout company master."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
import sqlite3

from app.ui.branding import application_resource_root


MASTER_FILENAME = "RangeScout_Company_Master.sqlite"


@dataclass(frozen=True, slots=True)
class CompanyMasterProvisionReport:
    source: str
    version: int
    available: int
    added: int
    aliases_added: int
    already_current: bool


def company_master_path() -> Path:
    return application_resource_root() / "resources" / MASTER_FILENAME


def provision_company_master(target_database: Path | str, master_database: Path | str | None = None) -> CompanyMasterProvisionReport:
    """Merge stable public reference rows without overwriting newer user data."""

    target = Path(target_database)
    master = Path(master_database) if master_database is not None else company_master_path()
    if not master.is_file():
        return CompanyMasterProvisionReport(str(master), 0, 0, 0, 0, False)
    with closing(sqlite3.connect(master)) as source:
        source.row_factory = sqlite3.Row
        version_row = source.execute("SELECT value FROM master_meta WHERE key='version'").fetchone()
        version = int(version_row[0]) if version_row else 0
        available = int(source.execute("SELECT COUNT(*) FROM seed_instruments").fetchone()[0])

    with closing(sqlite3.connect(target, timeout=15)) as destination:
        destination.row_factory = sqlite3.Row
        destination.execute("PRAGMA busy_timeout = 5000")
        destination.execute("PRAGMA synchronous = NORMAL")
        destination.execute("PRAGMA temp_store = MEMORY")
        destination.execute("PRAGMA foreign_keys = ON")
        current_row = destination.execute(
            "SELECT value FROM rs_schema_meta WHERE key='company_master_seed_version'"
        ).fetchone()
        current = int(current_row[0]) if current_row else 0
        if current >= version:
            return CompanyMasterProvisionReport(str(master), version, available, 0, 0, True)
        instrument_count = destination.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0]
        if instrument_count == 0:
            return _bulk_provision_empty(destination, master, version, available)
        with closing(sqlite3.connect(master)) as source:
            source.row_factory = sqlite3.Row
            records = source.execute("SELECT * FROM seed_instruments ORDER BY canonical_symbol").fetchall()
            aliases = source.execute("SELECT * FROM seed_aliases ORDER BY canonical_symbol,alias_symbol").fetchall()
            snapshots = source.execute("SELECT * FROM source_snapshots ORDER BY source_id").fetchall()
            references = source.execute(
                "SELECT * FROM seed_instrument_sources ORDER BY canonical_symbol,primary_venue,asset_class,source_id"
            ).fetchall()
        destination.execute("BEGIN IMMEDIATE")
        for row in snapshots:
            destination.execute(
                """INSERT OR IGNORE INTO rs_discovery_sources(
                       source_id,display_name,source_kind,official_url,enabled,refresh_interval_seconds,
                       last_success_utc,next_due_utc,created_at_utc,updated_at_utc)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["source_id"], row["source_id"].replace("_", " ").title(),
                    "frozen_company_master_reference", row["source_url"], 1, None,
                    row["retrieved_utc"], None, row["retrieved_utc"], row["retrieved_utc"],
                ),
            )
        before = destination.total_changes
        destination.executemany(
            """INSERT INTO rs_instruments(
                       canonical_symbol,security_name,asset_class,security_type,primary_venue,mic_code,
                       currency,country_code,cik,listing_date,is_active,first_seen_utc,last_seen_utc,
                       metadata_updated_utc,created_at_utc,updated_at_utc,sector,industry,website_domain)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(canonical_symbol,primary_venue,asset_class) DO NOTHING""",
            tuple(
                tuple(row[column] for column in (
                    "canonical_symbol", "security_name", "asset_class", "security_type", "primary_venue",
                    "mic_code", "currency", "country_code", "cik", "listing_date", "is_active",
                    "source_date", "source_date", "source_date", "source_date", "source_date",
                    "sector", "industry", "website_domain",
                ))
                for row in records
            ),
        )
        added = destination.total_changes - before
        instrument_ids = {
            (row["canonical_symbol"], row["primary_venue"], row["asset_class"]): row["instrument_id"]
            for row in destination.execute(
                "SELECT instrument_id,canonical_symbol,primary_venue,asset_class FROM rs_instruments"
            )
        }
        alias_before = destination.total_changes
        destination.executemany(
            """INSERT OR IGNORE INTO rs_instrument_aliases(
                           instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc)
                       VALUES(?,?,?,?,?,?)""",
            (
                (
                    instrument_ids[(row["canonical_symbol"], row["primary_venue"], row["asset_class"])],
                    row["alias_symbol"], row["primary_venue"], row["alias_kind"],
                    "rangescout_public_master", row["source_date"],
                )
                for row in aliases
                if (row["canonical_symbol"], row["primary_venue"], row["asset_class"]) in instrument_ids
            ),
        )
        aliases_added = destination.total_changes - alias_before
        retrieved_by_source = {row["source_id"]: row["retrieved_utc"] for row in snapshots}
        destination.executemany(
            """INSERT OR IGNORE INTO rs_instrument_reference_sources(
                   instrument_id,source_id,source_symbol,source_name,source_exchange,
                   source_snapshot_sha256,source_retrieved_utc)
               VALUES(?,?,?,?,?,?,?)""",
            (
                (
                    instrument_ids[(row["canonical_symbol"], row["primary_venue"], row["asset_class"])],
                    row["source_id"], row["source_symbol"], row["source_name"], row["source_exchange"],
                    row["source_snapshot_sha256"], retrieved_by_source[row["source_id"]],
                )
                for row in references
                if (row["canonical_symbol"], row["primary_venue"], row["asset_class"]) in instrument_ids
            ),
        )
        destination.execute(
            """INSERT OR REPLACE INTO rs_schema_meta(key,value,updated_at_utc)
               VALUES('company_master_seed_version',?,?)""",
            (str(version), records[0]["source_date"] if records else "2026-08-21T00:00:00+00:00"),
        )
        destination.commit()
    return CompanyMasterProvisionReport(str(master), version, len(records), added, aliases_added, False)


def _bulk_provision_empty(
    destination: sqlite3.Connection, master: Path, version: int, available: int,
) -> CompanyMasterProvisionReport:
    """Use set-based SQLite copies for the clean-install path; populated DBs keep the additive merge."""
    destination.execute("ATTACH DATABASE ? AS seed_master", (str(master),))
    destination.execute("PRAGMA cache_size = -65536")
    destination.execute("PRAGMA locking_mode = EXCLUSIVE")
    destination.execute("PRAGMA synchronous = OFF")
    try:
        destination.execute("BEGIN IMMEDIATE")
        deferred_indexes = destination.execute(
            """SELECT name,sql FROM sqlite_master
               WHERE type='index' AND sql IS NOT NULL
                 AND tbl_name IN ('rs_instruments','rs_instrument_aliases','rs_instrument_reference_sources')
               ORDER BY name"""
        ).fetchall()
        for name, _sql in deferred_indexes:
            destination.execute(f'DROP INDEX "{str(name).replace(chr(34), chr(34) * 2)}"')
        destination.execute(
            """INSERT OR IGNORE INTO rs_discovery_sources(
               source_id,display_name,source_kind,official_url,enabled,refresh_interval_seconds,
               last_success_utc,next_due_utc,created_at_utc,updated_at_utc)
               SELECT source_id,source_id,'frozen_company_master_reference',source_url,1,NULL,
                      retrieved_utc,NULL,retrieved_utc,retrieved_utc
               FROM seed_master.source_snapshots"""
        )
        before = destination.total_changes
        destination.execute(
            """INSERT OR IGNORE INTO rs_instruments(
               canonical_symbol,security_name,asset_class,security_type,primary_venue,mic_code,
               currency,country_code,cik,listing_date,is_active,first_seen_utc,last_seen_utc,
               metadata_updated_utc,created_at_utc,updated_at_utc,sector,industry,website_domain)
               SELECT canonical_symbol,security_name,asset_class,security_type,primary_venue,mic_code,
                      currency,country_code,cik,listing_date,is_active,source_date,source_date,
                      source_date,source_date,source_date,sector,industry,website_domain
               FROM seed_master.seed_instruments"""
        )
        added = destination.total_changes - before
        alias_before = destination.total_changes
        destination.execute(
            """INSERT OR IGNORE INTO rs_instrument_aliases(
               instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc)
               SELECT i.instrument_id,a.alias_symbol,a.primary_venue,a.alias_kind,
                      'rangescout_public_master',a.source_date
               FROM seed_master.seed_aliases a JOIN rs_instruments i
                 ON i.canonical_symbol=a.canonical_symbol AND i.primary_venue=a.primary_venue
                AND i.asset_class=a.asset_class"""
        )
        aliases_added = destination.total_changes - alias_before
        destination.execute(
            """INSERT OR IGNORE INTO rs_instrument_reference_sources(
               instrument_id,source_id,source_symbol,source_name,source_exchange,
               source_snapshot_sha256,source_retrieved_utc)
               SELECT i.instrument_id,r.source_id,r.source_symbol,r.source_name,r.source_exchange,
                      r.source_snapshot_sha256,s.retrieved_utc
               FROM seed_master.seed_instrument_sources r
               JOIN rs_instruments i ON i.canonical_symbol=r.canonical_symbol
                    AND i.primary_venue=r.primary_venue AND i.asset_class=r.asset_class
               JOIN seed_master.source_snapshots s ON s.source_id=r.source_id"""
        )
        seeded_at = destination.execute("SELECT MAX(source_date) FROM seed_master.seed_instruments").fetchone()[0]
        destination.execute(
            "INSERT OR REPLACE INTO rs_schema_meta(key,value,updated_at_utc) VALUES('company_master_seed_version',?,?)",
            (str(version), seeded_at or "2026-08-21T00:00:00+00:00"),
        )
        for _name, sql in deferred_indexes:
            destination.execute(str(sql))
        destination.commit()
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("company master seed integrity check failed")
        destination.execute("PRAGMA synchronous = NORMAL")
    except Exception:
        destination.rollback()
        raise
    finally:
        destination.execute("DETACH DATABASE seed_master")
    return CompanyMasterProvisionReport(str(master), version, available, added, aliases_added, False)
