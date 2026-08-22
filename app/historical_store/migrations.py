"""Additive transactional migrations for RangeScout's local SQLite store."""

from __future__ import annotations

import sqlite3

from app.historical_store.schema_v2 import MIGRATION_2_SQL
from app.historical_store.schema_v3 import MIGRATION_3_SQL
from app.historical_store.schema_v4 import MIGRATION_4_SQL
from app.historical_store.schema_v5 import MIGRATION_5_SQL
from app.historical_store.schema_v6 import MIGRATION_6_SQL
from app.historical_store.schema_v7 import MIGRATION_7_SQL


CURRENT_SCHEMA_VERSION = 7


def current_schema_version() -> int:
    return CURRENT_SCHEMA_VERSION


def apply_migrations(connection: sqlite3.Connection, existing_version: int) -> None:
    if existing_version > CURRENT_SCHEMA_VERSION:
        raise ValueError("Unknown newer DB schema version.")
    migrations = {
        2: MIGRATION_2_SQL,
        3: MIGRATION_3_SQL,
        4: MIGRATION_4_SQL,
        5: MIGRATION_5_SQL,
        6: MIGRATION_6_SQL,
        7: MIGRATION_7_SQL,
    }
    for target in range(existing_version + 1, CURRENT_SCHEMA_VERSION + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            for raw in migrations.get(target, "").split(";"):
                statement = raw.strip()
                if statement:
                    try:
                        connection.execute(statement)
                    except sqlite3.OperationalError as exc:
                        # Tests and field-recovery tools can deliberately lower
                        # meta.schema_version on an otherwise newer database.
                        # Additive v5 columns are safe to regard as already applied.
                        if target in {5, 6} and "duplicate column name" in str(exc).lower():
                            continue
                        raise
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(target),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
