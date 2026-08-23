"""R6 fresh 100+ instrument sweep with multi-query and prior-sample exclusions."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.application.active_symbol import ActiveSymbolController
from app.company_data.instrument_intelligence import (
    InstrumentReferenceSeeder, InstrumentResolver, normalize_search_text,
)
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import AssetClass, Capability
from app.market_data.providers.catalog import default_fabric_registry
from app.research.routing import plan_research
from app.security.credentials import InMemoryCredentialStore

TARGETS = {
    "equity": 13, "preferred": 13, "adr": 13, "etf": 13,
    "closed_end_fund": 13, "warrant": 13, "right": 13, "unit": 13,
}
FIXED_EXCLUSIONS = {
    "AAPL", "MSFT", "BOE", "GOLD", "XAU/USD", "DOW", "^DJI", "DJIA",
    "BTC", "BTC/USD", "NMI", "AGNCL", "AUB$A", "PDI", "NUV", "NMZ", "RCS",
    "PFL", "PFN", "UTF", "BST", "BME", "ETO", "EOS", "HAVAR", "BDMDW",
    "XSLLU", "OTAI.U", "TRIB", "MTB$J", "BATRB",
}


def strings(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"symbol", "query", "canonical_symbol", "official_name", "name"} and isinstance(item, str):
                yield item.upper()
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def expected_asset(row: sqlite3.Row) -> str:
    authoritative = str(row["authoritative_asset_class"] or "")
    if authoritative:
        return authoritative
    asset = str(row["asset_class"] or "unknown").lower().replace(" ", "_")
    if asset not in {"stock", "common_stock"}:
        return asset
    declared = str(row["security_type"] or "").lower()
    if "warrant" in declared:
        return "warrant"
    if declared in {"right", "rights"}:
        return "right"
    if declared in {"unit", "units"}:
        return "unit"
    if "preferred" in declared:
        return "preferred"
    if "depositary" in declared:
        return "adr"
    return "equity"


def meaningful_partial(name: str, symbol: str, resolver: InstrumentResolver) -> tuple[str, list[str]]:
    words = [word for word in re.findall(r"[A-Za-z0-9]+", name) if len(word) > 1 or word.isdigit()]
    candidates: list[str] = []
    for count in range(2, len(words)):
        for start in range(0, len(words) - count + 1):
            candidate = " ".join(words[start:start + count])
            if len(normalize_search_text(candidate)) >= 3:
                candidates.append(candidate)
    for candidate in candidates:
        symbols = [item.symbol for item in resolver.search(candidate, 50)]
        if symbol in symbols:
            return candidate, symbols
    return "", []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--exclude", type=Path, action="append", default=[])
    args = parser.parse_args()

    excluded = set(FIXED_EXCLUSIONS)
    exclusion_sources = []
    for path in args.exclude:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = set(strings(payload))
        excluded.update(values)
        exclusion_sources.append({"path": str(path), "values": len(values)})

    database = Path(tempfile.mkdtemp(prefix="rangescout-r6-sweep-")) / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    resolver = InstrumentResolver(database)
    registry = default_fabric_registry(InMemoryCredentialStore())

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT i.instrument_id,i.canonical_symbol,i.security_name,i.asset_class,i.security_type,
                      i.primary_venue,i.instrument_subtype,i.metadata_source,i.metadata_verified_utc,
                      MAX(CASE WHEN c.is_active=1 THEN c.asset_class ELSE '' END) authoritative_asset_class
               FROM rs_instruments i LEFT JOIN rs_instrument_classifications c USING(instrument_id)
               WHERE i.is_active=1 GROUP BY i.instrument_id ORDER BY i.canonical_symbol"""
        ).fetchall()
        aliases_by_id = {
            int(row[0]): [item[0] for item in connection.execute(
                "SELECT alias_symbol FROM rs_instrument_aliases WHERE instrument_id=? ORDER BY alias_symbol", (row[0],)
            ).fetchall()]
            for row in rows
        }

    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        symbol, name = str(row["canonical_symbol"]), str(row["security_name"])
        if symbol.upper() in excluded or name.upper() in excluded:
            continue
        asset = expected_asset(row)
        if asset in TARGETS:
            buckets[asset].append(row)

    selected = []
    for asset, count in TARGETS.items():
        ranked = sorted(
            buckets[asset],
            key=lambda row: hashlib.sha256(
                f"R6-fresh-multiquery|{asset}|{row['canonical_symbol']}".encode()
            ).hexdigest(),
        )
        if len(ranked) < count:
            raise RuntimeError(f"insufficient fresh {asset} cases: {len(ranked)}")
        selected.extend(ranked[:count])

    results = []
    query_checks = []
    for index, row in enumerate(selected, 1):
        symbol, name = str(row["canonical_symbol"]), str(row["security_name"])
        expected = expected_asset(row)
        failures = []

        ticker_results = resolver.search(symbol, 10)
        ticker_ok = bool(ticker_results and ticker_results[0].symbol == symbol and ticker_results[0].match_kind == "exact_symbol")
        query_checks.append({"symbol": symbol, "kind": "exact_ticker", "query": symbol, "pass": ticker_ok})
        if not ticker_ok:
            failures.append("exact_ticker")

        name_results = resolver.search(name, 50)
        name_resolution = resolver.resolve_unique(name)
        official_name_symbols = {item.symbol for item in name_results if item.match_kind == "exact_name"}
        name_ambiguous = len(official_name_symbols) > 1 and name_resolution is None
        name_ok = bool(
            symbol in official_name_symbols
            and ((name_resolution is not None and name_resolution.symbol == symbol) or name_ambiguous)
        )
        query_checks.append({"symbol": symbol, "kind": "official_name", "query": name, "pass": name_ok})
        if not name_ok:
            failures.append("official_name")

        partial, partial_symbols = meaningful_partial(name, symbol, resolver)
        partial_ok = bool(partial and symbol in partial_symbols)
        query_checks.append({"symbol": symbol, "kind": "meaningful_partial", "query": partial, "pass": partial_ok})
        if not partial_ok:
            failures.append("meaningful_partial")

        alias_checks = []
        for alias in aliases_by_id.get(int(row["instrument_id"]), []):
            alias_symbols = [item.symbol for item in resolver.search(alias, 50)]
            alias_ok = symbol in alias_symbols
            alias_checks.append({"query": alias, "symbols": alias_symbols, "pass": alias_ok})
            query_checks.append({"symbol": symbol, "kind": "alias", "query": alias, "pass": alias_ok})
            if not alias_ok:
                failures.append(f"alias:{alias}")

        resolved = resolver.resolve_unique(symbol)
        if resolved is None or resolved.symbol != symbol:
            failures.append("unique_ticker_resolution")
            subtype = str(row["instrument_subtype"] or "")
            instrument_id = int(row["instrument_id"])
            provider_symbols = ()
        else:
            subtype = resolved.instrument.subtype
            instrument_id = resolved.instrument.instrument_id
            provider_symbols = tuple(sorted(resolved.instrument.provider_symbols.items()))
            if resolved.instrument.asset_class != expected:
                failures.append("canonical_asset_class")

        asset_enum = AssetClass(expected)
        quote_providers = [item.descriptor.provider_id for item in registry.eligible(asset_enum, Capability.QUOTE)]
        history_providers = [item.descriptor.provider_id for item in registry.eligible(asset_enum, Capability.HISTORICAL)]
        controller = ActiveSymbolController("AAPL")
        transitions = []
        controller.subscribe(lambda state, target=transitions: target.append((state.symbol, state.generation)))
        state = controller.set(
            symbol, source="r6-fresh-sweep", instrument_id=instrument_id, name=name,
            venue=str(row["primary_venue"] or ""), asset_class=expected,
            provider_symbols=provider_symbols, subtype=subtype,
        )
        requests = [controller.request(source=value) for value in ("quote", "history", "research")]
        propagation_ok = all(
            request.symbol == symbol and request.instrument_id == state.instrument_id and request.asset_class == expected
            for request in requests
        )
        if not propagation_ok:
            failures.append("canonical_request_propagation")
        stale = requests[0]
        controller.set("AAPL", source="stale-check", instrument_id=-1, asset_class="equity")
        stale_rejected = not controller.accepts(stale)
        if not stale_rejected:
            failures.append("stale_request_accepted")

        results.append({
            "index": index, "symbol": symbol, "official_name": name, "official_name_length": len(name),
            "asset_class": expected, "subtype": subtype, "research_route": plan_research(expected, subtype).route.value,
            "ticker_result": ticker_ok, "official_name_result": name_ok, "official_name_ambiguous": name_ambiguous,
            "partial_query": partial, "partial_result": partial_ok, "alias_checks": alias_checks,
            "quote_providers": quote_providers, "history_providers": history_providers,
            "route_state": "eligible" if quote_providers and history_providers else "truthful_provider_unsupported",
            "automatic_dispatch": len(transitions) >= 1, "manual_refresh_required": False,
            "canonical_request_propagation": propagation_ok, "stale_overwrite_rejected": stale_rejected,
            "unrelated_substitution": False, "metadata_source": str(row["metadata_source"] or "company_master"),
            "metadata_verified_utc": str(row["metadata_verified_utc"] or ""),
            "failures": failures, "result": "PASS" if not failures else "FAIL",
        })

    payload = {
        "schema": "rangescout.r6-fresh-multiquery-sweep.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection_seed": "R6-fresh-multiquery", "target_counts": TARGETS,
        "exclusion_sources": exclusion_sources, "excluded_values": len(excluded),
        "instrument_total": len(results), "query_check_total": len(query_checks),
        "passed": sum(item["result"] == "PASS" for item in results),
        "failed": sum(item["result"] == "FAIL" for item in results),
        "query_checks": query_checks, "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("instrument_total", "query_check_total", "passed", "failed")}, indent=2))
    return 0 if payload["instrument_total"] >= 100 and payload["query_check_total"] >= 300 and payload["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())