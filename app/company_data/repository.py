"""Fast local company identity and logo-provenance queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from typing import Iterator


@dataclass(frozen=True, slots=True)
class CompanyRecord:
    instrument_id: int
    canonical_symbol: str
    security_name: str | None
    primary_venue: str
    cik: str | None
    aliases: tuple[str, ...]
    is_active: bool
    listing_date: str | None
    delisting_date: str | None
    logo_source_id: str | None
    logo_lookup_identifier: str | None
    logo_source_url: str | None
    local_logo_path: str | None
    logo_content_sha256: str | None
    logo_license_metadata: str | None
    logo_last_checked_utc: str | None
    logo_last_success_utc: str | None
    logo_next_refresh_utc: str | None
    logo_failure_count: int
    logo_last_error: str | None


@dataclass(frozen=True, slots=True)
class CompanyDatabaseStatus:
    total_instruments: int
    active_instruments: int
    inactive_instruments: int
    alias_count: int
    logo_coverage: int
    logo_failures: int
    last_database_update: str | None
    companies_added: int
    companies_changed: int
    inactive_or_delisted: int
    aliases_or_symbol_changes: int
    logo_successes: int
    source_failures: int
    current_update_status: str


class CompanyDatabaseRepository:
    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)

    def resolve(self, symbol: str) -> CompanyRecord | None:
        normalized = str(symbol).strip().upper()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT DISTINCT i.* FROM rs_instruments i
                   LEFT JOIN rs_instrument_aliases a ON a.instrument_id=i.instrument_id
                   WHERE UPPER(i.canonical_symbol)=? OR UPPER(COALESCE(a.alias_symbol,''))=?
                   ORDER BY i.is_active DESC, i.updated_at_utc DESC LIMIT 1""",
                (normalized, normalized),
            ).fetchone()
            if row is None:
                return None
            aliases = tuple(
                item[0] for item in connection.execute(
                    "SELECT alias_symbol FROM rs_instrument_aliases WHERE instrument_id=? ORDER BY alias_id DESC",
                    (row["instrument_id"],),
                ).fetchall()
            )
        return CompanyRecord(
            instrument_id=int(row["instrument_id"]),
            canonical_symbol=row["canonical_symbol"],
            security_name=row["security_name"],
            primary_venue=row["primary_venue"],
            cik=row["cik"],
            aliases=aliases,
            is_active=bool(row["is_active"]),
            listing_date=row["listing_date"],
            delisting_date=row["delisting_date"],
            logo_source_id=row["logo_source_id"],
            logo_lookup_identifier=row["logo_lookup_identifier"],
            logo_source_url=row["logo_source_url"],
            local_logo_path=row["local_logo_path"],
            logo_content_sha256=row["logo_content_sha256"],
            logo_license_metadata=row["logo_license_metadata"],
            logo_last_checked_utc=row["logo_last_checked_utc"],
            logo_last_success_utc=row["logo_last_success_utc"],
            logo_next_refresh_utc=row["logo_next_refresh_utc"],
            logo_failure_count=int(row["logo_failure_count"] or 0),
            logo_last_error=row["logo_last_error"],
        )

    def record_logo_result(
        self,
        symbol: str,
        *,
        source_id: str,
        lookup_identifier: str | None,
        source_url: str | None,
        content_sha256: str | None,
        license_metadata: str | None,
        local_path: str | None = None,
        success: bool,
        next_refresh_utc: datetime | None,
        error: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        normalized = str(symbol).strip().upper()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=? ORDER BY is_active DESC LIMIT 1",
                (normalized,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """UPDATE rs_instruments SET logo_source_id=?,logo_lookup_identifier=?,logo_source_url=?,
                   local_logo_path=?,logo_content_sha256=?,logo_license_metadata=?,logo_last_checked_utc=?,
                   logo_last_success_utc=CASE WHEN ? THEN ? ELSE logo_last_success_utc END,
                   logo_next_refresh_utc=?,logo_failure_count=CASE WHEN ? THEN 0 ELSE logo_failure_count+1 END,
                   logo_last_error=?,updated_at_utc=? WHERE instrument_id=?""",
                (
                    source_id, lookup_identifier, source_url, local_path, content_sha256, license_metadata,
                    now, int(success), now, next_refresh_utc.astimezone(timezone.utc).isoformat() if next_refresh_utc else None,
                    int(success), None if success else str(error or "unavailable")[:300], now, row[0],
                ),
            )
            connection.commit()
        return True

    def status(self) -> CompanyDatabaseStatus:
        with self._connect() as connection:
            totals = connection.execute(
                """SELECT COUNT(*),SUM(is_active),SUM(CASE WHEN is_active=0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN logo_last_success_utc IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN logo_failure_count>0 THEN 1 ELSE 0 END) FROM rs_instruments"""
            ).fetchone()
            aliases = connection.execute("SELECT COUNT(*) FROM rs_instrument_aliases").fetchone()[0]
            run = connection.execute(
                "SELECT * FROM rs_company_update_runs ORDER BY update_run_id DESC LIMIT 1"
            ).fetchone()
        return CompanyDatabaseStatus(
            total_instruments=int(totals[0] or 0), active_instruments=int(totals[1] or 0),
            inactive_instruments=int(totals[2] or 0), alias_count=int(aliases or 0),
            logo_coverage=int(totals[3] or 0), logo_failures=int(totals[4] or 0),
            last_database_update=run["completed_at_utc"] if run else None,
            companies_added=int(run["added_count"] or 0) if run else 0,
            companies_changed=int(run["changed_count"] or 0) if run else 0,
            inactive_or_delisted=int(run["inactive_count"] or 0) if run else 0,
            aliases_or_symbol_changes=int(run["alias_change_count"] or 0) if run else 0,
            logo_successes=int(run["logo_success_count"] or 0) if run else 0,
            source_failures=int(run["source_failure_count"] or 0) if run else 0,
            current_update_status=str(run["status"]) if run else "Never updated",
        )

    def record_update_run(
        self,
        kind: str,
        *,
        status: str,
        before: int = 0,
        after: int = 0,
        added: int = 0,
        changed: int = 0,
        inactive: int = 0,
        aliases: int = 0,
        logo_successes: int = 0,
        logo_failures: int = 0,
        source_failures: int = 0,
        error: str | None = None,
    ) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO rs_company_update_runs(update_kind,started_at_utc,completed_at_utc,status,
                   before_count,after_count,added_count,changed_count,inactive_count,alias_change_count,
                   logo_success_count,logo_failure_count,source_failure_count,error_summary)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (kind, stamp, stamp, status, before, after, added, changed, inactive, aliases,
                 logo_successes, logo_failures, source_failures, str(error)[:500] if error else None),
            )
            connection.commit()

    def health(self) -> dict[str, object]:
        with self._connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
        return {"healthy": integrity == "ok" and not foreign_keys, "integrity_check": integrity, "foreign_key_violations": foreign_keys}

    def last_success(self, kind: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT completed_at_utc FROM rs_company_update_runs
                   WHERE update_kind=? AND status='complete' ORDER BY update_run_id DESC LIMIT 1""",
                (kind,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        parsed = datetime.fromisoformat(row[0])
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def due_logo_symbols(self, now: datetime | None = None, *, limit: int = 25) -> tuple[tuple[str, str], ...]:
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT canonical_symbol,primary_venue FROM rs_instruments
                   WHERE is_active=1 AND (logo_next_refresh_utc IS NULL OR logo_next_refresh_utc<=?)
                   ORDER BY CASE WHEN logo_last_checked_utc IS NULL THEN 0 ELSE 1 END,
                            logo_last_checked_utc,canonical_symbol LIMIT ?""",
                (stamp, max(1, min(100, int(limit)))),
            ).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()
