"""Transactional official-directory discovery and weekly scheduling."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Callable, Iterable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.market_data.contracts import AssetClass
from app.market_data.instruments import DiscoveredInstrument
from app.instruments.security_classification import classify_official_security
from app.company_data.instrument_intelligence import (
    canonical_asset_class,
    classify_security_role,
    default_issuer_entity_type,
)
from app.market_data.provider_symbols import derive_yahoo_provider_symbol
from app.company_data.search_normalization import normalize_search_text, rebuild_instrument_search_index


WEEK_SECONDS = 7 * 24 * 60 * 60
RETRY_SECONDS = 6 * 60 * 60
MIN_PLAUSIBLE_ROWS = 1_000
MAX_SAFE_RELATIVE_DROP = 0.15
MAX_SAFE_ABSOLUTE_DROP = 250
MAX_SINGLE_SNAPSHOT_PENDING = 25
MISSING_CONFIRMATIONS_REQUIRED = 2

_EXPECTED_HEADERS = {
    "nasdaqlisted": ("Symbol", "Security Name", "Market Category", "Test Issue", "Financial Status", "Round Lot Size", "ETF", "NextShares"),
    "otherlisted": ("ACT Symbol", "Security Name", "Exchange", "CQS Symbol", "ETF", "Round Lot Size", "Test Issue", "NASDAQ Symbol"),
}

_VENUE_NORMALIZATION = {
    "Q": ("NASDAQ", "XNAS"), "NASDAQ": ("NASDAQ", "XNAS"), "XNAS": ("NASDAQ", "XNAS"),
    "N": ("NYSE", "XNYS"), "NYSE": ("NYSE", "XNYS"), "XNYS": ("NYSE", "XNYS"),
    "P": ("NYSE Arca", "ARCX"), "NYSE ARCA": ("NYSE Arca", "ARCX"), "ARCX": ("NYSE Arca", "ARCX"),
    "A": ("NYSE American", "XASE"), "NYSE AMERICAN": ("NYSE American", "XASE"), "XASE": ("NYSE American", "XASE"),
    "Z": ("Cboe BZX", "BATS"), "CBOE BZX": ("Cboe BZX", "BATS"), "BATS": ("Cboe BZX", "BATS"),
    "V": ("IEX", "IEXG"), "IEX": ("IEX", "IEXG"), "IEXG": ("IEX", "IEXG"),
}


def normalize_listing_venue(value: object) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    return _VENUE_NORMALIZATION.get(raw.upper(), (raw, None))


def normalize_listing_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _subtype_for_asset(asset: str) -> str:
    return {
        "equity": "common_stock", "preferred": "preferred_stock", "adr": "depositary_share",
        "warrant": "warrant", "right": "right", "unit": "unit",
        "etf": "exchange_traded_fund", "etn": "exchange_traded_note",
        "closed_end_fund": "closed_end_fund",
    }.get(asset, asset)


def _persist_official_aliases(connection, instrument_id: int, item: DiscoveredInstrument, source_id: str, stamp: str) -> None:
    aliases = item.official_aliases or ((item.provider_symbol or item.canonical_symbol, "source_symbol"),)
    for alias, kind in aliases:
        connection.execute(
            """INSERT INTO rs_instrument_aliases(
               instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc,
               normalized_alias,ranking_boost,last_verified_utc)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(instrument_id,alias_symbol,venue,alias_kind) DO UPDATE SET
                 source_id=excluded.source_id,last_verified_utc=excluded.last_verified_utc""",
            (instrument_id, alias, normalize_listing_venue(item.primary_venue)[0], kind, source_id, stamp,
             normalize_search_text(alias), 0, stamp),
        )


def _enrich_discovered_instrument(connection, instrument_id: int, item: DiscoveredInstrument, source_id: str, stamp: str) -> None:
    """Populate operational semantics and provider support without a restart."""
    _persist_official_aliases(connection, instrument_id, item, source_id, stamp)
    row = connection.execute(
        """SELECT canonical_symbol,security_name,asset_class,security_type,instrument_subtype,
                  issuer_entity_type,security_role,metadata_source
           FROM rs_instruments WHERE instrument_id=?""", (instrument_id,),
    ).fetchone()
    asset = canonical_asset_class(row["asset_class"], row["instrument_subtype"], row["security_type"], row["security_name"])
    subtype = str(row["instrument_subtype"] or _subtype_for_asset(asset))
    issuer = str(row["issuer_entity_type"] or "unknown")
    if issuer == "unknown":
        issuer = default_issuer_entity_type(asset, row["security_name"])
    role = str(row["security_role"] or "unknown")
    if role == "unknown":
        role = classify_security_role(asset, subtype, str(row["security_type"] or ""),
                                      str(row["canonical_symbol"]), str(row["security_name"] or ""), issuer)
    connection.execute(
        """UPDATE rs_instruments SET asset_class=?,instrument_subtype=?,issuer_entity_type=?,
           security_role=?,metadata_source=COALESCE(metadata_source,?),
           metadata_verified_utc=COALESCE(metadata_verified_utc,?),updated_at_utc=? WHERE instrument_id=?""",
        (asset, subtype, issuer, role, f"official_directory:{source_id}", stamp, stamp, instrument_id),
    )
    alias_pairs = [(str(value[0]), str(value[1])) for value in connection.execute(
        "SELECT alias_symbol,alias_kind FROM rs_instrument_aliases WHERE instrument_id=?", (instrument_id,)
    )]
    decision = derive_yahoo_provider_symbol(str(row["canonical_symbol"]), alias_pairs)
    status, reason = decision.status, decision.reason
    connection.execute(
        """INSERT OR IGNORE INTO rs_providers(provider_id,display_name,provider_class,enablement_state,
           requires_credentials,terms_review_state,created_at_utc,updated_at_utc)
           VALUES('yahoo','Yahoo Finance','reference_mapping','runtime_controlled',0,'reviewed',?,?)""", (stamp, stamp),
    )
    if decision.supported and decision.provider_symbol:
        conflict = connection.execute(
            "SELECT instrument_id FROM rs_provider_symbols WHERE provider_id='yahoo' AND provider_symbol=? AND is_active=1 ORDER BY instrument_id LIMIT 1",
            (decision.provider_symbol,),
        ).fetchone()
        if conflict is not None and int(conflict[0]) != instrument_id:
            status, reason = "unsupported", "provider_symbol_collision"
        else:
            connection.execute(
                """INSERT OR REPLACE INTO rs_provider_symbols(provider_id,instrument_id,provider_symbol,
                   provider_venue,product_type,is_active,first_seen_utc,last_seen_utc,metadata_json,
                   mapping_status,capabilities_json,verified_at_utc)
                   VALUES('yahoo',?,?,?,?,1,?,?,?,?,?,?)""",
                (instrument_id, decision.provider_symbol, "", asset, stamp, stamp,
                 json.dumps({"canonical_symbol": decision.canonical_symbol,
                             "mapping_source": decision.mapping_source,
                             "evidence_aliases": decision.evidence_aliases,
                             "discovery_source": source_id}, sort_keys=True),
                 "derived_official_aliases", json.dumps(["candles", "historical", "quote"]), stamp),
            )
    if status != "supported":
        connection.execute(
            """UPDATE rs_provider_symbols SET is_active=0
               WHERE provider_id='yahoo' AND instrument_id=? AND mapping_status LIKE 'derived_%'""",
            (instrument_id,),
        )
    for capability in ("quote", "historical", "candles"):
        connection.execute(
            """INSERT OR REPLACE INTO rs_provider_instrument_support(provider_id,instrument_id,capability,
               support_status,reason,mapping_source,verified_at_utc) VALUES('yahoo',?,?,?,?,?,?)""",
            (instrument_id, capability, status, reason, decision.mapping_source, stamp),
        )
        connection.execute(
            """INSERT OR REPLACE INTO rs_instrument_capabilities(instrument_id,capability,applicability,reason,updated_at_utc)
               VALUES(?,?,?,?,?)""",
            (instrument_id, capability, "applicable" if status == "supported" else "not_applicable",
             "" if status == "supported" else reason, stamp),
        )

@dataclass(frozen=True)
class SourceCompleteness:
    subsource_id: str
    row_count: int
    parse_errors: int
    header_valid: bool
    footer_valid: bool
    previous_success_count: int | None
    complete: bool
    status: str
    reason: str | None
    source_sha256: str


@dataclass(frozen=True)
class OfficialDirectorySnapshot:
    instruments: tuple[DiscoveredInstrument, ...]
    raw_source: bytes
    parse_errors: int
    validations: tuple[SourceCompleteness, ...]

    @property
    def complete(self) -> bool:
        return bool(self.validations) and all(item.complete for item in self.validations)

    def __iter__(self):
        # Preserve the historical three-value source API used by QA tools.
        yield list(self.instruments)
        yield self.raw_source
        yield self.parse_errors


@dataclass(frozen=True)
class DiscoveryReport:
    source: str
    before_count: int
    after_count: int
    added: int
    removed_inactive: int
    changed: int
    parse_errors: int
    source_sha256: str
    source_timestamp: str
    status: str = "complete"
    error_summary: str | None = None
    source_validations: tuple[dict[str, object], ...] = ()


def classify_nasdaq_row(row: dict[str, str]) -> tuple[AssetClass, str]:
    name = row.get("Security Name", row.get("SecurityName", ""))
    decision = classify_official_security(name, provider_etp_flag=row.get("ETF", "N").upper() == "Y")
    return AssetClass(decision.asset_class), decision.security_type


def parse_nasdaq_directory(text: str, venue: str, subsource_id: str | None = None) -> tuple[list[DiscoveredInstrument], int]:
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Discovery source is empty.")
    headers = lines[0].split("|")
    results: list[DiscoveredInstrument] = []
    errors = 0
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        values = line.split("|")
        if len(values) != len(headers):
            errors += 1
            continue
        row = dict(zip(headers, values))
        if str(row.get("Test Issue") or "").strip().upper() == "Y":
            continue
        symbol = row.get("Symbol") or row.get("ACT Symbol") or row.get("NASDAQ Symbol") or ""
        name = row.get("Security Name") or row.get("SecurityName") or ""
        row_venue, _mic = normalize_listing_venue(row.get("Exchange") or venue)
        try:
            asset_class, security_type = classify_nasdaq_row(row)
            aliases: list[tuple[str, str]] = []
            for field_name in ("Symbol", "ACT Symbol", "CQS Symbol", "NASDAQ Symbol"):
                alias = str(row.get(field_name) or "").strip()
                if alias:
                    aliases.append((alias, "official_directory_symbol" if field_name in {"Symbol", "ACT Symbol"} else "official_source_symbol_variant"))
            results.append(
                DiscoveredInstrument(
                    symbol, name, asset_class, security_type, row_venue,
                    provider_symbol=symbol, official_aliases=tuple(aliases), source_partition=subsource_id,
                )
            )
        except ValueError:
            errors += 1
    if errors and errors > max(10, len(lines) // 4):
        raise ValueError("Discovery source exceeds the safe parse-error threshold.")
    return results, errors


class InstrumentDiscovery:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def is_due(self, source_id: str, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        row = self.connection.execute(
            "SELECT next_due_utc FROM rs_discovery_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        return row is None or row["next_due_utc"] is None or datetime.fromisoformat(row["next_due_utc"]) <= current

    def import_snapshot(
        self,
        source_id: str,
        display_name: str,
        official_url: str,
        instruments: Iterable[DiscoveredInstrument],
        raw_source: bytes,
        *,
        parse_errors: int = 0,
        now: datetime | None = None,
        failpoint: Callable[[], None] | None = None,
        source_validations: Iterable[SourceCompleteness] = (),
        reconciliation_complete: bool = True,
    ) -> DiscoveryReport:
        current = now or datetime.now(timezone.utc)
        stamp = current.isoformat()
        digest = sha256(raw_source).hexdigest()
        snapshot = list(instruments)
        validations = tuple(source_validations)
        key_map = {(item.canonical_symbol, normalize_listing_venue(item.primary_venue)[0]): item for item in snapshot}
        if len(key_map) != len(snapshot):
            raise ValueError("Discovery snapshot contains duplicate canonical identities.")
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT INTO rs_discovery_sources(source_id,display_name,source_kind,official_url,enabled,
                   refresh_interval_seconds,created_at_utc,updated_at_utc)
                   VALUES(?,?,?,?,1,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET display_name=excluded.display_name,
                   official_url=excluded.official_url,updated_at_utc=excluded.updated_at_utc""",
                (source_id, display_name, "official_directory", official_url, WEEK_SECONDS, stamp, stamp),
            )
            before = connection.execute(
                """SELECT COUNT(*) FROM rs_instruments i WHERE i.is_active=1 AND (
                   EXISTS(SELECT 1 FROM rs_instrument_reference_sources r
                          WHERE r.instrument_id=i.instrument_id AND r.source_id=?)
                   OR EXISTS(SELECT 1 FROM rs_instrument_aliases a
                             WHERE a.instrument_id=i.instrument_id AND a.source_id=?))""",
                (source_id, source_id),
            ).fetchone()[0]
            run = connection.execute(
                """INSERT INTO rs_discovery_runs(source_id,started_at_utc,status,source_timestamp,source_sha256,
                   rows_seen,before_count,parse_error_count) VALUES(?,?,?,?,?,?,?,?)""",
                (source_id, stamp, "running", stamp, digest, len(snapshot), before, parse_errors),
            )
            run_id = run.lastrowid
            validation_payload = tuple(asdict(item) for item in validations)
            destructive_allowed = bool(reconciliation_complete) and all(item.complete for item in validations)
            if not destructive_allowed:
                status = "failed" if any(item.status == "failed" for item in validations) else "incomplete"
                reason = ";".join(filter(None, (item.reason for item in validations))) or "snapshot_completeness_not_proven"
                for validation in validations:
                    connection.execute(
                        """INSERT INTO rs_discovery_subsource_state(source_id,subsource_id,last_observed_count,last_status,last_error,updated_at_utc)
                           VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,subsource_id) DO UPDATE SET
                           last_observed_count=excluded.last_observed_count,last_status=excluded.last_status,
                           last_error=excluded.last_error,updated_at_utc=excluded.updated_at_utc""",
                        (source_id, validation.subsource_id, validation.row_count, validation.status, validation.reason, stamp),
                    )
                connection.execute(
                    """UPDATE rs_discovery_runs SET completed_at_utc=?,status=?,after_count=?,error_summary=?
                       WHERE discovery_run_id=?""",
                    (stamp, status, before, reason, run_id),
                )
                connection.execute(
                    "UPDATE rs_discovery_sources SET next_due_utc=?,updated_at_utc=? WHERE source_id=?",
                    ((current + timedelta(seconds=RETRY_SECONDS)).isoformat(), stamp, source_id),
                )
                connection.commit()
                return DiscoveryReport(
                    source_id, before, before, 0, 0, 0, parse_errors, digest, stamp, status, reason, validation_payload
                )
            partition_items: dict[str, set[str]] = {}
            for item in snapshot:
                if item.source_partition:
                    partition_items.setdefault(item.source_partition, set()).add(item.canonical_symbol)
            partition_hashes = {
                partition: sha256("\n".join(sorted(symbols)).encode("utf-8")).hexdigest()
                for partition, symbols in partition_items.items()
            }
            validation_hashes = {item.subsource_id: item.source_sha256 for item in validations}
            continuity_anomalies: list[str] = []
            for partition, current_symbols in partition_items.items():
                prior_rows = connection.execute(
                    """SELECT m.instrument_id,i.canonical_symbol FROM rs_discovery_subsource_members m
                       JOIN rs_instruments i ON i.instrument_id=m.instrument_id
                       WHERE m.source_id=? AND m.subsource_id=? AND m.state IN ('present','pending')""",
                    (source_id, partition),
                ).fetchall()
                if not prior_rows:
                    prior_rows = connection.execute(
                        """SELECT DISTINCT i.instrument_id,i.canonical_symbol
                           FROM rs_instruments i
                           JOIN rs_instrument_reference_sources r ON r.instrument_id=i.instrument_id
                           WHERE r.source_id=? AND i.is_active=1 AND
                           ((?='nasdaqlisted' AND UPPER(COALESCE(i.primary_venue,''))='NASDAQ') OR
                            (?='otherlisted' AND UPPER(COALESCE(i.primary_venue,''))<>'NASDAQ'))""",
                        (source_id, partition, partition),
                    ).fetchall()
                missing = [row for row in prior_rows if row["canonical_symbol"] not in current_symbols]
                if len(missing) > MAX_SINGLE_SNAPSHOT_PENDING:
                    continuity_anomalies.append(f"{partition}:missing={len(missing)}")
            if continuity_anomalies:
                reason = "continuity_anomaly:" + ";".join(continuity_anomalies)
                for validation in validations:
                    connection.execute(
                        """INSERT INTO rs_discovery_subsource_state(
                           source_id,subsource_id,last_observed_count,last_observed_symbol_set_sha256,
                           pending_missing_count,last_status,last_error,updated_at_utc)
                           VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,subsource_id) DO UPDATE SET
                           last_observed_count=excluded.last_observed_count,
                           last_observed_symbol_set_sha256=excluded.last_observed_symbol_set_sha256,
                           pending_missing_count=excluded.pending_missing_count,last_status=excluded.last_status,
                           last_error=excluded.last_error,updated_at_utc=excluded.updated_at_utc""",
                        (source_id, validation.subsource_id, validation.row_count,
                         partition_hashes.get(validation.subsource_id),
                         next((int(value.split("=")[1]) for value in continuity_anomalies
                               if value.startswith(validation.subsource_id + ":")), 0),
                         "incomplete", reason, stamp),
                    )
                connection.execute(
                    """UPDATE rs_discovery_runs SET completed_at_utc=?,status='incomplete',after_count=?,error_summary=?
                       WHERE discovery_run_id=?""", (stamp, before, reason, run_id),
                )
                connection.execute(
                    "UPDATE rs_discovery_sources SET next_due_utc=?,updated_at_utc=? WHERE source_id=?",
                    ((current + timedelta(seconds=RETRY_SECONDS)).isoformat(), stamp, source_id),
                )
                connection.commit()
                return DiscoveryReport(
                    source_id, before, before, 0, 0, 0, parse_errors, digest, stamp,
                    "incomplete", reason, validation_payload,
                )
            existing_rows = connection.execute(
                """SELECT i.*,
                   CASE WHEN EXISTS(SELECT 1 FROM rs_instrument_reference_sources r
                                    WHERE r.instrument_id=i.instrument_id AND r.source_id=:source)
                          OR EXISTS(SELECT 1 FROM rs_instrument_aliases a
                                    WHERE a.instrument_id=i.instrument_id AND a.source_id=:source)
                        THEN 1 ELSE 0 END AS source_owned
                   FROM rs_instruments i WHERE i.is_active=1 ORDER BY i.instrument_id""",
                {"source": source_id},
            ).fetchall()
            existing = {
                (row["canonical_symbol"], normalize_listing_venue(row["primary_venue"])[0]): row
                for row in existing_rows
            }
            added = changed = removed = 0
            seen_ids: set[int] = set()
            for key, item in key_map.items():
                normalized_key = (item.canonical_symbol, normalize_listing_venue(item.primary_venue)[0])
                row = existing.get(normalized_key)
                if row is None:
                    same_symbol = [
                        candidate
                        for candidate in existing_rows
                        if candidate["canonical_symbol"] == item.canonical_symbol
                    ]
                    verified_rename = next(
                        (
                            candidate
                            for candidate in existing_rows
                            if candidate["canonical_symbol"] in item.verified_previous_symbols
                            and normalize_listing_venue(candidate["primary_venue"])[0]
                                == normalize_listing_venue(item.primary_venue)[0]
                        ),
                        None,
                    )
                    venue_change = (
                        same_symbol[0]
                        if len(same_symbol) == 1
                        and normalize_listing_name(same_symbol[0]["security_name"])
                            == normalize_listing_name(item.security_name)
                        else None
                    )
                    if venue_change is not None:
                        instrument_id = int(venue_change["instrument_id"])
                        connection.execute(
                            """UPDATE rs_instruments SET canonical_symbol=?,security_name=?,
                               primary_venue=?,cik=COALESCE(?,cik),is_active=1,last_seen_utc=?,metadata_updated_utc=?,updated_at_utc=?
                               WHERE instrument_id=?""",
                            (
                                item.canonical_symbol,
                                item.security_name,
                                item.primary_venue,
                                item.cik,
                                stamp,
                                stamp,
                                stamp,
                                instrument_id,
                            ),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO rs_instrument_aliases(instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc) VALUES(?,?,?,?,?,?)",
                            (
                                instrument_id,
                                venue_change["canonical_symbol"],
                                venue_change["primary_venue"],
                                "previous_venue",
                                source_id,
                                stamp,
                            ),
                        )
                        connection.execute(
                            """INSERT INTO rs_discovery_changes(discovery_run_id,instrument_id,change_type,
                               old_symbol,new_symbol,old_name,new_name,old_venue,new_venue,created_at_utc)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (
                                run_id,
                                instrument_id,
                                "venue_changed",
                                venue_change["canonical_symbol"],
                                item.canonical_symbol,
                                venue_change["security_name"],
                                item.security_name,
                                venue_change["primary_venue"],
                                item.primary_venue,
                                stamp,
                            ),
                        )
                        change_type = None
                        changed += 1
                    elif verified_rename is not None:
                        instrument_id = int(verified_rename["instrument_id"])
                        old_symbol = str(verified_rename["canonical_symbol"])
                        connection.execute(
                            """UPDATE rs_instruments SET canonical_symbol=?,security_name=?,primary_venue=?,
                               is_active=1,last_seen_utc=?,metadata_updated_utc=?,updated_at_utc=?
                               WHERE instrument_id=?""",
                            (item.canonical_symbol, item.security_name, item.primary_venue,
                             stamp, stamp, stamp, instrument_id),
                        )
                        connection.execute(
                            """INSERT OR IGNORE INTO rs_instrument_aliases(
                               instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc)
                               VALUES(?,?,?,?,?,?)""",
                            (instrument_id, old_symbol, item.primary_venue, "previous_symbol", source_id, stamp),
                        )
                        connection.execute(
                            """INSERT OR IGNORE INTO rs_instrument_identity_evidence(
                               source_id,instrument_id,old_symbol,new_symbol,venue,evidence_type,
                               evidence_value,observed_at_utc) VALUES(?,?,?,?,?,?,?,?)""",
                            (source_id, instrument_id, old_symbol, item.canonical_symbol, item.primary_venue,
                             "authoritative_previous_symbol", old_symbol, stamp),
                        )
                        change_type = "symbol_changed_verified"
                        changed += 1
                    else:
                        result = connection.execute(
                            """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,security_type,
                               primary_venue,currency,country_code,cik,listing_date,is_active,first_seen_utc,last_seen_utc,
                               metadata_updated_utc,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                            (item.canonical_symbol, item.security_name, item.asset_class.value, item.security_type,
                             item.primary_venue, item.currency, item.country_code, item.cik,
                             item.listing_date.isoformat() if item.listing_date else None,
                             stamp, stamp, stamp, stamp, stamp),
                        )
                        instrument_id = int(result.lastrowid)
                        change_type = "added"
                        added += 1
                    connection.execute(
                        "INSERT OR IGNORE INTO rs_instrument_aliases(instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc) VALUES(?,?,?,?,?,?)",
                        (instrument_id, item.provider_symbol or item.canonical_symbol, item.primary_venue, "source_symbol", source_id, stamp),
                    )
                    if change_type is not None:
                        connection.execute(
                            "INSERT INTO rs_discovery_changes(discovery_run_id,instrument_id,change_type,new_symbol,new_name,new_venue,created_at_utc) VALUES(?,?,?,?,?,?,?)",
                            (run_id, instrument_id, change_type, item.canonical_symbol, item.security_name, item.primary_venue, stamp),
                        )
                else:
                    instrument_id = int(row["instrument_id"])
                    fields_changed = row["security_name"] != item.security_name or not row["is_active"]
                    connection.execute(
                        "UPDATE rs_instruments SET security_name=?,is_active=1,last_seen_utc=?,metadata_updated_utc=?,updated_at_utc=? WHERE instrument_id=?",
                        (item.security_name, stamp, stamp, stamp, instrument_id),
                    )
                    if fields_changed:
                        changed += 1
                        connection.execute(
                            "INSERT INTO rs_discovery_changes(discovery_run_id,instrument_id,change_type,old_name,new_name,created_at_utc) VALUES(?,?,?,?,?,?)",
                            (run_id, instrument_id, "metadata_changed", row["security_name"], item.security_name, stamp),
                        )
                connection.execute(
                    """INSERT INTO rs_instrument_aliases(
                       instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(instrument_id,alias_symbol,venue,alias_kind) DO UPDATE SET
                         source_id=COALESCE(rs_instrument_aliases.source_id,excluded.source_id)""",
                    (instrument_id, item.provider_symbol or item.canonical_symbol,
                     normalize_listing_venue(item.primary_venue)[0], "source_symbol", source_id, stamp),
                )
                _enrich_discovered_instrument(connection, instrument_id, item, source_id, stamp)
                if item.source_partition:
                    connection.execute(
                        """INSERT INTO rs_discovery_subsource_members(
                           source_id,subsource_id,instrument_id,state,missing_observations,
                           pending_since_utc,last_present_utc,last_missing_utc,
                           last_missing_source_sha256,updated_at_utc)
                           VALUES(?,?,?,'present',0,NULL,?,NULL,NULL,?)
                           ON CONFLICT(source_id,subsource_id,instrument_id) DO UPDATE SET
                           state='present',missing_observations=0,pending_since_utc=NULL,
                           last_present_utc=excluded.last_present_utc,last_missing_utc=NULL,
                           last_missing_source_sha256=NULL,updated_at_utc=excluded.updated_at_utc""",
                        (source_id, item.source_partition, instrument_id, stamp, stamp),
                    )
                connection.execute(
                    """INSERT INTO rs_instrument_reference_sources(
                       instrument_id,source_id,source_symbol,source_name,source_exchange,
                       source_snapshot_sha256,source_retrieved_utc)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(instrument_id,source_id) DO UPDATE SET
                         source_symbol=excluded.source_symbol,source_name=excluded.source_name,
                         source_exchange=excluded.source_exchange,
                         source_snapshot_sha256=excluded.source_snapshot_sha256,
                         source_retrieved_utc=excluded.source_retrieved_utc""",
                    (instrument_id, source_id, item.provider_symbol or item.canonical_symbol,
                     item.security_name, normalize_listing_venue(item.primary_venue)[0], digest, stamp),
                )
                seen_ids.add(instrument_id)
            for row in existing_rows:
                instrument_id = int(row["instrument_id"])
                if not (row["source_owned"] and instrument_id not in seen_ids and row["is_active"]):
                    continue
                memberships = connection.execute(
                    """SELECT * FROM rs_discovery_subsource_members
                       WHERE source_id=? AND instrument_id=? AND state IN ('present','pending')
                       ORDER BY subsource_id""", (source_id, instrument_id),
                ).fetchall()
                if not memberships:
                    continue
                confirmed = False
                for membership in memberships:
                    partition = str(membership["subsource_id"])
                    if partition not in partition_items:
                        continue
                    current_hash = validation_hashes.get(partition) or partition_hashes.get(partition) or digest
                    prior_observations = int(membership["missing_observations"] or 0)
                    independent = bool(
                        membership["last_missing_source_sha256"]
                        and membership["last_missing_source_sha256"] != current_hash
                    )
                    observations = prior_observations + 1 if independent or prior_observations == 0 else prior_observations
                    if observations >= MISSING_CONFIRMATIONS_REQUIRED:
                        connection.execute(
                            """UPDATE rs_discovery_subsource_members SET state='confirmed_inactive',
                               missing_observations=?,last_missing_utc=?,last_missing_source_sha256=?,updated_at_utc=?
                               WHERE source_id=? AND subsource_id=? AND instrument_id=?""",
                            (observations, stamp, current_hash, stamp, source_id, partition, instrument_id),
                        )
                        confirmed = True
                    else:
                        connection.execute(
                            """UPDATE rs_discovery_subsource_members SET state='pending',missing_observations=?,
                               pending_since_utc=COALESCE(pending_since_utc,?),last_missing_utc=?,
                               last_missing_source_sha256=?,updated_at_utc=?
                               WHERE source_id=? AND subsource_id=? AND instrument_id=?""",
                            (observations, stamp, stamp, current_hash, stamp, source_id, partition, instrument_id),
                        )
                if confirmed:
                    connection.execute(
                        "UPDATE rs_instruments SET is_active=0,delisting_date=?,updated_at_utc=? WHERE instrument_id=?",
                        (current.date().isoformat(), stamp, instrument_id),
                    )
                    connection.execute(
                        """INSERT INTO rs_discovery_changes(discovery_run_id,instrument_id,change_type,
                           old_symbol,old_name,old_venue,created_at_utc) VALUES(?,?,?,?,?,?,?)""",
                        (run_id, instrument_id, "inactive_confirmed", row["canonical_symbol"],
                         row["security_name"], row["primary_venue"], stamp),
                    )
                    removed += 1
            if failpoint:
                failpoint()
            after = connection.execute(
                """SELECT COUNT(*) FROM rs_instruments i WHERE i.is_active=1 AND
                   EXISTS(SELECT 1 FROM rs_instrument_reference_sources r
                          WHERE r.instrument_id=i.instrument_id AND r.source_id=?)""",
                (source_id,),
            ).fetchone()[0]
            connection.execute(
                """UPDATE rs_discovery_runs SET completed_at_utc=?,status='complete',after_count=?,
                   added_count=?,removed_count=?,changed_count=? WHERE discovery_run_id=?""",
                (stamp, after, added, removed, changed, run_id),
            )
            for validation in validations:
                pending_count = connection.execute(
                    """SELECT COUNT(*) FROM rs_discovery_subsource_members
                       WHERE source_id=? AND subsource_id=? AND state='pending'""",
                    (source_id, validation.subsource_id),
                ).fetchone()[0]
                symbol_set_hash = partition_hashes.get(validation.subsource_id)
                connection.execute(
                    """INSERT INTO rs_discovery_subsource_state(
                       source_id,subsource_id,last_success_count,last_success_sha256,last_success_utc,
                       last_success_symbol_set_sha256,last_observed_count,last_observed_symbol_set_sha256,
                       pending_missing_count,last_status,last_error,updated_at_utc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id,subsource_id) DO UPDATE SET
                       last_success_count=excluded.last_success_count,last_success_sha256=excluded.last_success_sha256,
                       last_success_utc=excluded.last_success_utc,
                       last_success_symbol_set_sha256=excluded.last_success_symbol_set_sha256,
                       last_observed_count=excluded.last_observed_count,
                       last_observed_symbol_set_sha256=excluded.last_observed_symbol_set_sha256,
                       pending_missing_count=excluded.pending_missing_count,last_status=excluded.last_status,
                       last_error=NULL,updated_at_utc=excluded.updated_at_utc""",
                    (source_id, validation.subsource_id, validation.row_count, validation.source_sha256, stamp,
                     symbol_set_hash, validation.row_count, symbol_set_hash, pending_count, "complete", None, stamp),
                )
            connection.execute(
                "UPDATE rs_discovery_sources SET last_success_utc=?,next_due_utc=?,updated_at_utc=? WHERE source_id=?",
                (stamp, (current + timedelta(seconds=WEEK_SECONDS)).isoformat(), stamp, source_id),
            )
            rebuild_instrument_search_index(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return DiscoveryReport(
            source_id, before, after, added, removed, changed, parse_errors, digest, stamp,
            "complete", None, tuple(asdict(item) for item in validations),
        )


class DiscoveryScheduler:
    def __init__(self, discovery: InstrumentDiscovery, max_workers: int = 1) -> None:
        self.discovery = discovery
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rangescout-discovery")

    def refresh_nonblocking(self, operation: Callable[[], DiscoveryReport]) -> Future[DiscoveryReport]:
        return self._executor.submit(operation)

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)


