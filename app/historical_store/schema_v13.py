"""R10 per-subsource discovery completeness state."""

MIGRATION_13_SQL = """
CREATE TABLE IF NOT EXISTS rs_discovery_subsource_state (
    source_id TEXT NOT NULL,
    subsource_id TEXT NOT NULL,
    last_success_count INTEGER,
    last_success_sha256 TEXT,
    last_success_utc TEXT,
    last_observed_count INTEGER,
    last_status TEXT NOT NULL DEFAULT 'never_run',
    last_error TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(source_id, subsource_id),
    FOREIGN KEY(source_id) REFERENCES rs_discovery_sources(source_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rs_discovery_subsource_status
    ON rs_discovery_subsource_state(source_id, last_status, updated_at_utc);
"""
