"""R13 canonical punctuation-normalized instrument search index."""

from __future__ import annotations

import sqlite3

from app.company_data.search_normalization import rebuild_instrument_search_index


MIGRATION_15_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS rs_instrument_search_fts USING fts5(
    instrument_id UNINDEXED,
    normalized_text,
    source_kind UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def backfill_r13_search_index(connection: sqlite3.Connection) -> None:
    rebuild_instrument_search_index(connection)
