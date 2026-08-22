"""Schema-v3 additions for company-logo resolution metadata.

Logo image bytes are intentionally excluded. The table exists to prevent repeat
failed lookups and to preserve provenance/health metadata without turning
RangeScout into a logo mirror.
"""

MIGRATION_3_SQL = """
CREATE TABLE IF NOT EXISTS rs_company_logo_state (
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT '',
    provider_id TEXT NOT NULL,
    status TEXT NOT NULL,
    last_attempt_utc TEXT NOT NULL,
    last_success_utc TEXT,
    retry_after_utc TEXT,
    content_type TEXT,
    content_sha256 TEXT,
    error_code TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(symbol, exchange, provider_id)
);
CREATE INDEX IF NOT EXISTS idx_rs_company_logo_retry
    ON rs_company_logo_state(provider_id, retry_after_utc);
"""
