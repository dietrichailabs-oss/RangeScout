"""R11 continuity, pending-removal, and explicit identity evidence state."""

MIGRATION_14_SQL = """
ALTER TABLE rs_discovery_subsource_state ADD COLUMN last_success_symbol_set_sha256 TEXT;
ALTER TABLE rs_discovery_subsource_state ADD COLUMN last_observed_symbol_set_sha256 TEXT;
ALTER TABLE rs_discovery_subsource_state ADD COLUMN pending_missing_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS rs_discovery_subsource_members (
    source_id TEXT NOT NULL,
    subsource_id TEXT NOT NULL,
    instrument_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'present',
    missing_observations INTEGER NOT NULL DEFAULT 0,
    pending_since_utc TEXT,
    last_present_utc TEXT,
    last_missing_utc TEXT,
    last_missing_source_sha256 TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(source_id, subsource_id, instrument_id),
    FOREIGN KEY(source_id) REFERENCES rs_discovery_sources(source_id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rs_discovery_members_state
    ON rs_discovery_subsource_members(source_id, subsource_id, state, missing_observations);

CREATE TABLE IF NOT EXISTS rs_instrument_identity_evidence (
    identity_evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    instrument_id INTEGER NOT NULL,
    old_symbol TEXT NOT NULL,
    new_symbol TEXT NOT NULL,
    venue TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_value TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    UNIQUE(source_id, old_symbol, new_symbol, venue, evidence_type),
    FOREIGN KEY(source_id) REFERENCES rs_discovery_sources(source_id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
"""
