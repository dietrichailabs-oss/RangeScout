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
from typing import Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.market_data.contracts import AssetClass
from app.market_data.instruments import DiscoveredInstrument
from app.instruments.security_classification import classify_official_security


WEEK_SECONDS = 7 * 24 * 60 * 60

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


def classify_nasdaq_row(row: dict[str, str]) -> tuple[AssetClass, str]:
    name = row.get("Security Name", row.get("SecurityName", ""))
    decision = classify_official_security(name, provider_etp_flag=row.get("ETF", "N").upper() == "Y")
    return AssetClass(decision.asset_class), decision.security_type


def parse_nasdaq_directory(text: str, venue: str) -> tuple[list[DiscoveredInstrument], int]:
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
            results.append(
                DiscoveredInstrument(symbol, name, asset_class, security_type, row_venue, provider_symbol=symbol)
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
    ) -> DiscoveryReport:
        current = now or datetime.now(timezone.utc)
        stamp = current.isoformat()
        digest = sha256(raw_source).hexdigest()
        snapshot = list(instruments)
        key_map = {(item.canonical_symbol, item.primary_venue, item.asset_class.value): item for item in snapshot}
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
                (row["canonical_symbol"], normalize_listing_venue(row["primary_venue"])[0], row["asset_class"]): row
                for row in existing_rows
            }
            added = changed = removed = 0
            seen_ids: set[int] = set()
            for key, item in key_map.items():
                normalized_key = (item.canonical_symbol, normalize_listing_venue(item.primary_venue)[0], item.asset_class.value)
                row = existing.get(normalized_key)
                if row is None:
                    same_symbol = [
                        candidate
                        for candidate in existing_rows
                        if candidate["canonical_symbol"] == item.canonical_symbol
                    ]
                    stable_identity = next(
                        (
                            candidate
                            for candidate in existing_rows
                            if item.cik and candidate["cik"] and candidate["cik"] == item.cik
                        ),
                        None,
                    )
                    venue_change = (
                        stable_identity
                        or (
                            same_symbol[0]
                            if len(same_symbol) == 1
                            and normalize_listing_name(same_symbol[0]["security_name"]) == normalize_listing_name(item.security_name)
                            else None
                        )
                    )
                    same_name = next(
                        (candidate for candidate in existing_rows if normalize_listing_name(candidate["security_name"]) == normalize_listing_name(item.security_name) and normalize_listing_venue(candidate["primary_venue"])[0] == normalize_listing_venue(item.primary_venue)[0]),
                        None,
                    )
                    if venue_change is not None:
                        instrument_id = int(venue_change["instrument_id"])
                        connection.execute(
                            """UPDATE rs_instruments SET canonical_symbol=?,security_name=?,asset_class=?,security_type=?,
                               primary_venue=?,cik=COALESCE(?,cik),is_active=1,last_seen_utc=?,metadata_updated_utc=?,updated_at_utc=?
                               WHERE instrument_id=?""",
                            (
                                item.canonical_symbol,
                                item.security_name,
                                item.asset_class.value,
                                item.security_type,
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
                    elif same_name is not None:
                        instrument_id = int(same_name["instrument_id"])
                        connection.execute(
                            "UPDATE rs_instruments SET canonical_symbol=?,asset_class=?,security_type=?,is_active=1,last_seen_utc=?,updated_at_utc=? WHERE instrument_id=?",
                            (item.canonical_symbol, item.asset_class.value, item.security_type, stamp, stamp, instrument_id),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO rs_instrument_aliases(instrument_id,alias_symbol,venue,alias_kind,source_id,created_at_utc) VALUES(?,?,?,?,?,?)",
                            (instrument_id, same_name["canonical_symbol"], item.primary_venue, "previous_symbol", source_id, stamp),
                        )
                        change_type = "symbol_changed"
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
                    fields_changed = row["security_name"] != item.security_name or row["security_type"] != item.security_type or not row["is_active"]
                    connection.execute(
                        "UPDATE rs_instruments SET security_name=?,security_type=?,is_active=1,last_seen_utc=?,metadata_updated_utc=?,updated_at_utc=? WHERE instrument_id=?",
                        (item.security_name, item.security_type, stamp, stamp, stamp, instrument_id),
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
                if row["source_owned"] and instrument_id not in seen_ids and row["is_active"]:
                    connection.execute(
                        "UPDATE rs_instruments SET is_active=0,delisting_date=?,updated_at_utc=? WHERE instrument_id=?",
                        (current.date().isoformat(), stamp, instrument_id),
                    )
                    connection.execute(
                        "INSERT INTO rs_discovery_changes(discovery_run_id,instrument_id,change_type,old_symbol,old_name,old_venue,created_at_utc) VALUES(?,?,?,?,?,?,?)",
                        (run_id, instrument_id, "inactive", row["canonical_symbol"], row["security_name"], row["primary_venue"], stamp),
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
            connection.execute(
                "UPDATE rs_discovery_sources SET last_success_utc=?,next_due_utc=?,updated_at_utc=? WHERE source_id=?",
                (stamp, (current + timedelta(seconds=WEEK_SECONDS)).isoformat(), stamp, source_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return DiscoveryReport(source_id, before, after, added, removed, changed, parse_errors, digest, stamp)


class DiscoveryScheduler:
    def __init__(self, discovery: InstrumentDiscovery, max_workers: int = 1) -> None:
        self.discovery = discovery
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rangescout-discovery")

    def refresh_nonblocking(self, operation: Callable[[], DiscoveryReport]) -> Future[DiscoveryReport]:
        return self._executor.submit(operation)

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)


class OfficialNasdaqDirectorySource:
    """Terms-approved Nasdaq Trader pipe-delimited listing directories."""

    NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

    def __init__(self, fetch_text: Callable[[str], str] | None = None) -> None:
        self._fetch_text = fetch_text or self._download

    def fetch(self) -> tuple[list[DiscoveredInstrument], bytes, int]:
        nasdaq_text = self._fetch_text(self.NASDAQ_URL)
        other_text = self._fetch_text(self.OTHER_URL)
        nasdaq, nasdaq_errors = parse_nasdaq_directory(nasdaq_text, "Q")
        other, other_errors = parse_nasdaq_directory(other_text, "N")
        raw = (
            f"SOURCE={self.NASDAQ_URL}\n{nasdaq_text}\n"
            f"SOURCE={self.OTHER_URL}\n{other_text}\n"
        ).encode("utf-8")
        return nasdaq + other, raw, nasdaq_errors + other_errors

    @staticmethod
    def _download(url: str) -> str:
        request = Request(
            url,
            headers={"User-Agent": "RangeScout/1.3 (Dietrich AI Labs; official-directory discovery)"},
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
            instruments, raw, parse_errors = self.source.fetch()
            with self._connection() as connection:
                report = InstrumentDiscovery(connection).import_snapshot(
                    self.SOURCE_ID,
                    self.DISPLAY_NAME,
                    self.OFFICIAL_URL,
                    instruments,
                    raw,
                    parse_errors=parse_errors,
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
