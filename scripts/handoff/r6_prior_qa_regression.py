"""Replay R4/R5 Independent-QA instrument datasets against the R6 resolver."""

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
from app.research.routing import plan_research


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    database = Path(tempfile.mkdtemp(prefix="rangescout-r6-priorqa-")) / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    resolver = InstrumentResolver(database)
    results = []
    for prior in source["rows"]:
        symbol = str(prior["symbol"])
        name = str(prior["official_name"])
        partial = str(prior["partial_query"])
        failures = []
        symbol_results = resolver.search(symbol, 10)
        symbol_unique = resolver.resolve_unique(symbol)
        if not symbol_results or symbol_results[0].symbol != symbol or symbol_results[0].match_kind != "exact_symbol":
            failures.append("exact_symbol")
        if symbol_unique is None or symbol_unique.symbol != symbol:
            failures.append("unique_symbol")
        resolved_asset = symbol_results[0].instrument.asset_class if symbol_results else ""
        route = plan_research(resolved_asset, symbol_results[0].instrument.subtype if symbol_results else "").route.value
        if resolved_asset != prior["expected_asset_class"]:
            failures.append("asset_class")
        if route != prior["expected_research_route"]:
            failures.append("research_route")
        name_results = resolver.search(name, 50)
        exact_name_symbols = {item.symbol for item in name_results if item.match_kind == "exact_name"}
        name_unique = resolver.resolve_unique(name)
        identical_count = int(prior.get("official_name_identical_active_count", 1))
        if symbol not in exact_name_symbols:
            failures.append("official_name_missing")
        elif identical_count == 1 and (name_unique is None or name_unique.symbol != symbol):
            failures.append("official_name_unique")
        elif identical_count > 1 and name_unique is not None and name_unique.symbol != symbol:
            failures.append("official_name_unrelated_substitution")
        partial_results = resolver.search(partial, 50)
        partial_contains_target = symbol in {item.symbol for item in partial_results}
        results.append({
            "symbol": symbol, "official_name": name, "official_name_length": len(name),
            "partial_query": partial, "asset_class": resolved_asset, "research_route": route,
            "symbol_top": [(item.symbol, item.match_kind) for item in symbol_results[:5]],
            "official_name_top": [(item.symbol, item.match_kind) for item in name_results[:5]],
            "partial_top": [(item.symbol, item.match_kind) for item in partial_results[:5]],
            "partial_contains_target": partial_contains_target,
            "failures": failures, "result": "PASS" if not failures else "FAIL",
        })
    payload = {
        "schema": "rangescout.r6-prior-independent-qa-regression.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_dataset": str(args.input), "count": len(results),
        "pass_count": sum(item["result"] == "PASS" for item in results),
        "fail_count": sum(item["result"] == "FAIL" for item in results),
        "rows": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("count", "pass_count", "fail_count")}, indent=2))
    return 0 if payload["count"] == 50 and payload["fail_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())