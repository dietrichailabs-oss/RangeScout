"""SQLite-backed historical cache and retrieval layer."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
import sqlite3
from pathlib import Path
from typing import Any
from datetime import datetime
from decimal import Decimal

from app.domain.errors import DataQualityError
from app.models.schemas import InstrumentIdentifier, OhlcvBar
from app.normalization.normalize import normalize_histories
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION, apply_migrations


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT NOT NULL,
    exchange TEXT,
    provider TEXT NOT NULL,
    currency TEXT,
    PRIMARY KEY (symbol, provider)
);

CREATE TABLE IF NOT EXISTS ohlcv_bars (
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    bar_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    adjusted INTEGER NOT NULL DEFAULT 0,
    exchange TEXT,
    source TEXT,
    provider_timestamp TEXT,
    source_timezone TEXT,
    PRIMARY KEY (symbol, provider, bar_date),
    FOREIGN KEY (symbol, provider) REFERENCES instruments(symbol, provider)
);
"""


class HistoricalStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.path)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA busy_timeout = 5000")
        self._con.execute("PRAGMA journal_mode = WAL")
        self._con.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def __enter__(self) -> "HistoricalStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _migrate(self) -> None:
        c = self._con.cursor()
        c.executescript(SCHEMA)
        c.execute("SELECT value FROM meta WHERE key='schema_version'")
        existing = c.fetchone()
        if existing is None:
            c.execute("INSERT INTO meta VALUES('schema_version', '1')")
            self._con.commit()
            existing_version = 1
        else:
            existing_version = int(existing["value"])
        try:
            apply_migrations(self._con, existing_version)
        except ValueError as exc:
            raise DataQualityError(str(exc)) from exc

    def database_checks(self) -> dict[str, object]:
        integrity = self._con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [tuple(row) for row in self._con.execute("PRAGMA foreign_key_check").fetchall()]
        return {"integrity_check": integrity, "foreign_key_violations": foreign_keys}

    def upsert_bars(self, bars: Sequence[OhlcvBar], provider: str) -> None:
        if not bars:
            return
        normalized, validation = normalize_histories(bars)
        if not normalized:
            raise DataQualityError("No valid bars to upsert.")
        first = normalized[0]
        c = self._con.cursor()
        c.execute(
            "INSERT OR IGNORE INTO instruments(symbol, exchange, provider, currency) VALUES (?,?,?,?)",
            (first.instrument.symbol, None, provider, "USD"),
        )
        for bar in normalized:
            c.execute(
                """
                INSERT OR REPLACE INTO ohlcv_bars(
                    symbol, provider, bar_date, open, high, low, close, volume, adjusted, exchange, source, provider_timestamp, source_timezone
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    bar.instrument.symbol,
                    provider,
                    bar.date.isoformat(),
                    float(bar.open),
                    float(bar.high),
                    float(bar.low),
                    float(bar.close),
                    bar.volume,
                    int(bar.adjusted),
                    bar.instrument.exchange,
                    bar.source,
                    bar.provider_timestamp.isoformat() if bar.provider_timestamp else None,
                    bar.source_timezone,
                ),
            )
        self._con.commit()

    def close(self) -> None:
        self._con.close()

    def get_bars(
        self,
        identifier: InstrumentIdentifier,
        provider: str,
        start: date | None = None,
        end: date | None = None,
        adjusted: bool | None = None,
    ) -> list[OhlcvBar]:
        query = """
            SELECT b.*, i.currency
            FROM ohlcv_bars b
            JOIN instruments i ON i.symbol=b.symbol AND i.provider=b.provider
            WHERE b.symbol = ? AND b.provider = ?
        """
        params: list[Any] = [identifier.symbol, provider]
        if start is not None:
            query += " AND bar_date >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND bar_date <= ?"
            params.append(end.isoformat())
        if adjusted is not None:
            query += " AND adjusted = ?"
            params.append(1 if adjusted else 0)
        query += " ORDER BY bar_date ASC"

        rows = self._con.execute(query, params).fetchall()
        return [
            OhlcvBar(
                instrument=identifier,
                date=date.fromisoformat(row["bar_date"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=row["volume"],
                provider=row["provider"],
                adjusted=bool(row["adjusted"]),
                source=row["source"],
                provider_timestamp=(
                    datetime.fromisoformat(row["provider_timestamp"]) if row["provider_timestamp"] else None
                ),
                source_timezone=row["source_timezone"] or "UTC",
            )
            for row in rows
        ]

    def get_recent_bars_any_provider(self, symbol: str, *, limit: int = 365) -> list[OhlcvBar]:
        """Return one provider's newest locally cached regular-session bars."""
        normalized = str(symbol).strip().upper()
        provider_row = self._con.execute(
            """SELECT provider,MAX(bar_date) AS newest FROM ohlcv_bars
               WHERE symbol=? GROUP BY provider ORDER BY newest DESC LIMIT 1""",
            (normalized,),
        ).fetchone()
        if provider_row is None:
            return []
        rows = self._con.execute(
            """SELECT b.*,i.currency FROM ohlcv_bars b JOIN instruments i
               ON i.symbol=b.symbol AND i.provider=b.provider
               WHERE b.symbol=? AND b.provider=? ORDER BY b.bar_date DESC LIMIT ?""",
            (normalized, provider_row["provider"], max(1, min(3650, int(limit)))),
        ).fetchall()
        return [
            OhlcvBar(
                instrument=InstrumentIdentifier(normalized, row["exchange"]), date=date.fromisoformat(row["bar_date"]),
                open=Decimal(row["open"]), high=Decimal(row["high"]), low=Decimal(row["low"]), close=Decimal(row["close"]),
                volume=row["volume"], provider=row["provider"], adjusted=bool(row["adjusted"]), source=row["source"],
                provider_timestamp=datetime.fromisoformat(row["provider_timestamp"]) if row["provider_timestamp"] else None,
                source_timezone=row["source_timezone"] or "UTC",
            )
            for row in reversed(rows)
        ]