def _validate_official_directory(
    subsource_id: str,
    text: str,
    venue: str,
    previous_success_count: int | None,
) -> tuple[list[DiscoveredInstrument], SourceCompleteness]:
    raw = text.encode("utf-8")
    digest = sha256(raw).hexdigest()
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    expected = _EXPECTED_HEADERS[subsource_id]
    header_valid = bool(lines) and tuple(lines[0].split("|")) == expected
    footer_valid = bool(lines) and any(line.startswith("File Creation Time:") for line in lines[-3:])
    rows: list[DiscoveredInstrument] = []
    parse_errors = 0
    reasons: list[str] = []
    if not header_valid:
        reasons.append("malformed_header")
    if not footer_valid:
        reasons.append("missing_official_footer")
    if header_valid and footer_valid:
        try:
            rows, parse_errors = parse_nasdaq_directory(text, venue, subsource_id)
        except ValueError as exc:
            reasons.append(f"parse_rejected:{exc}")
    row_count = len(rows)
    if row_count < MIN_PLAUSIBLE_ROWS:
        reasons.append("implausibly_small_source")
    if previous_success_count:
        absolute_drop = previous_success_count - row_count
        relative_floor = int(previous_success_count * (1.0 - MAX_SAFE_RELATIVE_DROP))
        if absolute_drop > MAX_SAFE_ABSOLUTE_DROP and row_count < relative_floor:
            reasons.append("implausible_drop_from_last_success")
    complete = not reasons
    return rows, SourceCompleteness(
        subsource_id=subsource_id,
        row_count=row_count,
        parse_errors=parse_errors,
        header_valid=header_valid,
        footer_valid=footer_valid,
        previous_success_count=previous_success_count,
        complete=complete,
        status="complete" if complete else "incomplete",
        reason=";".join(reasons) if reasons else None,
        source_sha256=digest,
    )


