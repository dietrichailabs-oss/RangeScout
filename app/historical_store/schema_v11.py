"""Schema-v11 separates traded-security identity from issuer/entity identity."""

MIGRATION_11_SQL = """
ALTER TABLE rs_instruments ADD COLUMN issuer_entity_type TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE rs_instruments ADD COLUMN security_role TEXT NOT NULL DEFAULT 'unknown';
UPDATE rs_instruments
SET issuer_entity_type = CASE
    WHEN LOWER(asset_class) = 'closed_end_fund' THEN 'closed_end_fund'
    WHEN LOWER(asset_class) IN ('etf','mutual_fund') THEN 'fund_vehicle'
    WHEN LOWER(asset_class) IN ('index','fx','crypto_spot','commodity_spot') THEN 'non_company_market_instrument'
    WHEN LOWER(asset_class) IN ('equity','stock','common_stock','preferred','adr','otc') THEN 'operating_company'
    ELSE 'unknown'
END
WHERE issuer_entity_type = 'unknown';
UPDATE rs_instruments
SET security_role = CASE
    WHEN LOWER(asset_class) = 'closed_end_fund' THEN 'primary_common'
    WHEN LOWER(asset_class) IN ('preferred','warrant','right','unit','adr','etn') THEN 'alternate_security'
    WHEN LOWER(asset_class) IN ('equity','stock','common_stock') THEN 'primary_common'
    ELSE 'other'
END
WHERE security_role = 'unknown';
CREATE INDEX IF NOT EXISTS idx_rs_instruments_issuer_entity
ON rs_instruments(issuer_entity_type, is_active);
"""