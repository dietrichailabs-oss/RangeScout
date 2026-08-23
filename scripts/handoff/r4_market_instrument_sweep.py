"""Generate the deterministic R4 broad-market resolver sweep evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.company_data.instrument_intelligence import InstrumentResolver
from app.market_data.contracts import AssetClass, Capability
from app.market_data.providers.catalog import default_fabric_registry
from app.research.routing import plan_research
from app.security.credentials import InMemoryCredentialStore


SWEEP_QUERIES = (
    "Apple Inc", "Microsoft", "Boeing Company", "Oracle", "Ford Motor", "Intel",
    "JPMorgan", "Coca Cola", "Berkshire Hathaway", "Tesla", "NVIDIA", "Amazon",
    "Walmart", "Costco", "Adobe", "Salesforce", "Uber", "Airbnb", "Palantir",
    "Snowflake", "Rivian", "Lucid", "GameStop", "AMC Entertainment",
    "BlackRock Enhanced", "PIMCO Dynamic Income", "Nuveen Municipal",
    "SPY", "QQQ", "IWM", "DIA", "VTI", "EEM", "TLT", "HYG", "ARKK", "GLD",
    "SLV", "USO", "BA$A", "JPM$D", "BAC$L", "WFC$L", "BRK.A", "BRK.B",
    "Dow Jones", "Dow 30", "S&P 500", "SPX", "Nasdaq Composite", "Gold Spot",
    "XAU/USD", "EUR/USD", "Euro Dollar", "Bitcoin", "BTC/USD", "Alphabet",
    "Meta Platforms", "Coca", "BlackRock", "Global Dividend", "Commonwealth",
    "Toyota Motor", "7203",
)


def run(database_path: Path) -> dict[str, object]:
    resolver = InstrumentResolver(database_path)
    registry = default_fabric_registry(InMemoryCredentialStore())
    rows: list[dict[str, object]] = []
    for query in SWEEP_QUERIES:
        matches = resolver.search(query, 5)
        unique = resolver.resolve_unique(query)
        selected = unique or (matches[0] if len(matches) == 1 else None)
        instrument = selected.instrument if selected else None
        asset = None
        if instrument is not None:
            try:
                asset = AssetClass(instrument.asset_class)
            except ValueError:
                asset = AssetClass.UNKNOWN
        quote_providers = [
            adapter.descriptor.provider_id for adapter in registry.snapshot()
            if asset in adapter.descriptor.asset_classes and Capability.QUOTE in adapter.descriptor.capabilities
        ] if asset is not None else []
        history_providers = [
            adapter.descriptor.provider_id for adapter in registry.snapshot()
            if asset in adapter.descriptor.asset_classes and Capability.HISTORICAL in adapter.descriptor.capabilities
        ] if asset is not None else []
        plan = plan_research(instrument.asset_class, instrument.subtype) if instrument else None
        ambiguous = unique is None and len(matches) > 1
        passed = bool(matches) and (instrument is not None or ambiguous)
        rows.append({
            "query": query,
            "top_results": [
                {
                    "symbol": match.symbol, "name": match.name, "score": match.score,
                    "match_kind": match.match_kind, "asset_class": match.instrument.asset_class,
                    "subtype": match.instrument.subtype,
                }
                for match in matches
            ],
            "selected_canonical_instrument": instrument.identity if instrument else None,
            "selected_symbol": instrument.symbol if instrument else None,
            "instrument_type": instrument.asset_class if instrument else None,
            "subtype": instrument.subtype if instrument else None,
            "provider_mapping": dict(instrument.provider_symbols) if instrument else {},
            "quote_result": "ROUTE ELIGIBLE" if quote_providers else "PROVIDER NOT SUPPORTED",
            "quote_providers": quote_providers,
            "history_result": "ROUTE ELIGIBLE" if history_providers else "PROVIDER NOT SUPPORTED",
            "history_providers": history_providers,
            "research_route": plan.route.value if plan else "not_selected",
            "manual_refresh_required": False,
            "ambiguous_disambiguation_required": ambiguous,
            "result": "PASS" if passed else "FAIL",
            "defect_notes": "" if passed else "No safe canonical result or disambiguation candidates.",
        })
    return {
        "schema": "rangescout.r4-market-instrument-sweep.v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "query_count": len(rows),
        "pass_count": sum(row["result"] == "PASS" for row in rows),
        "fail_count": sum(row["result"] == "FAIL" for row in rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("query_count", "pass_count", "fail_count")}, sort_keys=True))
    return 0 if report["fail_count"] == 0 and report["query_count"] >= 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
