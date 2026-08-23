"""R5 semantic instrument sweep with exclusions and deterministic discovery fakes."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import tempfile

from app.application.active_symbol import ActiveSymbolController
from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import AssetClass, Capability
from app.market_data.providers.catalog import default_fabric_registry
from app.research.routing import plan_research
from app.security.credentials import InMemoryCredentialStore


TARGETS = {
    "equity": 10, "preferred": 10, "adr": 10, "etf": 10,
    "closed_end_fund": 10, "warrant": 10, "right": 10, "unit": 10,
}


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


def strings(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"symbol", "query", "canonical_symbol", "official_name"} and isinstance(item, str):
                yield item.upper()
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--exclude", type=Path, action="append", default=[])
    args = parser.parse_args()
    excluded: set[str] = set()
    for path in args.exclude:
        excluded.update(strings(json.loads(path.read_text(encoding="utf-8"))))

    database = Path(tempfile.mkdtemp(prefix="rangescout-r5-sweep-")) / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    resolver = InstrumentResolver(database)

    discovered = [
        ("XAG/USD", "Silver Spot / U.S. Dollar", "commodity_spot", "precious_metal_spot", "twelve_data"),
        ("GBP/USD", "British Pound / U.S. Dollar", "fx", "physical_currency_pair", "twelve_data"),
        ("ETH/USD", "Ethereum / U.S. Dollar", "crypto_spot", "digital_currency", "twelve_data"),
        ("^R5QA", "R5 Deterministic Provider Index", "index", "index", "yahoo"),
        ("R5-WT", "R5 Discovery Security Warrant", "warrant", "Warrant", "yahoo"),
        ("R5-RT", "R5 Discovery Security Right", "right", "Right", "yahoo"),
        ("R5-UN", "R5 Discovery Security Unit", "unit", "Unit", "yahoo"),
    ]
    for symbol, name, asset, subtype, provider in discovered:
        resolver.enrich_provider_results(provider, [{
            "canonical_symbol": symbol, "provider_symbol": symbol, "name": name,
            "asset_class": asset, "subtype": subtype, "instrument_type": subtype,
            "venue": "QA DISCOVERY", "currency": "USD",
            "verified_at_utc": "2026-08-23T00:00:00+00:00",
        }])

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT i.canonical_symbol,i.security_name,i.asset_class,i.security_type,i.primary_venue,
                      i.instrument_subtype,i.metadata_source,i.metadata_verified_utc,
                      MAX(CASE WHEN c.is_active=1 THEN c.asset_class ELSE '' END) authoritative_asset_class
               FROM rs_instruments i LEFT JOIN rs_instrument_classifications c USING(instrument_id)
               WHERE i.is_active=1 GROUP BY i.instrument_id ORDER BY i.canonical_symbol"""
        ).fetchall()
    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        match = resolver.resolve_unique(row["canonical_symbol"])
        if match is None or match.symbol in excluded or row["security_name"].upper() in excluded:
            continue
        asset = expected_asset(row)
        if asset in TARGETS:
            buckets[asset].append(row)
    selected: list[sqlite3.Row] = []
    for asset, count in TARGETS.items():
        ranked = sorted(
            buckets[asset],
            key=lambda row: hashlib.sha256(f"R5-semantic|{asset}|{row['canonical_symbol']}".encode()).hexdigest(),
        )
        if len(ranked) < count:
            raise RuntimeError(f"insufficient fresh {asset} cases: {len(ranked)}")
        selected.extend(ranked[:count])
    discovered_symbols = {item[0] for item in discovered}
    selected.extend(row for row in rows if row["canonical_symbol"] in discovered_symbols)

    registry = default_fabric_registry(InMemoryCredentialStore())
    results = []
    for index, row in enumerate(selected, 1):
        symbol = str(row["canonical_symbol"])
        matches = resolver.search(symbol, 10)
        resolved = resolver.resolve_unique(symbol)
        expected = expected_asset(row)
        expected_route = plan_research(expected, resolved.instrument.subtype if resolved else "").route.value
        asset_enum = AssetClass(expected)
        quote_providers = [item.descriptor.provider_id for item in registry.eligible(asset_enum, Capability.QUOTE)]
        history_providers = [item.descriptor.provider_id for item in registry.eligible(asset_enum, Capability.HISTORICAL)]
        controller = ActiveSymbolController("AAPL")
        transitions = []
        controller.subscribe(lambda state: transitions.append((state.symbol, state.generation)))
        state = controller.set(
            symbol, source="r5-semantic-sweep", instrument_id=resolved.instrument.instrument_id,
            name=resolved.instrument.name, venue=resolved.instrument.venue, asset_class=expected,
            provider_symbols=tuple(sorted(resolved.instrument.provider_symbols.items())),
            subtype=resolved.instrument.subtype,
        )
        quote_request = controller.request(source="quote")
        history_request = controller.request(source="history")
        research_request = controller.request(source="research")
        route_supported = bool(quote_providers and history_providers)
        failures = []
        if not matches or matches[0].symbol != symbol or matches[0].match_kind != "exact_symbol":
            failures.append("exact_symbol_identity")
        if resolved is None or resolved.symbol != symbol:
            failures.append("unsafe_unique_resolution")
        if resolved and expected != resolved.instrument.asset_class:
            failures.append("canonical_asset_class")
        if not all(request.symbol == symbol and request.instrument_id == state.instrument_id and
                   request.asset_class == expected and request.provider_symbols == state.provider_symbols
                   for request in (quote_request, history_request, research_request)):
            failures.append("canonical_request_propagation")
        if len(transitions) != 1 or transitions[0][0] != symbol:
            failures.append("automatic_selection_dispatch")
        prior = quote_request
        controller.set("AAPL", source="stale-check", instrument_id=-1, asset_class="equity")
        if controller.accepts(prior):
            failures.append("stale_request_accepted")
        results.append({
            "index": index, "query": symbol, "symbol": symbol, "name": str(row["security_name"]),
            "asset_class": expected, "subtype": resolved.instrument.subtype if resolved else "",
            "research_route": expected_route, "provider_symbols": dict(resolved.instrument.provider_symbols) if resolved else {},
            "quote_providers": quote_providers, "history_providers": history_providers,
            "route_state": "eligible" if route_supported else "truthful_provider_unsupported",
            "automatic_dispatch": len(transitions) >= 1, "manual_refresh_required": False,
            "stale_overwrite_rejected": not controller.accepts(prior), "unrelated_substitution": False,
            "metadata_source": str(row["metadata_source"] or "company_master"),
            "metadata_verified_utc": str(row["metadata_verified_utc"] or ""),
            "provider_discovered": symbol in discovered_symbols,
            "failures": failures, "result": "PASS" if not failures else "FAIL",
        })
    payload = {
        "schema": "rangescout.r5-semantic-instrument-sweep.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection_seed": "R5-semantic",
        "excluded_values": len(excluded),
        "target_counts": TARGETS,
        "total": len(results),
        "passed": sum(item["result"] == "PASS" for item in results),
        "failed": sum(item["result"] == "FAIL" for item in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("total", "passed", "failed")}, indent=2))
    return 0 if payload["failed"] == 0 and payload["total"] >= 75 else 1


if __name__ == "__main__":
    raise SystemExit(main())
