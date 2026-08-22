"""Schema-v8 canonical instrument intelligence and capability routing."""

MIGRATION_8_SQL = """
ALTER TABLE rs_instruments ADD COLUMN instrument_subtype TEXT;
ALTER TABLE rs_instruments ADD COLUMN search_priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rs_instruments ADD COLUMN metadata_source TEXT;
ALTER TABLE rs_instruments ADD COLUMN metadata_verified_utc TEXT;
ALTER TABLE rs_instrument_aliases ADD COLUMN normalized_alias TEXT;
ALTER TABLE rs_instrument_aliases ADD COLUMN ranking_boost INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rs_instrument_aliases ADD COLUMN last_verified_utc TEXT;
ALTER TABLE rs_provider_symbols ADD COLUMN mapping_status TEXT NOT NULL DEFAULT 'verified';
ALTER TABLE rs_provider_symbols ADD COLUMN capabilities_json TEXT;
ALTER TABLE rs_provider_symbols ADD COLUMN verified_at_utc TEXT;
CREATE TABLE IF NOT EXISTS rs_instrument_capabilities (
    instrument_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    applicability TEXT NOT NULL CHECK(applicability IN ('applicable','not_applicable')),
    reason TEXT NOT NULL DEFAULT '',
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(instrument_id, capability),
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rs_provider_symbol_instrument ON rs_provider_symbols(instrument_id, provider_id, mapping_status);
"""
