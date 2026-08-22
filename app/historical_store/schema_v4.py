"""Schema-v4 additions for optional analyst-data response caching."""

MIGRATION_4_SQL = """
CREATE TABLE IF NOT EXISTS rs_analyst_cache (
    provider_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    dataset TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    provider_timestamp_utc TEXT,
    retrieved_at_utc TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    PRIMARY KEY (provider_id, symbol, dataset)
);
CREATE INDEX IF NOT EXISTS idx_rs_analyst_cache_expiry
    ON rs_analyst_cache(provider_id, expires_at_utc);
"""
