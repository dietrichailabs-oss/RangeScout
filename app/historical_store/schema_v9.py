"""Schema-v9 authoritative instrument-classification provenance."""

MIGRATION_9_SQL = """
CREATE TABLE IF NOT EXISTS rs_instrument_classifications (
    instrument_id INTEGER NOT NULL,
    asset_class TEXT NOT NULL,
    instrument_subtype TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    authority_level TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_value TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    verified_at_utc TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    PRIMARY KEY(instrument_id, source_id, evidence_type),
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rs_instrument_classification_active
ON rs_instrument_classifications(instrument_id, is_active, authority_level);
"""
