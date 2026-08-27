"""Additive transactional migrations for RangeScout's local SQLite store."""

from __future__ import annotations

import sqlite3

from app.historical_store.schema_v2 import MIGRATION_2_SQL
from app.historical_store.schema_v3 import MIGRATION_3_SQL
from app.historical_store.schema_v4 import MIGRATION_4_SQL
from app.historical_store.schema_v5 import MIGRATION_5_SQL
from app.historical_store.schema_v6 import MIGRATION_6_SQL
from app.historical_store.schema_v7 import MIGRATION_7_SQL
from app.historical_store.schema_v8 import MIGRATION_8_SQL
from app.historical_store.schema_v9 import MIGRATION_9_SQL
from app.historical_store.schema_v10 import MIGRATION_10_SQL
from app.historical_store.schema_v11 import MIGRATION_11_SQL
from app.historical_store.schema_v12 import MIGRATION_12_SQL, repair_r8_discovery_duplicates
from app.historical_store.schema_v13 import MIGRATION_13_SQL
from app.historical_store.schema_v14 import MIGRATION_14_SQL
from app.historical_store.schema_v15 import MIGRATION_15_SQL, backfill_r13_search_index
from app.historical_store.schema_v16 import MIGRATION_16_SQL, backfill_r14_optional_conjunction_index


CURRENT_SCHEMA_VERSION = 16


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
        8: MIGRATION_8_SQL,
        9: MIGRATION_9_SQL,
        10: MIGRATION_10_SQL,
        11: MIGRATION_11_SQL,
        12: MIGRATION_12_SQL,
        13: MIGRATION_13_SQL,
        14: MIGRATION_14_SQL,
        15: MIGRATION_15_SQL,
        16: MIGRATION_16_SQL,
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
                        # Additive columns in these versions are safe to regard
                        # as already applied when recovering a database whose
                        # schema-version marker was deliberately lowered.
                        if target in {5, 6, 8, 11, 14} and "duplicate column name" in str(exc).lower():
                            continue
                        raise
            if target == 12:
                repair_r8_discovery_duplicates(connection)
            if target == 15:
                backfill_r13_search_index(connection)
            if target == 16:
                backfill_r14_optional_conjunction_index(connection)
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(target),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
