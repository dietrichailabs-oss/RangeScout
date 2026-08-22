"""Canonical instrument identity, ranked resolution, and reference seeding."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable, Mapping


_SPACE = re.compile(r"[^a-z0-9]+")
_SUFFIX = re.compile(r"\b(?:incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group)\b\.?,?", re.I)
_REFERENCE_VERSION = 1
_REFERENCE_TIME = "2026-08-22T00:00:00+00:00"


def normalize_search_text(value: object) -> str:
    return _SPACE.sub(" ", _SUFFIX.sub(" ", str(value or "")).lower()).strip()


@dataclass(frozen=True, slots=True)
class CanonicalInstrument:
    instrument_id: int
    symbol: str
    name: str
    venue: str
    asset_class: str
    subtype: str
    currency: str
    provider_symbols: Mapping[str, str]
    capabilities: Mapping[str, str]

    @property
    def identity(self) -> str:
        return f"instrument:{self.instrument_id}"


@dataclass(frozen=True, slots=True)
class InstrumentMatch:
    instrument: CanonicalInstrument
    score: int
    match_kind: str
    matched_text: str

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def name(self) -> str:
        return self.instrument.name

    @property
    def exchange(self) -> str:
        return self.instrument.venue

    @property
    def asset_type(self) -> str:
        return (self.instrument.subtype or self.instrument.asset_class).replace("_", " ").title()

    @property
    def display_text(self) -> str:
        return f"{self.symbol}  ·  {self.name}  ·  {self.exchange}  ·  {self.asset_type}"


_REFERENCE_INSTRUMENTS = (
    ("^DJI", "Dow Jones Industrial Average", "index", "broad_market_index", "INDEX", "USD", 100,
     ("DJIA", "Dow", "Dow 30", "Dow Jones"), {"yahoo": "^DJI"}),
    ("^GSPC", "S&P 500 Index", "index", "broad_market_index", "INDEX", "USD", 100,
     ("S&P 500", "SP500", "SPX", "S&P500"), {"yahoo": "^GSPC"}),
    ("^IXIC", "Nasdaq Composite Index", "index", "broad_market_index", "INDEX", "USD", 95,
     ("Nasdaq", "Nasdaq Composite"), {"yahoo": "^IXIC"}),
    ("XAU/USD", "Gold Spot / U.S. Dollar", "commodity_spot", "precious_metal_spot", "OTC SPOT", "USD", 100,
     ("Gold", "Gold Spot", "XAU", "XAUUSD", "XAU/USD"), {"twelve_data": "XAU/USD"}),
    ("BTC/USD", "Bitcoin / U.S. Dollar", "crypto_spot", "crypto_spot_pair", "MULTI", "USD", 90,
     ("Bitcoin", "BTC", "BTCUSD", "BTC-USD"), {"coinbase": "BTC-USD", "kraken": "XBTUSD", "yahoo": "BTC-USD"}),
)
_REFERENCE_ALIASES = (
    ("AAPL", "NASDAQ", "Apple", "company_name"),
    ("AAPL", "NASDAQ", "Apple Inc", "company_name"),
    ("MSFT", "NASDAQ", "Microsoft", "company_name"),
    ("MSFT", "NASDAQ", "Microsoft Corporation", "company_name"),
    ("BOE", "NYSE", "BlackRock Enhanced", "fund_name"),
    ("BOE", "NYSE", "BlackRock Enhanced Global Dividend Trust", "fund_name"),
)
_DEFAULT_CAPABILITIES = {
    "quote": "applicable", "historical": "applicable", "fundamentals": "not_applicable", "news": "applicable"
}


class InstrumentReferenceSeeder:
    """Idempotently installs factual reference metadata; never stores live values."""

    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)

    def apply(self) -> int:
        with closing(sqlite3.connect(self.path, timeout=15)) as con:
            con.row_factory = sqlite3.Row
            current = con.execute("SELECT value FROM rs_schema_meta WHERE key='instrument_reference_version'").fetchone()
            if current and int(current[0]) >= _REFERENCE_VERSION:
                return 0
            con.execute("BEGIN IMMEDIATE")
            changed_before = con.total_changes
            for provider_id in ("yahoo", "twelve_data", "coinbase", "kraken"):
                con.execute(
                    """INSERT OR IGNORE INTO rs_providers(
                       provider_id,display_name,provider_class,enablement_state,requires_credentials,
                       terms_review_state,created_at_utc,updated_at_utc)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (provider_id, provider_id.replace("_", " ").title(), "reference_mapping", "runtime_controlled", 0,
                     "reviewed", _REFERENCE_TIME, _REFERENCE_TIME),
                )
            for symbol, name, asset, subtype, venue, currency, priority, aliases, mappings in _REFERENCE_INSTRUMENTS:
                con.execute(
                    """INSERT INTO rs_instruments(
                       canonical_symbol,security_name,asset_class,security_type,primary_venue,currency,is_active,
                       first_seen_utc,last_seen_utc,metadata_updated_utc,created_at_utc,updated_at_utc,
                       instrument_subtype,search_priority,metadata_source,metadata_verified_utc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(canonical_symbol,primary_venue,asset_class) DO UPDATE SET
                         security_name=excluded.security_name,instrument_subtype=excluded.instrument_subtype,
                         search_priority=excluded.search_priority,metadata_source=excluded.metadata_source,
                         metadata_verified_utc=excluded.metadata_verified_utc,updated_at_utc=excluded.updated_at_utc""",
                    (symbol, name, asset, subtype, venue, currency, 1, _REFERENCE_TIME, _REFERENCE_TIME,
                     _REFERENCE_TIME, _REFERENCE_TIME, _REFERENCE_TIME, subtype, priority,
                     "rangescout_public_reference", _REFERENCE_TIME),
                )
                row = con.execute(
                    "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=? AND primary_venue=? AND asset_class=?",
                    (symbol, venue, asset),
                ).fetchone()
                instrument_id = int(row[0])
                for alias in aliases:
                    self._insert_alias(con, instrument_id, alias, venue, "reference_alias", 50)
                for provider_id, provider_symbol in mappings.items():
                    con.execute(
                        """INSERT OR REPLACE INTO rs_provider_symbols(
                           provider_id,instrument_id,provider_symbol,provider_venue,product_type,is_active,
                           first_seen_utc,last_seen_utc,metadata_json,mapping_status,capabilities_json,verified_at_utc)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (provider_id, instrument_id, provider_symbol, "", subtype, 1, _REFERENCE_TIME, _REFERENCE_TIME,
                         json.dumps({"source": "RangeScout public reference"}, sort_keys=True), "verified",
                         json.dumps(sorted(k for k, v in _DEFAULT_CAPABILITIES.items() if v == "applicable")), _REFERENCE_TIME),
                    )
                for capability, applicability in _DEFAULT_CAPABILITIES.items():
                    con.execute(
                        "INSERT OR REPLACE INTO rs_instrument_capabilities VALUES(?,?,?,?,?)",
                        (instrument_id, capability, applicability,
                         "SEC corporate fundamentals do not apply to this instrument type." if applicability == "not_applicable" else "",
                         _REFERENCE_TIME),
                    )
            for symbol, venue, alias, kind in _REFERENCE_ALIASES:
                rows = con.execute(
                    "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=? AND primary_venue=? ORDER BY is_active DESC",
                    (symbol, venue),
                ).fetchall()
                for row in rows[:1]:
                    self._insert_alias(con, int(row[0]), alias, venue, kind, 35)
            con.execute(
                """UPDATE rs_instruments SET instrument_subtype='closed_end_fund', search_priority=40,
                   metadata_source='official_listing_name_classification', metadata_verified_utc=?
                   WHERE canonical_symbol='BOE' AND primary_venue='NYSE'""", (_REFERENCE_TIME,)
            )
            con.execute(
                "INSERT OR REPLACE INTO rs_schema_meta(key,value,updated_at_utc) VALUES('instrument_reference_version',?,?)",
                (str(_REFERENCE_VERSION), _REFERENCE_TIME),
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_rs_instrument_name_search ON rs_instruments(security_name, is_active, search_priority)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_rs_alias_normalized_search ON rs_instrument_aliases(normalized_alias, ranking_boost)")
            changed = con.total_changes - changed_before
            con.commit()
            return changed

    @staticmethod
    def _insert_alias(con: sqlite3.Connection, instrument_id: int, alias: str, venue: str, kind: str, boost: int) -> None:
        con.execute(
            """INSERT INTO rs_instrument_aliases(
               instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc,
               normalized_alias,ranking_boost,last_verified_utc)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(instrument_id,alias_symbol,venue,alias_kind) DO UPDATE SET
                 normalized_alias=excluded.normalized_alias,ranking_boost=excluded.ranking_boost,
                 last_verified_utc=excluded.last_verified_utc""",
            (instrument_id, alias, venue, kind, "rangescout_public_reference", _REFERENCE_TIME,
             normalize_search_text(alias), boost, _REFERENCE_TIME),
        )


class InstrumentResolver:
    """Local-first market-instrument resolver with provider-enrichment support."""

    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path)

    def search(self, query: str, limit: int = 12) -> list[InstrumentMatch]:
        raw = str(query or "").strip()
        if len(raw) < 2 or len(raw) > 160:
            return []
        upper, normalized = raw.upper(), normalize_search_text(raw)
        token = f"%{raw}%"
        with closing(sqlite3.connect(self.path, timeout=10)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT i.*,GROUP_CONCAT(a.alias_symbol,'|') aliases,
                          GROUP_CONCAT(COALESCE(a.normalized_alias,''),'|') normalized_aliases,
                          MAX(COALESCE(a.ranking_boost,0)) alias_boost
                   FROM rs_instruments i LEFT JOIN rs_instrument_aliases a ON a.instrument_id=i.instrument_id
                   WHERE UPPER(i.canonical_symbol)=? OR UPPER(COALESCE(a.alias_symbol,''))=?
                      OR i.security_name LIKE ? COLLATE NOCASE OR i.canonical_symbol LIKE ? COLLATE NOCASE
                      OR COALESCE(a.alias_symbol,'') LIKE ? COLLATE NOCASE
                   GROUP BY i.instrument_id ORDER BY i.is_active DESC,i.search_priority DESC LIMIT 350""",
                (upper, upper, token, f"{raw}%", f"{raw}%"),
            ).fetchall()
            matches = [self._match(con, row, raw, upper, normalized) for row in rows]
        ranked = [item for item in matches if item.score > 0]
        ranked.sort(key=lambda item: (-item.score, -item.instrument.instrument_id, item.symbol, item.exchange))
        return ranked[:max(1, min(50, int(limit)))]

    def resolve_unique(self, query: str) -> InstrumentMatch | None:
        results = self.search(query, 10)
        if not results:
            return None
        first, second = results[0], results[1] if len(results) > 1 else None
        if first.match_kind in {"exact_symbol", "exact_alias"}:
            return first
        if first.score >= 930 and (second is None or first.score - second.score >= 70):
            return first
        if first.score >= 760 and (second is None or first.score - second.score >= 150):
            return first
        return None

    def by_id(self, instrument_id: int) -> CanonicalInstrument | None:
        with closing(sqlite3.connect(self.path, timeout=10)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM rs_instruments WHERE instrument_id=?", (int(instrument_id),)).fetchone()
            return self._instrument(con, row) if row else None

    def enrich_provider_results(self, provider_id: str, rows: Iterable[Mapping[str, object]]) -> int:
        """Normalize/cache eligible provider discovery results; no request is made here."""
        changed = 0
        with closing(sqlite3.connect(self.path, timeout=15)) as con:
            con.execute("BEGIN IMMEDIATE")
            for item in rows:
                symbol = str(item.get("canonical_symbol") or item.get("symbol") or "").strip().upper()
                name = str(item.get("name") or symbol).strip()
                asset = str(item.get("asset_class") or "unknown").strip().lower()
                venue = str(item.get("venue") or "").strip().upper()
                provider_symbol = str(item.get("provider_symbol") or symbol).strip()
                if not symbol or not provider_symbol:
                    continue
                now = str(item.get("verified_at_utc") or _REFERENCE_TIME)
                con.execute(
                    """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,primary_venue,is_active,
                       first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc,metadata_source,metadata_verified_utc)
                       VALUES(?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(canonical_symbol,primary_venue,asset_class)
                       DO UPDATE SET security_name=excluded.security_name,last_seen_utc=excluded.last_seen_utc,
                       metadata_source=excluded.metadata_source,metadata_verified_utc=excluded.metadata_verified_utc""",
                    (symbol, name, asset, venue, now, now, now, now, f"provider_discovery:{provider_id}", now),
                )
                instrument_id = con.execute(
                    "SELECT instrument_id FROM rs_instruments WHERE canonical_symbol=? AND primary_venue=? AND asset_class=?",
                    (symbol, venue, asset),
                ).fetchone()[0]
                before = con.total_changes
                con.execute(
                    """INSERT OR REPLACE INTO rs_provider_symbols(provider_id,instrument_id,provider_symbol,
                       provider_venue,product_type,is_active,first_seen_utc,last_seen_utc,mapping_status,verified_at_utc)
                       VALUES(?,?,?,?,?,1,?,?,?,?)""",
                    (provider_id, instrument_id, provider_symbol, venue, str(item.get("instrument_type") or ""),
                     now, now, "discovered", now),
                )
                changed += con.total_changes - before
            con.commit()
        return changed

    def _match(self, con: sqlite3.Connection, row: sqlite3.Row, raw: str, upper: str, normalized: str) -> InstrumentMatch:
        symbol, name = str(row["canonical_symbol"]).upper(), str(row["security_name"] or row["canonical_symbol"])
        aliases = [value for value in str(row["aliases"] or "").split("|") if value]
        alias_upper = {value.upper() for value in aliases}
        name_norm = normalize_search_text(name)
        priority = int(row["search_priority"] or 0) + int(row["alias_boost"] or 0)
        if upper == symbol:
            score, kind, matched = 1200, "exact_symbol", symbol
        elif upper in alias_upper:
            score, kind, matched = 1140, "exact_alias", next(v for v in aliases if v.upper() == upper)
        elif raw.casefold() == name.casefold():
            score, kind, matched = 1080, "exact_name", name
        elif normalized and normalized == name_norm:
            score, kind, matched = 1020, "normalized_name", name
        elif normalized and name_norm.startswith(normalized):
            score, kind, matched = 860 - min(100, len(name_norm) - len(normalized)), "name_prefix", name
        elif symbol.startswith(upper):
            score, kind, matched = 790 - min(100, len(symbol) - len(upper)), "symbol_prefix", symbol
        elif normalized and normalized in name_norm:
            score, kind, matched = 680 - min(120, name_norm.index(normalized)), "name_contains", name
        else:
            ratio = SequenceMatcher(None, normalized, name_norm).ratio() if len(normalized) >= 4 else 0
            score, kind, matched = (int(560 * ratio), "fuzzy", name) if ratio >= .86 else (0, "none", "")
        return InstrumentMatch(self._instrument(con, row), score + priority + (5 if row["is_active"] else 0), kind, matched)

    @staticmethod
    def _instrument(con: sqlite3.Connection, row: sqlite3.Row) -> CanonicalInstrument:
        instrument_id = int(row["instrument_id"])
        mappings = {str(r[0]): str(r[1]) for r in con.execute(
            "SELECT provider_id,provider_symbol FROM rs_provider_symbols WHERE instrument_id=? AND is_active=1 AND mapping_status!='disabled'",
            (instrument_id,),
        )}
        capabilities = {str(r[0]): str(r[1]) for r in con.execute(
            "SELECT capability,applicability FROM rs_instrument_capabilities WHERE instrument_id=?", (instrument_id,)
        )}
        return CanonicalInstrument(instrument_id, str(row["canonical_symbol"]).upper(),
            str(row["security_name"] or row["canonical_symbol"]), str(row["primary_venue"] or "N/A"),
            str(row["asset_class"] or "unknown"), str(row["instrument_subtype"] or row["security_type"] or ""),
            str(row["currency"] or "USD"), mappings, capabilities)
