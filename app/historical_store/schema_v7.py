"""Schema-v7 frozen-reference provenance for the broad company master."""

MIGRATION_7_SQL = """
CREATE TABLE IF NOT EXISTS rs_instrument_reference_sources (
    instrument_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    source_symbol TEXT,
    source_name TEXT,
    source_exchange TEXT,
    source_snapshot_sha256 TEXT NOT NULL,
    source_retrieved_utc TEXT NOT NULL,
    PRIMARY KEY(instrument_id, source_id),
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    FOREIGN KEY(source_id) REFERENCES rs_discovery_sources(source_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_rs_reference_source_symbol
    ON rs_instrument_reference_sources(source_id, source_symbol);
"""
