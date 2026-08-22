"""Schema-v5 company identity, logo provenance, and maintenance additions."""

MIGRATION_5_SQL = """
ALTER TABLE rs_instruments ADD COLUMN logo_source_id TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_lookup_identifier TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_source_url TEXT;
ALTER TABLE rs_instruments ADD COLUMN local_logo_path TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_content_sha256 TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_license_metadata TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_last_checked_utc TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_last_success_utc TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_next_refresh_utc TEXT;
ALTER TABLE rs_instruments ADD COLUMN logo_failure_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rs_instruments ADD COLUMN logo_last_error TEXT;
CREATE INDEX IF NOT EXISTS idx_rs_instruments_logo_refresh
    ON rs_instruments(is_active, logo_next_refresh_utc);
CREATE TABLE IF NOT EXISTS rs_company_update_runs (
    update_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_kind TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL,
    before_count INTEGER NOT NULL DEFAULT 0,
    after_count INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    inactive_count INTEGER NOT NULL DEFAULT 0,
    alias_change_count INTEGER NOT NULL DEFAULT 0,
    logo_success_count INTEGER NOT NULL DEFAULT 0,
    logo_failure_count INTEGER NOT NULL DEFAULT 0,
    source_failure_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_rs_company_update_runs_completed
    ON rs_company_update_runs(completed_at_utc DESC);
"""
