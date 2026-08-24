"""Exhaustive R9 identity, provider-route, issuer, and Research semantic audit."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3
import sys
import tempfile
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.company_data.instrument_intelligence import (
    InstrumentReferenceSeeder,
    InstrumentResolver,
    canonical_asset_class,
    default_issuer_entity_type,
)
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import AssetClass, Capability, FabricRequest
from app.market_data.provider_symbols import normalize_yahoo_symbol
from app.market_data.discovery import DiscoveryCoordinator, InstrumentDiscovery, parse_nasdaq_directory
from app.market_data.providers.catalog import default_fabric_registry
from app.research.routing import plan_research
from app.security.credentials import InMemoryCredentialStore


CEF_COMMON_MARKER = re.compile(r"\bcommon\s+(?:stock|shares?|units?)(?:\s+of\s+beneficial\s+interest)?\b", re.I)
CEF_PREFERRED_MARKER = re.compile(
    r"(?:\bterm\s+preferred\b|\bseries\s+[a-z0-9-]+\s+(?:term\s+)?preferred\b|"
    r"\bpreferred\s+(?:stock|shares?)\b(?=.*(?:\bseries\b|\bdue\b|\d(?:\.\d+)?%))|"
    r"\bdepositary\s+shares?.*\bpreferred\b)", re.I,
)
CEF_ALTERNATE_MARKER = re.compile(r"\b(warrant|right|unit|note|depositary share)", re.I)


def _independent_cef_role(name: str) -> str:
    if CEF_COMMON_MARKER.search(name):
        return "primary_common"
    if CEF_PREFERRED_MARKER.search(name):
        return "preferred_security"
    if CEF_ALTERNATE_MARKER.search(name):
        return "alternate_security"
    return "primary_common"


CAPABILITIES = (Capability.QUOTE, Capability.HISTORICAL)
QA_PLACEHOLDER = "NONE."


def _provider_plan(registry, instrument, asset: AssetClass, capability: Capability) -> dict[str, object]:
    overrides = tuple(sorted(instrument.provider_symbols.items()))
    request = FabricRequest(
        canonical_instrument_id=instrument.identity,
        canonical_symbol=instrument.symbol,
        asset_class=asset,
        capability=capability,
        venue=instrument.venue,
        provider_symbol_overrides=overrides,
    )
    routes: list[dict[str, object]] = []
    for adapter in registry.eligible(asset, capability):
        provider_id = adapter.descriptor.provider_id
        override = dict(overrides).get(provider_id)
        adapter_request = replace(request, canonical_symbol=override) if override else request
        try:
            provider_symbol = adapter.provider_symbol_for(adapter_request)
            if provider_id == "yahoo":
                provider_symbol = normalize_yahoo_symbol(provider_symbol)
            routes.append({
                "provider_id": provider_id,
                "provider_symbol": provider_symbol,
                "mapping": "explicit" if override else "canonical",
                "status": "supported",
            })
        except Exception as exc:
            routes.append({
                "provider_id": provider_id,
                "provider_symbol": None,
                "mapping": "explicit" if override else "canonical",
                "status": "unsupported",
                "reason": str(exc),
            })
    usable = [route for route in routes if route["status"] == "supported"]
    return {
        "capability": capability.value,
        "status": "supported" if usable else "unsupported",
        "reason": None if usable else "no_eligible_provider_with_valid_symbol_mapping",
        "routes": routes,
    }


def _database(refresh_count: int) -> tuple[Path, list[dict[str, object]]]:
    database = Path(tempfile.mkdtemp(prefix="rangescout-r9-exhaustive-")) / "history.sqlite"
    with HistoricalStore(database):
        pass
    provision_company_master(database)
    InstrumentReferenceSeeder(database).apply()
    reports: list[dict[str, object]] = []
    if refresh_count:
        root = Path("docs/engineering/v1.6/company_master_sources")
        first_text = (root / "nasdaq_nasdaqlisted.txt").read_text(encoding="utf-8-sig", errors="replace")
        second_text = (root / "nasdaq_otherlisted.txt").read_text(encoding="utf-8-sig", errors="replace")
        first, first_errors = parse_nasdaq_directory(first_text, "Q")
        second, second_errors = parse_nasdaq_directory(second_text, "N")
        raw = (first_text + "\n" + second_text).encode("utf-8")
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            discovery = InstrumentDiscovery(connection)
            for _index in range(refresh_count):
                report = discovery.import_snapshot(
                    DiscoveryCoordinator.SOURCE_ID, DiscoveryCoordinator.DISPLAY_NAME,
                    DiscoveryCoordinator.OFFICIAL_URL, first + second, raw,
                    parse_errors=first_errors + second_errors,
                )
                reports.append(report.__dict__)
    return database, reports


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--refresh-count", type=int, choices=(0, 1, 2), default=0)
    args = parser.parse_args()
    started = monotonic()
    database, refresh_reports = _database(args.refresh_count)
    resolver = InstrumentResolver(database)
    registry = default_fabric_registry(InMemoryCredentialStore())
    classification_payload = json.loads(
        Path("resources/RangeScout_Instrument_Classifications.json").read_text(encoding="utf-8")
    )
    authoritative_cef_ciks = {
        str(record["cik"]).zfill(10) for record in classification_payload["classifications"]
    }
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT instrument_id,canonical_symbol,security_name,asset_class,security_type,
                      COALESCE(instrument_subtype,'') instrument_subtype,primary_venue,
                      issuer_entity_type,security_role,COALESCE(cik,'') cik
               FROM rs_instruments WHERE is_active=1 ORDER BY canonical_symbol,instrument_id"""
        ).fetchall()
        if args.limit > 0:
            rows = rows[:args.limit]
        yahoo_support = {
            (int(row[0]), str(row[1])): (str(row[2]), str(row[3]))
            for row in connection.execute(
                """SELECT instrument_id,capability,support_status,reason
                   FROM rs_provider_instrument_support WHERE provider_id='yahoo'"""
            )
        }
    results: list[dict[str, object]] = []
    failure_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    name_ranks: Counter[int] = Counter()
    for index, row in enumerate(rows, 1):
        instrument_id = int(row["instrument_id"])
        symbol = str(row["canonical_symbol"])
        name = str(row["security_name"])
        failures: list[str] = []
        try:
            symbol_results = resolver.search(symbol, 10)
            symbol_ok = bool(
                symbol_results
                and int(symbol_results[0].instrument.instrument_id) == instrument_id
                and symbol_results[0].match_kind == "exact_symbol"
            )
            if not symbol_ok:
                failures.append("exact_symbol")
            name_results = resolver.search(name, 50)
            expected_positions = [
                position for position, item in enumerate(name_results, 1)
                if int(item.instrument.instrument_id) == instrument_id
            ]
            name_rank = expected_positions[0] if expected_positions else None
            if name_rank is None:
                failures.append("full_official_name")
            else:
                name_ranks[name_rank] += 1
            instrument = resolver.by_id(instrument_id)
            if instrument is None:
                raise RuntimeError("canonical instrument disappeared during audit")
            base_expected_asset = canonical_asset_class(
                row["asset_class"], row["instrument_subtype"], row["security_type"], name
            )
            cik = str(row["cik"] or "").zfill(10) if row["cik"] else ""
            authoritative_cef = cik in authoritative_cef_ciks
            expected_role = _independent_cef_role(name) if authoritative_cef else instrument.security_role
            expected_asset = (
                "closed_end_fund" if authoritative_cef and expected_role == "primary_common" else
                "preferred" if authoritative_cef and expected_role == "preferred_security" else
                base_expected_asset
            )
            expected_issuer = "closed_end_fund" if authoritative_cef else default_issuer_entity_type(expected_asset)
            if instrument.asset_class != expected_asset:
                failures.append("classification")
            if instrument.issuer_type != expected_issuer:
                failures.append("issuer_entity_type")
            if authoritative_cef and instrument.security_role != expected_role:
                failures.append("security_role")
            asset = AssetClass(expected_asset)
            plans = {
                capability.value: _provider_plan(registry, instrument, asset, capability)
                for capability in CAPABILITIES
            }
            for capability, plan in plans.items():
                route_counts[f"{capability}:{plan['status']}"] += 1
                support = yahoo_support.get((instrument_id, capability))
                yahoo_routes = [route for route in plan["routes"] if route["provider_id"] == "yahoo"]
                if support and support[0] == "supported" and not any(
                    route["status"] == "supported" for route in yahoo_routes
                ):
                    failures.append(f"{capability}_yahoo_support_mismatch")
            research = plan_research(
                instrument.asset_class, instrument.subtype, instrument.issuer_type, instrument.security_role
            )
            expected_research_route = (
                "fund" if expected_issuer == "closed_end_fund" or expected_asset in {"closed_end_fund", "etf", "mutual_fund"}
                else "corporate" if expected_asset in {"equity", "preferred", "adr", "otc"}
                else "market_instrument"
            )
            if research.route.value != expected_research_route or not research.visible_sections:
                failures.append("research_semantics")
            if expected_issuer == "closed_end_fund" and research.analyst_applicable:
                failures.append("cef_corporate_analyst_route")
            result = {
                "index": index,
                "instrument_id": instrument_id,
                "canonical_symbol": symbol,
                "security_name": name,
                "classification": instrument.asset_class,
                "subtype": instrument.subtype,
                "issuer_entity_type": instrument.issuer_type,
                "security_role": instrument.security_role,
                "cik": cik,
                "authoritative_cef": authoritative_cef,
                "expected_security_role": expected_role,
                "expected_research_route": expected_research_route,
                "exact_symbol": "PASS" if symbol_ok else "FAIL",
                "full_name_result": "PASS" if name_rank is not None else "FAIL",
                "official_name_rank": name_rank,
                "provider_symbols": dict(sorted(instrument.provider_symbols.items())),
                "quote_plan": plans["quote"],
                "history_plan": plans["historical"],
                "research_route": research.route.value,
                "research_state": research.state.value,
                "unsupported_is_explicit": all(
                    plan["status"] == "supported" or bool(plan["reason"]) for plan in plans.values()
                ),
                "route_exception": None,
                "failures": failures,
                "result": "PASS" if not failures else "FAIL",
            }
        except Exception as exc:
            failures.append("route_exception")
            result = {
                "index": index,
                "instrument_id": instrument_id,
                "canonical_symbol": symbol,
                "security_name": name,
                "route_exception": f"{type(exc).__name__}: {exc}",
                "failures": failures,
                "result": "FAIL",
            }
        for failure in failures:
            failure_counts[failure] += 1
        results.append(result)
        if index % 1000 == 0:
            print(f"{index}/{len(rows)} failures={sum(failure_counts.values())} categories={dict(failure_counts)}", flush=True)
    yahoo_rows = [result for result in results if "$" in str(result["canonical_symbol"])]
    payload = {
        "schema": "rangescout.r9-exhaustive-semantic-instrument-audit.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_database": str(database),
        "refresh_count": args.refresh_count,
        "refresh_reports": refresh_reports,
        "active_population": len(results),
        "symbol_passed": sum(result.get("exact_symbol") == "PASS" for result in results),
        "full_name_passed": sum(result.get("full_name_result") == "PASS" for result in results),
        "route_exceptions": failure_counts["route_exception"],
        "failed_instruments": sum(result["result"] == "FAIL" for result in results),
        "failure_counts": dict(sorted(failure_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "full_name_rank_counts": {str(key): value for key, value in sorted(name_ranks.items())},
        "authoritative_cef_ciks": len(authoritative_cef_ciks),
        "semantic_passed": sum("research_semantics" not in result.get("failures", ()) and
                               "issuer_entity_type" not in result.get("failures", ()) and
                               "security_role" not in result.get("failures", ()) for result in results),
        "ordinary_corporate_cef_misroutes": sum(
            result.get("authoritative_cef") and result.get("research_route") == "corporate"
            for result in results
        ),
        "yahoo_incompatible_r6_audit": {
            "population": len(yahoo_rows) + 1,
            "active_dollar_symbols": len(yahoo_rows),
            "placeholder_symbol": QA_PLACEHOLDER,
            "placeholder_active": any(result["canonical_symbol"] == QA_PLACEHOLDER for result in results),
            "mapped_supported": sum(
                any(route["provider_id"] == "yahoo" and route["status"] == "supported"
                    for route in result.get("quote_plan", {}).get("routes", ()))
                for result in yahoo_rows
            ),
            "rows": yahoo_rows,
        },
        "elapsed_seconds": round(monotonic() - started, 3),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {key: payload[key] for key in (
        "active_population", "symbol_passed", "full_name_passed", "route_exceptions", "failed_instruments",
        "route_counts", "authoritative_cef_ciks", "semantic_passed",
        "ordinary_corporate_cef_misroutes", "elapsed_seconds",
    )}
    print(json.dumps(summary, indent=2, sort_keys=True))
    yahoo = payload["yahoo_incompatible_r6_audit"]
    return 0 if (
        payload["active_population"] >= 16_000
        and payload["symbol_passed"] == payload["active_population"]
        and payload["full_name_passed"] == payload["active_population"]
        and payload["route_exceptions"] == 0
        and payload["failed_instruments"] == 0
        and payload["authoritative_cef_ciks"] == 355
        and payload["semantic_passed"] == payload["active_population"]
        and payload["ordinary_corporate_cef_misroutes"] == 0
        and yahoo["population"] == 385
        and yahoo["active_dollar_symbols"] == yahoo["mapped_supported"] == 384
        and not yahoo["placeholder_active"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
