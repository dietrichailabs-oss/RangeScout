"""R14 optional-conjunction search-document backfill."""

from __future__ import annotations

import sqlite3

from app.company_data.search_normalization import rebuild_instrument_search_index


MIGRATION_16_SQL = ""


def backfill_r14_optional_conjunction_index(connection: sqlite3.Connection) -> None:
    """Transactionally replace R13 FTS documents with R14 variants."""

    rebuild_instrument_search_index(connection)
    tables = {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "rs_schema_meta" in tables:
        connection.execute(
            "INSERT OR REPLACE INTO rs_schema_meta(key,value,updated_at_utc) "
            "VALUES('instrument_search_document_version','2',CURRENT_TIMESTAMP)"
        )
