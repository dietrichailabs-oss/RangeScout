#!/usr/bin/env python
"""Build the deterministic RangeScout company master from frozen official snapshots."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "docs" / "engineering" / "v1.6" / "company_master_sources"
DEFAULT_OUTPUT = ROOT / "resources" / "RangeScout_Company_Master.sqlite"
DEFAULT_REPORT = ROOT / "docs" / "engineering" / "v1.6" / "COMPANY_MASTER_GENERATION_REPORT.json"
PARSER_VERSION = "rangescout-company-master-v2"
MASTER_VERSION = 2

SOURCE_FILES = {
    "sec_company_tickers_exchange": "sec_company_tickers_exchange.json",
    "nasdaq_trader_nasdaqlisted": "nasdaq_nasdaqlisted.txt",
    "nasdaq_trader_otherlisted": "nasdaq_otherlisted.txt",
}

OTHER_VENUES = {
    "A": ("NYSE American", "XASE"),
    "N": ("NYSE", "XNYS"),
    "P": ("NYSE Arca", "ARCX"),
    "V": ("IEX", "IEXG"),
    "Z": ("Cboe BZX", "BATS"),
}

SEC_VENUES = {
    "nasdaq": ("NASDAQ", "XNAS"),
    "nyse": ("NYSE", "XNYS"),
    "nyse american": ("NYSE American", "XASE"),
    "nyse arca": ("NYSE Arca", "ARCX"),
    "cboe": ("Cboe", "BATS"),
    "otc": ("OTC", "PINX"),
}


@dataclass
class SourceStats:
    source_id: str
    filename: str
    source_url: str
    retrieved_utc: str
    size_bytes: int
    sha256: str
    input_row_count: int = 0
    rejected_row_count: int = 0
    test_row_count: int = 0
    normalized_row_count: int = 0
    source_timestamp_utc: str | None = None


@dataclass
class InstrumentRow:
    symbol: str
    name: str
    asset_class: str
    security_type: str
    venue: str
    mic: str | None
    currency: str = "USD"
    country: str = "US"
    cik: str | None = None
    listing_date: str | None = None
    sources: dict[str, dict[str, str | None]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.symbol, self.venue, self.asset_class)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _match_key(value: str) -> str:
    return re.sub(r"[.\-/$]+", "-", _symbol(value)).strip("-")


def _valid_symbol(value: str) -> bool:
    return bool(value) and len(value) <= 32 and bool(re.fullmatch(r"[A-Z0-9.\-/$^]+", value))


def _security_type(name: str, is_etf: bool = False) -> tuple[str, str]:
    lowered = name.lower()
    if is_etf or " exchange traded fund" in lowered or lowered.endswith(" etf") or " etf " in lowered:
        return "etf", "Exchange Traded Fund"
    for token, label in (
        ("warrant", "Warrant"), ("subscription right", "Right"), (" rights", "Right"),
        (" unit", "Unit"), ("preferred", "Preferred Stock"), ("depositary", "Depositary Share"),
        ("ordinary share", "Ordinary Share"), ("common stock", "Common Stock"),
    ):
        if token in lowered:
            return "stock", label
    return "stock", "Listed Security"


def _source_date_from_footer(path: Path) -> str | None:
    tail = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()[-1]
    match = re.search(r"File Creation Time:\s*(\d{8})(\d{2}:\d{2})", tail)
    if not match:
        return None
    parsed = datetime.strptime("".join(match.groups()), "%m%d%Y%H:%M").replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _identity_records(input_dir: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads((input_dir / "SOURCE_SNAPSHOT_IDENTITY.json").read_text(encoding="utf-8-sig"))
    records = {row["filename"]: row for row in payload}
    for filename in SOURCE_FILES.values():
        path = input_dir / filename
        row = records.get(filename)
        if row is None or not path.is_file():
            raise RuntimeError(f"Frozen source snapshot is missing: {filename}")
        if path.stat().st_size != int(row["size_bytes"]) or _sha256(path) != str(row["sha256"]).upper():
            raise RuntimeError(f"Frozen source identity mismatch: {filename}")
    return records


def _source_stats(source_id: str, input_dir: Path, identities: dict[str, dict[str, Any]]) -> SourceStats:
    filename = SOURCE_FILES[source_id]
    row = identities[filename]
    return SourceStats(
        source_id, filename, str(row["source_url"]), str(row["retrieved_utc"]),
        int(row["size_bytes"]), str(row["sha256"]).upper(),
        source_timestamp_utc=_source_date_from_footer(input_dir / filename) if filename.endswith(".txt") else None,
    )


def _parse_nasdaq_listed(path: Path, stats: SourceStats) -> list[InstrumentRow]:
    records: list[InstrumentRow] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="|")
        for raw in reader:
            stats.input_row_count += 1
            symbol = _symbol(raw.get("Symbol"))
            if symbol.startswith("FILE CREATION TIME:") or not _valid_symbol(symbol):
                stats.rejected_row_count += 1
                continue
            if _symbol(raw.get("Test Issue")) == "Y":
                stats.test_row_count += 1
                continue
            name = str(raw.get("Security Name") or "").strip()
            if not name:
                stats.rejected_row_count += 1
                continue
            asset_class, security_type = _security_type(name, _symbol(raw.get("ETF")) == "Y")
            record = InstrumentRow(symbol, name, asset_class, security_type, "NASDAQ", "XNAS")
            record.sources[stats.source_id] = {
                "symbol": symbol, "name": name, "exchange": "NASDAQ", "snapshot_sha256": stats.sha256,
            }
            records.append(record)
    stats.normalized_row_count = len(records)
    return records


def _parse_other_listed(path: Path, stats: SourceStats) -> list[InstrumentRow]:
    records: list[InstrumentRow] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="|")
        for raw in reader:
            stats.input_row_count += 1
            symbol = _symbol(raw.get("ACT Symbol"))
            if symbol.startswith("FILE CREATION TIME:") or not _valid_symbol(symbol):
                stats.rejected_row_count += 1
                continue
            if _symbol(raw.get("Test Issue")) == "Y":
                stats.test_row_count += 1
                continue
            name = str(raw.get("Security Name") or "").strip()
            if not name:
                stats.rejected_row_count += 1
                continue
            venue_code = _symbol(raw.get("Exchange"))
            venue, mic = OTHER_VENUES.get(venue_code, (venue_code or "Other Listed", None))
            asset_class, security_type = _security_type(name, _symbol(raw.get("ETF")) == "Y")
            record = InstrumentRow(symbol, name, asset_class, security_type, venue, mic)
            record.sources[stats.source_id] = {
                "symbol": symbol, "name": name, "exchange": venue_code, "snapshot_sha256": stats.sha256,
            }
            for alias in (_symbol(raw.get("CQS Symbol")), _symbol(raw.get("NASDAQ Symbol"))):
                if alias and alias != symbol and _valid_symbol(alias):
                    record.aliases[alias] = "official_directory_symbol"
            records.append(record)
    stats.normalized_row_count = len(records)
    return records


def _parse_sec(path: Path, stats: SourceStats) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    fields = [str(value) for value in payload.get("fields", [])]
    records: list[dict[str, Any]] = []
    for values in payload.get("data", []):
        stats.input_row_count += 1
        raw = dict(zip(fields, values, strict=False))
        symbol = _symbol(raw.get("ticker"))
        name = str(raw.get("name") or "").strip()
        if not _valid_symbol(symbol) or not name:
            stats.rejected_row_count += 1
            continue
        try:
            cik = f"{int(raw['cik']):010d}"
        except (KeyError, TypeError, ValueError):
            stats.rejected_row_count += 1
            continue
        records.append({"symbol": symbol, "name": name, "exchange": str(raw.get("exchange") or "").strip(), "cik": cik})
    stats.normalized_row_count = len(records)
    return records


def _compatible(venue: str, sec_exchange: str) -> bool:
    expected = SEC_VENUES.get(sec_exchange.lower())
    return expected is None or expected[0].lower() == venue.lower() or sec_exchange.lower() in venue.lower()


def normalize(input_dir: Path) -> tuple[list[InstrumentRow], list[SourceStats]]:
    identities = _identity_records(input_dir)
    stats = {source_id: _source_stats(source_id, input_dir, identities) for source_id in SOURCE_FILES}
    directory_rows = (
        _parse_nasdaq_listed(input_dir / SOURCE_FILES["nasdaq_trader_nasdaqlisted"], stats["nasdaq_trader_nasdaqlisted"])
        + _parse_other_listed(input_dir / SOURCE_FILES["nasdaq_trader_otherlisted"], stats["nasdaq_trader_otherlisted"])
    )
    merged: dict[tuple[str, str, str], InstrumentRow] = {}
    for record in directory_rows:
        existing = merged.get(record.key)
        if existing is None:
            merged[record.key] = record
        else:
            existing.sources.update(record.sources)
            existing.aliases.update(record.aliases)
    by_match: dict[str, list[InstrumentRow]] = defaultdict(list)
    for record in merged.values():
        by_match[_match_key(record.symbol)].append(record)
    sec_rows = _parse_sec(input_dir / SOURCE_FILES["sec_company_tickers_exchange"], stats["sec_company_tickers_exchange"])
    for sec in sec_rows:
        candidates = by_match.get(_match_key(sec["symbol"]), [])
        compatible = [row for row in candidates if _compatible(row.venue, sec["exchange"])]
        target = compatible[0] if len(compatible) == 1 else (candidates[0] if len(candidates) == 1 else None)
        if target is None:
            venue, mic = SEC_VENUES.get(sec["exchange"].lower(), (sec["exchange"] or "SEC associated", None))
            asset_class, security_type = _security_type(sec["name"])
            target = InstrumentRow(sec["symbol"], sec["name"], asset_class, security_type, venue, mic, cik=sec["cik"])
            merged[target.key] = target
            by_match[_match_key(target.symbol)].append(target)
        elif not target.cik:
            target.cik = sec["cik"]
        target.sources["sec_company_tickers_exchange"] = {
            "symbol": sec["symbol"], "name": sec["name"], "exchange": sec["exchange"],
            "snapshot_sha256": stats["sec_company_tickers_exchange"].sha256,
        }
        if sec["symbol"] != target.symbol:
            target.aliases[sec["symbol"]] = "official_source_symbol_variant"
    return sorted(merged.values(), key=lambda row: row.key), [stats[source_id] for source_id in SOURCE_FILES]


def source_stats_by_id(stats: list[SourceStats], source_id: str) -> SourceStats:
    return next(item for item in stats if item.source_id == source_id)


def build(input_dir: Path, output: Path, report_path: Path) -> dict[str, Any]:
    rows, source_stats = normalize(input_dir)
    if len(rows) < 5000:
        raise RuntimeError(f"Broad company master unexpectedly small: {len(rows)}")
    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output)
    connection.executescript(
        """
        PRAGMA page_size=4096;
        PRAGMA foreign_keys=ON;
        CREATE TABLE master_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE source_snapshots(
            source_id TEXT PRIMARY KEY,filename TEXT NOT NULL,source_url TEXT NOT NULL,
            retrieved_utc TEXT NOT NULL,source_timestamp_utc TEXT,size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,parser_version TEXT NOT NULL,input_row_count INTEGER NOT NULL,
            rejected_row_count INTEGER NOT NULL,test_row_count INTEGER NOT NULL,
            normalized_row_count INTEGER NOT NULL);
        CREATE TABLE seed_instruments(
            canonical_symbol TEXT NOT NULL,security_name TEXT NOT NULL,asset_class TEXT NOT NULL,
            security_type TEXT,primary_venue TEXT NOT NULL,mic_code TEXT,currency TEXT,country_code TEXT,
            cik TEXT,listing_date TEXT,is_active INTEGER NOT NULL,sector TEXT,industry TEXT,
            website_domain TEXT,source_date TEXT NOT NULL,source_ids TEXT NOT NULL,
            PRIMARY KEY(canonical_symbol,primary_venue,asset_class));
        CREATE TABLE seed_aliases(
            canonical_symbol TEXT NOT NULL,primary_venue TEXT NOT NULL,asset_class TEXT NOT NULL,
            alias_symbol TEXT NOT NULL,alias_kind TEXT NOT NULL,source_date TEXT NOT NULL,
            PRIMARY KEY(canonical_symbol,primary_venue,asset_class,alias_symbol));
        CREATE TABLE seed_instrument_sources(
            canonical_symbol TEXT NOT NULL,primary_venue TEXT NOT NULL,asset_class TEXT NOT NULL,
            source_id TEXT NOT NULL,source_symbol TEXT,source_name TEXT,source_exchange TEXT,
            source_snapshot_sha256 TEXT NOT NULL,
            PRIMARY KEY(canonical_symbol,primary_venue,asset_class,source_id));
        """
    )
    frozen_at = max(stat.retrieved_utc for stat in source_stats)
    connection.executemany("INSERT INTO master_meta VALUES(?,?)", (
        ("version", str(MASTER_VERSION)), ("parser_version", PARSER_VERSION),
        ("source", "Frozen official SEC and Nasdaq Trader reference snapshots"),
        ("license", "Official/public factual issuer and listing reference data; internal build snapshots retained for audit"),
        ("generated_utc", frozen_at), ("contains_market_prices", "false"),
        ("contains_analyst_payloads", "false"), ("contains_credentials", "false"),
    ))
    connection.executemany(
        "INSERT INTO source_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple((s.source_id, s.filename, s.source_url, s.retrieved_utc, s.source_timestamp_utc,
               s.size_bytes, s.sha256, PARSER_VERSION, s.input_row_count, s.rejected_row_count,
               s.test_row_count, s.normalized_row_count) for s in source_stats),
    )
    for row in rows:
        source_date = max(source_stats_by_id(source_stats, source_id).retrieved_utc for source_id in row.sources)
        connection.execute(
            "INSERT INTO seed_instruments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row.symbol, row.name, row.asset_class, row.security_type, row.venue, row.mic,
             row.currency, row.country, row.cik, row.listing_date, 1, None, None, None,
             source_date, json.dumps(sorted(row.sources), separators=(",", ":"))),
        )
        for alias, kind in sorted(row.aliases.items()):
            connection.execute("INSERT OR IGNORE INTO seed_aliases VALUES(?,?,?,?,?,?)",
                               (row.symbol, row.venue, row.asset_class, alias, kind, source_date))
        for source_id, provenance in sorted(row.sources.items()):
            connection.execute(
                "INSERT INTO seed_instrument_sources VALUES(?,?,?,?,?,?,?,?)",
                (row.symbol, row.venue, row.asset_class, source_id, provenance.get("symbol"),
                 provenance.get("name"), provenance.get("exchange"), provenance["snapshot_sha256"]),
            )
    connection.commit()
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.execute("VACUUM")
    connection.close()
    source_counts = Counter(source_id for row in rows for source_id in row.sources)
    venue_counts = Counter(row.venue for row in rows)
    asset_counts = Counter(row.asset_class for row in rows)
    alias_count = sum(len(row.aliases) for row in rows)
    symbols = {row.symbol: row for row in rows}
    spot_symbols = ("AAPL", "BA", "NVDA", "MSFT", "GOOGL", "JPM", "AMD", "IBM", "KO", "XOM")
    report: dict[str, Any] = {
        "schema": "rangescout.company-master-generation.v2", "master_version": MASTER_VERSION,
        "parser_version": PARSER_VERSION, "master_path": output.name,
        "master_size_bytes": output.stat().st_size, "master_sha256": _sha256(output),
        "input_snapshots": [vars(stat) for stat in source_stats],
        "final_unique_instrument_count": len(rows), "alias_count": alias_count,
        "counts_by_source": dict(sorted(source_counts.items())),
        "counts_by_exchange_venue": dict(sorted(venue_counts.items())),
        "counts_by_asset_class": dict(sorted(asset_counts.items())),
        "known_symbol_spot_checks": {
            symbol: {"present": symbol in symbols, "name": symbols[symbol].name if symbol in symbols else None,
                     "venue": symbols[symbol].venue if symbol in symbols else None,
                     "cik": symbols[symbol].cik if symbol in symbols else None}
            for symbol in spot_symbols
        },
        "integrity_check": integrity, "foreign_key_violations": len(foreign_keys),
        "contains_market_prices": False, "contains_analyst_payloads": False, "contains_credentials": False,
    }
    report["pass"] = (len(rows) >= 5000 and all(report["known_symbol_spot_checks"][symbol]["present"] for symbol in spot_symbols)
                      and integrity == "ok" and not foreign_keys)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = build(args.input_dir.resolve(), args.output.resolve(), args.report.resolve())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
