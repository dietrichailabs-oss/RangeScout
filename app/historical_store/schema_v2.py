"""Schema-v2 DDL kept separate so migrations remain auditable."""

MIGRATION_2_SQL = """
CREATE TABLE IF NOT EXISTS rs_schema_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_instruments (
    instrument_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_symbol TEXT NOT NULL,
    security_name TEXT,
    asset_class TEXT NOT NULL,
    security_type TEXT,
    primary_venue TEXT NOT NULL DEFAULT '',
    currency TEXT,
    country_code TEXT,
    cik TEXT,
    listing_date TEXT,
    delisting_date TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    metadata_updated_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    UNIQUE(canonical_symbol, primary_venue, asset_class)
);
CREATE INDEX IF NOT EXISTS idx_rs_instruments_symbol ON rs_instruments(canonical_symbol);
CREATE INDEX IF NOT EXISTS idx_rs_instruments_active_class ON rs_instruments(is_active, asset_class);
CREATE TABLE IF NOT EXISTS rs_instrument_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id INTEGER NOT NULL,
    alias_symbol TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT '',
    alias_kind TEXT NOT NULL DEFAULT 'symbol',
    valid_from TEXT,
    valid_to TEXT,
    source_id TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    UNIQUE(instrument_id, alias_symbol, venue, alias_kind)
);
CREATE INDEX IF NOT EXISTS idx_rs_alias_lookup ON rs_instrument_aliases(alias_symbol, venue);
CREATE TABLE IF NOT EXISTS rs_providers (
    provider_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    provider_class TEXT NOT NULL,
    enablement_state TEXT NOT NULL DEFAULT 'disabled',
    requires_credentials INTEGER NOT NULL DEFAULT 0 CHECK(requires_credentials IN (0,1)),
    credential_kind TEXT,
    terms_review_state TEXT NOT NULL DEFAULT 'pending',
    terms_reviewed_utc TEXT,
    official_docs_url TEXT,
    delay_class TEXT,
    attribution_text TEXT,
    experimental INTEGER NOT NULL DEFAULT 0 CHECK(experimental IN (0,1)),
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_provider_capabilities (
    provider_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    realtime_class TEXT,
    max_symbols INTEGER,
    notes TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(provider_id, capability, asset_class, venue),
    FOREIGN KEY(provider_id) REFERENCES rs_providers(provider_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_provider_symbols (
    provider_id TEXT NOT NULL,
    instrument_id INTEGER NOT NULL,
    provider_symbol TEXT NOT NULL,
    provider_venue TEXT NOT NULL DEFAULT '',
    product_type TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    metadata_json TEXT,
    PRIMARY KEY(provider_id, provider_symbol, provider_venue, product_type),
    FOREIGN KEY(provider_id) REFERENCES rs_providers(provider_id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_provider_health (
    provider_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    venue TEXT NOT NULL DEFAULT '',
    window_started_utc TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    timeout_count INTEGER NOT NULL DEFAULT 0,
    parse_failure_count INTEGER NOT NULL DEFAULT 0,
    stale_count INTEGER NOT NULL DEFAULT 0,
    validation_failure_count INTEGER NOT NULL DEFAULT 0,
    rate_limit_count INTEGER NOT NULL DEFAULT 0,
    p50_latency_ms REAL,
    p95_latency_ms REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_success_utc TEXT,
    last_failure_utc TEXT,
    circuit_state TEXT NOT NULL DEFAULT 'closed',
    cooldown_until_utc TEXT,
    updated_at_utc TEXT NOT NULL,
    PRIMARY KEY(provider_id, capability, asset_class, venue),
    FOREIGN KEY(provider_id) REFERENCES rs_providers(provider_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_discovery_sources (
    source_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    official_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    refresh_interval_seconds INTEGER,
    last_success_utc TEXT,
    next_due_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rs_discovery_runs (
    discovery_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL,
    source_timestamp TEXT,
    source_sha256 TEXT,
    rows_seen INTEGER NOT NULL DEFAULT 0,
    before_count INTEGER NOT NULL DEFAULT 0,
    after_count INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    parse_error_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    FOREIGN KEY(source_id) REFERENCES rs_discovery_sources(source_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS rs_discovery_changes (
    discovery_change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    discovery_run_id INTEGER NOT NULL,
    instrument_id INTEGER,
    change_type TEXT NOT NULL,
    old_symbol TEXT,
    new_symbol TEXT,
    old_name TEXT,
    new_name TEXT,
    old_venue TEXT,
    new_venue TEXT,
    details_json TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(discovery_run_id) REFERENCES rs_discovery_runs(discovery_run_id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS rs_futures_contracts (
    instrument_id INTEGER PRIMARY KEY,
    root_symbol TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    exchange TEXT,
    month_code TEXT,
    contract_month INTEGER,
    contract_year INTEGER,
    expiration_date TEXT,
    contract_multiplier TEXT,
    tick_size TEXT,
    tick_value TEXT,
    session_calendar_id TEXT,
    is_continuous INTEGER NOT NULL DEFAULT 0 CHECK(is_continuous IN (0,1)),
    continuous_roll_rule TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_futures_roll_map (
    roll_map_id INTEGER PRIMARY KEY AUTOINCREMENT,
    continuous_instrument_id INTEGER NOT NULL,
    contract_instrument_id INTEGER NOT NULL,
    effective_from_utc TEXT NOT NULL,
    effective_to_utc TEXT,
    roll_reason TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(continuous_instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    FOREIGN KEY(contract_instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    UNIQUE(continuous_instrument_id, contract_instrument_id, effective_from_utc)
);
CREATE TABLE IF NOT EXISTS rs_crypto_products (
    instrument_id INTEGER PRIMARY KEY,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    venue TEXT,
    product_type TEXT NOT NULL,
    settlement_asset TEXT,
    status TEXT,
    price_precision INTEGER,
    size_precision INTEGER,
    min_order_size TEXT,
    contract_multiplier TEXT,
    expiration_date TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_option_contracts (
    instrument_id INTEGER PRIMARY KEY,
    underlying_instrument_id INTEGER NOT NULL,
    option_type TEXT NOT NULL CHECK(option_type IN ('call','put')),
    strike TEXT NOT NULL,
    expiration_date TEXT NOT NULL,
    contract_multiplier TEXT,
    venue TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    FOREIGN KEY(underlying_instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_market_data_cache (
    cache_key TEXT PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    capability TEXT NOT NULL,
    interval_key TEXT NOT NULL DEFAULT '',
    adjustment_policy TEXT NOT NULL DEFAULT '',
    provider_id TEXT,
    provider_timestamp_utc TEXT,
    received_at_utc TEXT NOT NULL,
    delay_class TEXT,
    currency TEXT,
    payload_json TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    FOREIGN KEY(provider_id) REFERENCES rs_providers(provider_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_rs_market_cache_expiry ON rs_market_data_cache(expires_at_utc);
CREATE TABLE IF NOT EXISTS rs_company_identity (
    instrument_id INTEGER PRIMARY KEY,
    cik TEXT,
    legal_name TEXT,
    sic TEXT,
    sic_description TEXT,
    fiscal_year_end TEXT,
    sec_entity_json TEXT,
    source_updated_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rs_fundamental_facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id INTEGER NOT NULL,
    taxonomy TEXT NOT NULL,
    concept TEXT NOT NULL,
    normalized_metric TEXT,
    unit TEXT NOT NULL,
    value_text TEXT NOT NULL,
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    filed_date TEXT,
    accession_number TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL,
    source_timestamp_utc TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY(instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE CASCADE,
    UNIQUE(instrument_id, taxonomy, concept, unit, period_start, period_end, accession_number, value_text)
);
CREATE TABLE IF NOT EXISTS rs_maintenance_state (
    task_id TEXT PRIMARY KEY,
    last_started_utc TEXT,
    last_completed_utc TEXT,
    last_status TEXT,
    next_due_utc TEXT,
    last_error TEXT,
    updated_at_utc TEXT NOT NULL
)
"""