class OfficialNasdaqDirectorySource:
    """Terms-approved directories with independent fail-safe completeness validation."""

    NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

    def __init__(self, fetch_text: Callable[[str], str] | None = None) -> None:
        self._fetch_text = fetch_text or self._download

    def fetch(self, previous_counts: Mapping[str, int] | None = None) -> OfficialDirectorySnapshot:
        previous = dict(previous_counts or {})
        rows: list[DiscoveredInstrument] = []
        validations: list[SourceCompleteness] = []
        raw_parts: list[str] = []
        for subsource_id, url, venue in (
            ("nasdaqlisted", self.NASDAQ_URL, "Q"),
            ("otherlisted", self.OTHER_URL, "N"),
        ):
            try:
                source_text = self._fetch_text(url)
            except Exception as exc:
                source_text = ""
                validation = SourceCompleteness(
                    subsource_id, 0, 0, False, False, previous.get(subsource_id), False,
                    "failed", f"fetch_failed:{type(exc).__name__}", sha256(b"").hexdigest(),
                )
                source_rows: list[DiscoveredInstrument] = []
            else:
                source_rows, validation = _validate_official_directory(
                    subsource_id, source_text, venue, previous.get(subsource_id)
                )
            rows.extend(source_rows)
            validations.append(validation)
            raw_parts.append(f"SOURCE={url}\n{source_text}\n")
        raw = "".join(raw_parts).encode("utf-8")
        return OfficialDirectorySnapshot(
            tuple(rows), raw, sum(item.parse_errors for item in validations), tuple(validations)
        )

    @staticmethod
    def _download(url: str) -> str:
        request = Request(
            url,
            headers={"User-Agent": "RangeScout/1.6.2 (Dietrich AI Labs; official-directory discovery)"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=6.0) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise RuntimeError("Official Nasdaq Trader discovery source is unavailable.") from None
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError("Official Nasdaq Trader discovery response exceeds the safety limit.")
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RuntimeError("Official Nasdaq Trader discovery response is not valid UTF-8.") from None


class DiscoveryCoordinator:
    """Production lifecycle, scheduling, status, and search for official discovery."""

    SOURCE_ID = "nasdaq_trader_us_listings"
    DISPLAY_NAME = "Nasdaq Trader official US listing directories"
    OFFICIAL_URL = "https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs"

    def __init__(
        self,
        database_path: Path | str,
        *,
        source: OfficialNasdaqDirectorySource | None = None,
        scheduler: DiscoveryScheduler | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.source = source or OfficialNasdaqDirectorySource()
        self._scheduler = scheduler
        self._future: Future[DiscoveryReport] | None = None
        self._lock = RLock()
        self._last_error: str | None = None

    def refresh_if_due(self, now: datetime | None = None) -> Future[DiscoveryReport] | None:
        with self._connection() as connection:
            if not InstrumentDiscovery(connection).is_due(self.SOURCE_ID, now):
                return None
        return self.refresh_manual()

    def refresh_manual(self) -> Future[DiscoveryReport]:
        with self._lock:
            if self._future is not None and not self._future.done():
                return self._future
            if self._scheduler is None:
                placeholder = sqlite3.connect(self.database_path, check_same_thread=False)
                try:
                    self._scheduler = DiscoveryScheduler(InstrumentDiscovery(placeholder))
                finally:
                    placeholder.close()
            self._future = self._scheduler.refresh_nonblocking(self._refresh_operation)
            return self._future

    def _refresh_operation(self) -> DiscoveryReport:
        try:
            with self._connection() as connection:
                previous_counts = {
                    str(row["subsource_id"]): int(row["last_success_count"])
                    for row in connection.execute(
                        "SELECT subsource_id,last_success_count FROM rs_discovery_subsource_state WHERE source_id=? AND last_success_count IS NOT NULL",
                        (self.SOURCE_ID,),
                    )
                }
            if isinstance(self.source, OfficialNasdaqDirectorySource):
                snapshot = self.source.fetch(previous_counts)
            else:
                snapshot = self.source.fetch()
            if isinstance(snapshot, OfficialDirectorySnapshot):
                instruments, raw, parse_errors = snapshot
                validations = snapshot.validations
                complete = snapshot.complete
            else:
                instruments, raw, parse_errors = snapshot
                validations = ()
                complete = False
            with self._connection() as connection:
                report = InstrumentDiscovery(connection).import_snapshot(
                    self.SOURCE_ID,
                    self.DISPLAY_NAME,
                    self.OFFICIAL_URL,
                    instruments,
                    raw,
                    parse_errors=parse_errors,
                    source_validations=validations,
                    reconciliation_complete=complete,
                )
        except Exception as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            raise
        with self._lock:
            self._last_error = None
        return report

    def status(self) -> dict[str, object]:
        with self._connection() as connection:
            source = connection.execute(
                "SELECT * FROM rs_discovery_sources WHERE source_id=?", (self.SOURCE_ID,)
            ).fetchone()
            run = connection.execute(
                """SELECT * FROM rs_discovery_runs WHERE source_id=? AND status='complete'
                   ORDER BY discovery_run_id DESC LIMIT 1""",
                (self.SOURCE_ID,),
            ).fetchone()
            latest_run = connection.execute(
                "SELECT * FROM rs_discovery_runs WHERE source_id=? ORDER BY discovery_run_id DESC LIMIT 1",
                (self.SOURCE_ID,),
            ).fetchone()
            subsources = [dict(row) for row in connection.execute(
                "SELECT * FROM rs_discovery_subsource_state WHERE source_id=? ORDER BY subsource_id",
                (self.SOURCE_ID,),
            )]
        with self._lock:
            running = self._future is not None and not self._future.done()
            last_error = self._last_error
        return {
            "source_id": self.SOURCE_ID,
            "display_name": self.DISPLAY_NAME,
            "official_url": self.OFFICIAL_URL,
            "running": running,
            "last_error": last_error,
            "last_success_utc": source["last_success_utc"] if source else None,
            "next_due_utc": source["next_due_utc"] if source else None,
            "source_sha256": run["source_sha256"] if run else None,
            "added": run["added_count"] if run else 0,
            "removed_inactive": run["removed_count"] if run else 0,
            "changed": run["changed_count"] if run else 0,
            "parse_errors": run["parse_error_count"] if run else 0,
            "last_run_status": latest_run["status"] if latest_run else None,
            "last_run_error": latest_run["error_summary"] if latest_run else None,
            "subsources": subsources,
        }

    def search(self, query: str, limit: int = 25) -> list[dict[str, object]]:
        normalized = query.strip().upper()
        if not normalized:
            return []
        pattern = normalized + "%"
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT i.instrument_id,i.canonical_symbol,i.security_name,i.asset_class,
                   i.primary_venue FROM rs_instruments i LEFT JOIN rs_instrument_aliases a
                   ON a.instrument_id=i.instrument_id WHERE i.is_active=1 AND
                   (UPPER(i.canonical_symbol) LIKE ? OR UPPER(COALESCE(a.alias_symbol,'')) LIKE ? OR
                    UPPER(COALESCE(i.security_name,'')) LIKE ?)
                   ORDER BY i.canonical_symbol,i.primary_venue LIMIT ?""",
                (pattern, pattern, "%" + normalized + "%", max(1, min(100, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            scheduler = self._scheduler
        if scheduler is not None:
            scheduler.shutdown(wait=wait)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()


def report_json(report: DiscoveryReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)
