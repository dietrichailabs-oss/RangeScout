"""Thread-safe, bounded SQLite persistence for optional analyst datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from contextlib import contextmanager
from typing import Iterator


MAX_ANALYST_PAYLOAD_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class AnalystCacheEntry:
    provider_id: str
    symbol: str
    dataset: str
    payload: dict[str, Any]
    provider_timestamp_utc: str | None
    retrieved_at_utc: datetime
    expires_at_utc: datetime
    status: str

    @property
    def stale(self) -> bool:
        return self.expires_at_utc <= datetime.now(timezone.utc)


class AnalystCacheRepository:
    """Uses short-lived connections so worker threads never share a SQLite handle."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def get(self, provider_id: str, symbol: str, dataset: str) -> AnalystCacheEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider_id, symbol, dataset, payload_json, provider_timestamp_utc,
                       retrieved_at_utc, expires_at_utc, status
                  FROM rs_analyst_cache
                 WHERE provider_id=? AND symbol=? AND dataset=?
                """,
                (self._provider(provider_id), self._symbol(symbol), self._dataset(dataset)),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[3])
            if not isinstance(payload, dict):
                return None
            retrieved = self._datetime(row[5])
            expires = self._datetime(row[6])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return AnalystCacheEntry(row[0], row[1], row[2], payload, row[4], retrieved, expires, row[7])

    def put(
        self,
        provider_id: str,
        symbol: str,
        dataset: str,
        payload: dict[str, Any],
        *,
        retrieved_at_utc: datetime,
        expires_at_utc: datetime,
        provider_timestamp_utc: str | None = None,
        status: str = "ok",
    ) -> None:
        rendered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        if len(rendered.encode("utf-8")) > MAX_ANALYST_PAYLOAD_BYTES:
            raise ValueError("Analyst cache payload exceeds the safety limit.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO rs_analyst_cache(
                    provider_id, symbol, dataset, payload_json, provider_timestamp_utc,
                    retrieved_at_utc, expires_at_utc, status
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_id, symbol, dataset) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    provider_timestamp_utc=excluded.provider_timestamp_utc,
                    retrieved_at_utc=excluded.retrieved_at_utc,
                    expires_at_utc=excluded.expires_at_utc,
                    status=excluded.status
                """,
                (
                    self._provider(provider_id), self._symbol(symbol), self._dataset(dataset), rendered,
                    provider_timestamp_utc, self._utc(retrieved_at_utc), self._utc(expires_at_utc), str(status)[:64],
                ),
            )
            connection.commit()

    def clear(self, provider_id: str | None = None) -> None:
        with self._connect() as connection:
            if provider_id is None:
                connection.execute("DELETE FROM rs_analyst_cache")
            else:
                connection.execute("DELETE FROM rs_analyst_cache WHERE provider_id=?", (self._provider(provider_id),))
            connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _provider(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"finnhub", "alpha_vantage"}:
            raise ValueError("Unsupported analyst provider.")
        return normalized

    @staticmethod
    def _symbol(value: str) -> str:
        normalized = str(value).strip().upper()
        if not normalized or len(normalized) > 32 or not all(ch.isalnum() or ch in ".-" for ch in normalized):
            raise ValueError("Invalid analyst symbol.")
        return normalized

    @staticmethod
    def _dataset(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"recommendation_trends", "earnings_estimates"}:
            raise ValueError("Unsupported analyst dataset.")
        return normalized

    @staticmethod
    def _utc(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("Analyst cache timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Cached timestamp is not timezone-aware.")
        return parsed.astimezone(timezone.utc)
