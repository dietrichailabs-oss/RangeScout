"""R6 targeted ambiguity and all-long-official-name resolver evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore


CASES = {
    "gold": ({"GOLD", "XAU/USD"}, "XAU/USD"),
    "Gold": ({"GOLD", "XAU/USD"}, "XAU/USD"),
    "Dow": ({"DOW", "^DJI"}, "^DJI"),
    "DJIA": ({"DJIA", "^DJI"}, "DJIA"),
    "BTC": ({"BTC", "BTC/USD"}, "BTC"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    database = Path(tempfile.mkdtemp(prefix="rangescout-r6-targeted-")) / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    resolver = InstrumentResolver(database)

    collisions = []
    for query, (expected, preferred) in CASES.items():
        results = resolver.search(query, 10)
        symbols = [item.symbol for item in results]
        selected = next((item for item in results if item.symbol == preferred), None)
        canonical = resolver.by_id(selected.instrument.instrument_id) if selected else None
        passed = bool(
            results and results[0].symbol == preferred and expected.issubset(set(symbols))
            and resolver.resolve_unique(query) is None
            and canonical is not None and canonical.symbol == preferred
        )
        collisions.append({
            "query": query, "expected_symbols": sorted(expected), "preferred": preferred,
            "results": [{"symbol": item.symbol, "kind": item.match_kind, "score": item.score} for item in results],
            "resolve_unique": None, "explicit_canonical_selection": canonical.symbol if canonical else None,
            "result": "PASS" if passed else "FAIL",
        })

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT canonical_symbol,security_name,COUNT(*) OVER (PARTITION BY security_name) identical_name_count
               FROM rs_instruments WHERE is_active=1 AND LENGTH(security_name)>160
               ORDER BY canonical_symbol"""
        ).fetchall()
    long_names = []
    for row in rows:
        symbol, name, identical = str(row["canonical_symbol"]), str(row["security_name"]), int(row["identical_name_count"])
        results = resolver.search(name, 50)
        exact_symbols = {item.symbol for item in results if item.match_kind == "exact_name"}
        unique = resolver.resolve_unique(name)
        passed = symbol in exact_symbols and (
            (identical == 1 and unique is not None and unique.symbol == symbol)
            or (identical > 1 and (unique is None or unique.symbol == symbol))
        )
        long_names.append({
            "symbol": symbol, "official_name": name, "length": len(name),
            "identical_active_name_count": identical, "exact_name_symbols": sorted(exact_symbols),
            "resolved_unique": unique.symbol if unique else None, "result": "PASS" if passed else "FAIL",
        })

    nmi = resolver.search("NMI", 10)
    payload = {
        "schema": "rangescout.r6-targeted-resolver-evidence.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "collision_cases": collisions,
        "collision_passed": sum(item["result"] == "PASS" for item in collisions),
        "collision_failed": sum(item["result"] == "FAIL" for item in collisions),
        "long_name_count": len(long_names), "long_name_rows": long_names,
        "long_name_passed": sum(item["result"] == "PASS" for item in long_names),
        "long_name_failed": sum(item["result"] == "FAIL" for item in long_names),
        "exact_ticker_guard": {
            "query": "NMI", "top_symbol": nmi[0].symbol if nmi else None,
            "top_kind": nmi[0].match_kind if nmi else None,
            "resolved_unique": resolver.resolve_unique("NMI").symbol if resolver.resolve_unique("NMI") else None,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in (
        "collision_passed", "collision_failed", "long_name_count", "long_name_passed", "long_name_failed"
    )}, indent=2))
    return 0 if payload["collision_failed"] == 0 and payload["long_name_count"] >= 2 and payload["long_name_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())