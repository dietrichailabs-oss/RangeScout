"""Persistent non-image logo state stored in RangeScout's SQLite database."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from typing import Iterator


@dataclass(frozen=True, slots=True)
class LogoState:
    symbol: str
    exchange: str
    provider_id: str
    status: str
    last_attempt_utc: datetime
    last_success_utc: datetime | None
    retry_after_utc: datetime | None
    content_type: str | None
    content_sha256: str | None
    error_code: str | None


class CompanyLogoStateRepository:
    """Stores only resolution/retry metadata; never provider credentials or logo bytes."""

    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)

    def load(self, symbol: str, exchange: str | None, provider_id: str) -> LogoState | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT symbol, exchange, provider_id, status, last_attempt_utc, last_success_utc,
                       retry_after_utc, content_type, content_sha256, error_code
                FROM rs_company_logo_state
                WHERE symbol=? AND exchange=? AND provider_id=?
                """,
                (_symbol(symbol), _exchange(exchange), provider_id),
            ).fetchone()
        if row is None:
            return None
        return LogoState(
            symbol=row["symbol"],
            exchange=row["exchange"],
            provider_id=row["provider_id"],
            status=row["status"],
            last_attempt_utc=_dt(row["last_attempt_utc"]) or datetime.now(timezone.utc),
            last_success_utc=_dt(row["last_success_utc"]),
            retry_after_utc=_dt(row["retry_after_utc"]),
            content_type=row["content_type"],
            content_sha256=row["content_sha256"],
            error_code=row["error_code"],
        )

    def record(
        self,
        *,
        symbol: str,
        exchange: str | None,
        provider_id: str,
        status: str,
        attempted_at: datetime,
        success_at: datetime | None = None,
        retry_after: datetime | None = None,
        content_type: str | None = None,
        content_sha256: str | None = None,
        error_code: str | None = None,
    ) -> None:
        values = (
            _symbol(symbol),
            _exchange(exchange),
            provider_id,
            status,
            attempted_at.astimezone(timezone.utc).isoformat(),
            success_at.astimezone(timezone.utc).isoformat() if success_at else None,
            retry_after.astimezone(timezone.utc).isoformat() if retry_after else None,
            content_type,
            content_sha256,
            error_code,
            attempted_at.astimezone(timezone.utc).isoformat(),
        )
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO rs_company_logo_state(
                    symbol, exchange, provider_id, status, last_attempt_utc, last_success_utc,
                    retry_after_utc, content_type, content_sha256, error_code, updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, exchange, provider_id) DO UPDATE SET
                    status=excluded.status,
                    last_attempt_utc=excluded.last_attempt_utc,
                    last_success_utc=excluded.last_success_utc,
                    retry_after_utc=excluded.retry_after_utc,
                    content_type=excluded.content_type,
                    content_sha256=excluded.content_sha256,
                    error_code=excluded.error_code,
                    updated_at_utc=excluded.updated_at_utc
                """,
                values,
            )
            con.commit()

    def retry_blocked(self, symbol: str, exchange: str | None, provider_id: str, now: datetime) -> bool:
        state = self.load(symbol, exchange, provider_id)
        return bool(state and state.retry_after_utc and state.retry_after_utc > now.astimezone(timezone.utc))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
        finally:
            con.close()


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _symbol(value: str) -> str:
    return str(value).strip().upper()


def _exchange(value: str | None) -> str:
    return str(value or "").strip().upper()
