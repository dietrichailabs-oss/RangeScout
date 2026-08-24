"""Schema-v12 safely records and repairs exact R8 discovery clones."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

MIGRATION_12_SQL = """
CREATE TABLE IF NOT EXISTS rs_instrument_identity_merges (
    old_instrument_id INTEGER PRIMARY KEY,
    survivor_instrument_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    merged_at_utc TEXT NOT NULL,
    FOREIGN KEY(old_instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE RESTRICT,
    FOREIGN KEY(survivor_instrument_id) REFERENCES rs_instruments(instrument_id) ON DELETE RESTRICT
);
"""

_DISCOVERY_SOURCE = "nasdaq_trader_us_listings"
_VENUES = {
    "Q": "NASDAQ", "XNAS": "NASDAQ", "NASDAQ": "NASDAQ",
    "N": "NYSE", "XNYS": "NYSE", "NYSE": "NYSE",
    "P": "NYSE ARCA", "ARCX": "NYSE ARCA", "NYSE ARCA": "NYSE ARCA",
    "A": "NYSE AMERICAN", "XASE": "NYSE AMERICAN", "NYSE AMERICAN": "NYSE AMERICAN",
    "Z": "CBOE BZX", "BATS": "CBOE BZX", "CBOE BZX": "CBOE BZX",
    "V": "IEX", "IEXG": "IEX", "IEX": "IEX",
}


def _venue(value: object) -> str:
    raw = str(value or "").strip().upper()
    return _VENUES.get(raw, raw)


def _name(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _has_provenance(connection: sqlite3.Connection, instrument_id: int, *, discovery: bool) -> bool:
    operator = "=" if discovery else "<>"
    row = connection.execute(
        f"""SELECT EXISTS(SELECT 1 FROM rs_instrument_reference_sources
                            WHERE instrument_id=? AND source_id{operator}?)
                    OR EXISTS(SELECT 1 FROM rs_instrument_aliases
                              WHERE instrument_id=? AND source_id{operator}?)""",
        (instrument_id, _DISCOVERY_SOURCE, instrument_id, _DISCOVERY_SOURCE),
    ).fetchone()
    return bool(row and row[0])


def _official_identity_agrees(
    connection: sqlite3.Connection, survivor: int, loser: int, symbol: str,
) -> bool:
    """Require independent official sources to assert the same canonical listing."""
    master = connection.execute(
        """SELECT EXISTS(
             SELECT 1 FROM rs_instrument_reference_sources
             WHERE instrument_id=? AND source_id<>? AND UPPER(source_symbol)=UPPER(?))""",
        (survivor, _DISCOVERY_SOURCE, symbol),
    ).fetchone()
    discovery = connection.execute(
        """SELECT EXISTS(
             SELECT 1 FROM rs_instrument_aliases
             WHERE instrument_id=? AND source_id=? AND UPPER(alias_symbol)=UPPER(?))
           OR EXISTS(
             SELECT 1 FROM rs_instrument_reference_sources
             WHERE instrument_id=? AND source_id=? AND UPPER(source_symbol)=UPPER(?))""",
        (loser, _DISCOVERY_SOURCE, symbol, loser, _DISCOVERY_SOURCE, symbol),
    ).fetchone()
    return bool(master and master[0] and discovery and discovery[0])


def _retain_provenance(connection: sqlite3.Connection, survivor: int, loser: int, stamp: str) -> None:
    alias = connection.execute(
        """SELECT alias_symbol,venue FROM rs_instrument_aliases
           WHERE instrument_id=? AND source_id=? ORDER BY alias_id LIMIT 1""",
        (loser, _DISCOVERY_SOURCE),
    ).fetchone()
    if alias is not None:
        connection.execute(
            """INSERT INTO rs_instrument_reference_sources(
               instrument_id,source_id,source_symbol,source_name,source_exchange,
               source_snapshot_sha256,source_retrieved_utc)
               SELECT ?,?,?,security_name,?,'legacy-r8-discovery-repair',?
               FROM rs_instruments WHERE instrument_id=?
               ON CONFLICT(instrument_id,source_id) DO NOTHING""",
            (survivor, _DISCOVERY_SOURCE, alias[0], _venue(alias[1]), stamp, loser),
        )


def _remap_nonconflicting_references(connection: sqlite3.Connection, loser: int, survivor: int) -> int:
    """Move references when constraints permit; preserve conflicting rows untouched."""
    conflicts = 0
    excluded = {"rs_instrument_identity_merges", "rs_instruments"}
    for table in sorted(_tables(connection) - excluded):
        foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        columns = sorted({str(row[3]) for row in foreign_keys if str(row[2]) == "rs_instruments"})
        for column in columns:
            rows = connection.execute(
                f'SELECT rowid FROM "{table}" WHERE "{column}"=? ORDER BY rowid', (loser,)
            ).fetchall()
            for (rowid,) in rows:
                try:
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (survivor, rowid)
                    )
                except sqlite3.IntegrityError:
                    conflicts += 1
    return conflicts


def _deactivate_official_test_issues(connection: sqlite3.Connection, stamp: str) -> int:
    path = Path(__file__).resolve().parents[2] / "resources" / "RangeScout_Nasdaq_Test_Issues.json"
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbols = tuple(sorted({str(row["symbol"]).upper() for row in payload.get("rows", ())}))
    if not symbols:
        return 0
    placeholders = ",".join("?" for _ in symbols)
    rows = connection.execute(
        f"""SELECT instrument_id FROM rs_instruments i WHERE i.is_active=1
            AND i.canonical_symbol IN ({placeholders})
            AND EXISTS(SELECT 1 FROM rs_instrument_aliases a WHERE a.instrument_id=i.instrument_id AND a.source_id=?)
            AND NOT EXISTS(SELECT 1 FROM rs_instrument_reference_sources r WHERE r.instrument_id=i.instrument_id AND r.source_id<>?)""",
        (*symbols, _DISCOVERY_SOURCE, _DISCOVERY_SOURCE),
    ).fetchall()
    for (instrument_id,) in rows:
        connection.execute(
            """UPDATE rs_instruments SET is_active=0,metadata_source='official_test_issue_filtered',
               metadata_updated_utc=?,updated_at_utc=? WHERE instrument_id=?""",
            (stamp, stamp, int(instrument_id)),
        )
        connection.execute("UPDATE rs_provider_symbols SET is_active=0 WHERE instrument_id=?", (int(instrument_id),))
    return len(rows)


def repair_r8_discovery_duplicates(connection: sqlite3.Connection) -> dict[str, int]:
    """Collapse only exact master/discovery clone pairs, without deleting any row."""
    required = {"rs_instruments", "rs_instrument_aliases", "rs_instrument_reference_sources"}
    if not required.issubset(_tables(connection)):
        return {"duplicate_groups_before": 0, "merged": 0, "reference_conflicts": 0, "duplicate_groups_after": 0}
    groups = connection.execute(
        """SELECT canonical_symbol FROM rs_instruments WHERE is_active=1
           GROUP BY canonical_symbol HAVING COUNT(*)>1 ORDER BY canonical_symbol"""
    ).fetchall()
    before, merged, conflicts = len(groups), 0, 0
    stamp = datetime.now(timezone.utc).isoformat()
    for (symbol,) in groups:
        rows = connection.execute(
            """SELECT instrument_id,security_name,primary_venue FROM rs_instruments
               WHERE canonical_symbol=? AND is_active=1 ORDER BY instrument_id""", (symbol,)
        ).fetchall()
        masters = [row for row in rows if _has_provenance(connection, int(row[0]), discovery=False)]
        clones = [row for row in rows if _has_provenance(connection, int(row[0]), discovery=True)
                  and not _has_provenance(connection, int(row[0]), discovery=False)]
        if len(masters) != 1:
            continue
        survivor_row, survivor = masters[0], int(masters[0][0])
        for clone in clones:
            loser = int(clone[0])
            names_match = _name(survivor_row[1]) == _name(clone[1])
            official_identity = _official_identity_agrees(connection, survivor, loser, str(symbol))
            if _venue(survivor_row[2]) != _venue(clone[2]) or not (names_match or official_identity):
                continue
            _retain_provenance(connection, survivor, loser, stamp)
            conflicts += _remap_nonconflicting_references(connection, loser, survivor)
            connection.execute(
                """INSERT OR REPLACE INTO rs_instrument_identity_merges
                   (old_instrument_id,survivor_instrument_id,reason,merged_at_utc) VALUES(?,?,?,?)""",
                (loser, survivor, "r8_parallel_official_directory_identity", stamp),
            )
            connection.execute(
                """UPDATE rs_instruments SET is_active=0,metadata_source='r9_identity_merge',
                   metadata_updated_utc=?,updated_at_utc=? WHERE instrument_id=?""",
                (stamp, stamp, loser),
            )
            merged += 1
    test_issues_deactivated = _deactivate_official_test_issues(connection, stamp)
    after = int(connection.execute(
        """SELECT COUNT(*) FROM (SELECT canonical_symbol FROM rs_instruments WHERE is_active=1
           GROUP BY canonical_symbol HAVING COUNT(*)>1)"""
    ).fetchone()[0])
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('r9_discovery_repair_merged',?)", (str(merged),))
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('r9_discovery_repair_conflicts',?)", (str(conflicts),))
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('r9_test_issues_deactivated',?)", (str(test_issues_deactivated),))
    return {"duplicate_groups_before": before, "merged": merged, "reference_conflicts": conflicts, "test_issues_deactivated": test_issues_deactivated, "duplicate_groups_after": after}
