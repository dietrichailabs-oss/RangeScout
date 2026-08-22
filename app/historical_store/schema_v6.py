"""Schema-v6 local-first snapshot, quote-cache, and lookup indexes."""

MIGRATION_6_SQL = """
ALTER TABLE rs_instruments ADD COLUMN mic_code TEXT;
ALTER TABLE rs_instruments ADD COLUMN sector TEXT;
ALTER TABLE rs_instruments ADD COLUMN industry TEXT;
ALTER TABLE rs_instruments ADD COLUMN website_domain TEXT;
CREATE INDEX IF NOT EXISTS idx_rs_instruments_name
    ON rs_instruments(security_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_rs_instruments_cik
    ON rs_instruments(cik);
CREATE INDEX IF NOT EXISTS idx_rs_instruments_mic
    ON rs_instruments(mic_code, is_active);
CREATE INDEX IF NOT EXISTS idx_rs_provider_symbols_instrument
    ON rs_provider_symbols(instrument_id, provider_id, is_active);
CREATE TABLE IF NOT EXISTS rs_last_quotes (
    instrument_id INTEGER PRIMARY KEY,
    last_price TEXT NOT NULL,
    previous_close TEXT,
    volume INTEGER,
    currency TEXT NOT NULL DEFAULT 'USD',
    session_label TEXT,
    provider_id TEXT NOT NULL,
    provider_timestamp_utc TEXT,
    received_at_utc TEXT NOT NULL,
    delay_label TEXT NOT NULL,
    source_timezone TEXT NOT NULL DEFAULT 'UTC',
    day_low TEXT,
    day_high TEXT,
    fifty_two_week_low TEXT,
    fifty_two_week_high TEXT,
    average_volume INTEGER,
    market_cap TEXT,
    pre_market_price TEXT,
    pre_market_change TEXT,
    pre_market_change_percent TEXT,
    after_hours_price TEXT,
    after_hours_change TEXT,
    after_hours_change_percent TEXT,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rs_last_quotes_received
    ON rs_last_quotes(received_at_utc DESC);
"""
