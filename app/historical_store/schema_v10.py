"""Schema-v10 provider-specific support decisions and mapping provenance."""

MIGRATION_10_SQL = """
CREATE TABLE IF NOT EXISTS rs_provider_instrument_support (
    provider_id TEXT NOT NULL,
    instrument_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    support_status TEXT NOT NULL CHECK(support_status IN ('supported','unsupported','unknown')),
    reason TEXT NOT NULL DEFAULT '',
    mapping_source TEXT NOT NULL DEFAULT '',
    verified_at_utc TEXT NOT NULL,
    PRIMARY KEY(provider_id, instrument_id, capability),
    FOREIGN KEY(provider_id) REFERENCES rs_providers(provider_id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rs_provider_support_status
ON rs_provider_instrument_support(provider_id, capability, support_status);
"""
