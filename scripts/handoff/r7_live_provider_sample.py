"""Recorded-seed R7 30+ stratified live/provider validation sample."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.company_data.instrument_intelligence import (
    InstrumentReferenceSeeder,
    InstrumentResolver,
    canonical_asset_class,
)
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import AssetClass, Capability, FabricRequest
from app.market_data.providers.catalog import default_fabric_registry
from app.market_data.providers.crypto_public import CoinbaseExchangeAdapter
from app.providers.live_provider import YahooFinanceProvider
from app.research.routing import plan_research
from app.security.credentials import InMemoryCredentialStore
from scripts.handoff.r7_exhaustive_instrument_audit import _provider_plan


SEED = "RangeScout-R7-live-provider-20260823"
EXCLUDED_R6 = {
    "FTHF", "PENN", "QBUF", "MPB", "SIO", "SEZL", "FLC", "NGHT", "CINF", "FWONK",
    "NNRGF", "BTC/USD", "BBMC", "PMAR", "MOD", "SBFMW", "TROW", "FIW", "BA", "NAT",
}
TARGETS = {
    "preferred": 4,
    "adr": 3,
    "unit": 3,
    "warrant": 3,
    "right": 3,
    "closed_end_fund": 2,
    "etf": 2,
    "equity": 3,
    "otc": 2,
    "crypto_spot": 1,
    "fx": 1,
    "index": 3,
    "commodity_spot": 1,
}


def _database() -> Path:
    path = Path(tempfile.mkdtemp(prefix="rangescout-r7-live-sample-")) / "history.sqlite"
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    return path


def _bucket(row: sqlite3.Row) -> str:
    asset = canonical_asset_class(
        row["asset_class"], row["instrument_subtype"], row["security_type"], row["security_name"]
    )
    if str(row["primary_venue"] or "").upper() == "OTC" and asset == "equity":
        return "otc"
    return asset


def _rank(bucket: str, symbol: str) -> str:
    return hashlib.sha256(f"{SEED}|{bucket}|{symbol}".encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    database = _database()
    resolver = InstrumentResolver(database)
    registry = default_fabric_registry(InMemoryCredentialStore())
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT instrument_id,canonical_symbol,security_name,asset_class,security_type,
                      COALESCE(instrument_subtype,'') instrument_subtype,primary_venue
               FROM rs_instruments WHERE is_active=1 ORDER BY canonical_symbol,instrument_id"""
        ).fetchall()
    buckets: dict[str, list[sqlite3.Row]] = {key: [] for key in TARGETS}
    for row in rows:
        symbol = str(row["canonical_symbol"])
        bucket = _bucket(row)
        reference_adversarial = bucket in {"crypto_spot", "fx", "index", "commodity_spot"}
        if (symbol not in EXCLUDED_R6 or reference_adversarial) and bucket in buckets:
            buckets[bucket].append(row)
    selected: list[tuple[str, sqlite3.Row]] = []
    for bucket, target in TARGETS.items():
        ranked = sorted(buckets[bucket], key=lambda row: _rank(bucket, str(row["canonical_symbol"])))
        if len(ranked) < target:
            raise RuntimeError(f"insufficient {bucket} population: {len(ranked)} < {target}")
        selected.extend((bucket, row) for row in ranked[:target])
    yahoo = YahooFinanceProvider(timeout_seconds=5.0)
    coinbase = CoinbaseExchangeAdapter()
    results: list[dict[str, object]] = []
    for index, (bucket, row) in enumerate(selected, 1):
        instrument = resolver.by_id(int(row["instrument_id"]))
        if instrument is None:
            raise RuntimeError(f"missing selected instrument {row['canonical_symbol']}")
        asset = AssetClass(instrument.asset_class)
        quote_plan = _provider_plan(registry, instrument, asset, Capability.QUOTE)
        history_plan = _provider_plan(registry, instrument, asset, Capability.HISTORICAL)
        yahoo_route = next((
            route for route in quote_plan["routes"]
            if route["provider_id"] == "yahoo" and route["status"] == "supported"
        ), None)
        live: dict[str, object]
        began = monotonic()
        if yahoo_route is not None:
            try:
                response = yahoo.fetch_quote(str(yahoo_route["provider_symbol"]))
                quote = response.payload
                live = {
                    "status": "PASS",
                    "provider_id": "yahoo",
                    "provider_symbol": yahoo_route["provider_symbol"],
                    "returned_symbol": quote.instrument.identifier.symbol,
                    "price": str(quote.last),
                    "provider_timestamp_utc": (
                        quote.provider_timestamp.isoformat() if quote.provider_timestamp else None
                    ),
                }
            except Exception as exc:
                live = {
                    "status": "PROVIDER_UNAVAILABLE",
                    "provider_id": "yahoo",
                    "provider_symbol": yahoo_route["provider_symbol"],
                    "error_type": type(exc).__name__,
                }
            sleep(0.55)
        elif asset is AssetClass.CRYPTO_SPOT:
            request = FabricRequest(
                canonical_instrument_id=instrument.identity,
                canonical_symbol=instrument.symbol,
                asset_class=asset,
                capability=Capability.QUOTE,
                provider_symbol_overrides=tuple(sorted(instrument.provider_symbols.items())),
            )
            try:
                override = instrument.provider_symbols.get("coinbase")
                response = coinbase.request(
                    FabricRequest(
                        canonical_instrument_id=request.canonical_instrument_id,
                        canonical_symbol=override or request.canonical_symbol,
                        asset_class=request.asset_class,
                        capability=request.capability,
                    )
                )
                live = {
                    "status": "PASS",
                    "provider_id": "coinbase",
                    "provider_symbol": response.provider_symbol,
                    "price": str(response.payload.get("price")),
                    "provider_timestamp_utc": response.provider_timestamp.isoformat(),
                }
            except Exception as exc:
                live = {"status": "PROVIDER_UNAVAILABLE", "provider_id": "coinbase", "error_type": type(exc).__name__}
        else:
            live = {
                "status": "NOT_CONFIGURED",
                "provider_id": "twelve_data",
                "reason": "BYO free-tier credential was intentionally unavailable; no credential or quota was used.",
            }
        live["elapsed_ms"] = round((monotonic() - began) * 1000.0, 3)
        results.append({
            "index": index,
            "selection_bucket": bucket,
            "instrument_id": instrument.instrument_id,
            "canonical_symbol": instrument.symbol,
            "security_name": instrument.name,
            "asset_class": instrument.asset_class,
            "subtype": instrument.subtype,
            "venue": instrument.venue,
            "provider_symbols": dict(sorted(instrument.provider_symbols.items())),
            "quote_plan": quote_plan,
            "history_plan": history_plan,
            "research_route": plan_research(instrument.asset_class, instrument.subtype).route.value,
            "live_quote": live,
        })
        print(f"{index}/{len(selected)} {instrument.symbol} {live['status']}", flush=True)
    class_counts = {bucket: sum(item["selection_bucket"] == bucket for item in results) for bucket in TARGETS}
    payload = {
        "schema": "rangescout.r7-live-provider-sample.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection_seed": SEED,
        "population": len(rows),
        "exclusions": {"r6_random20": sorted(EXCLUDED_R6)},
        "target_counts": TARGETS,
        "class_counts": class_counts,
        "sample_size": len(results),
        "direct_live_attempts": sum(item["live_quote"]["status"] != "NOT_CONFIGURED" for item in results),
        "live_passes": sum(item["live_quote"]["status"] == "PASS" for item in results),
        "provider_unavailable": sum(item["live_quote"]["status"] == "PROVIDER_UNAVAILABLE" for item in results),
        "not_configured": sum(item["live_quote"]["status"] == "NOT_CONFIGURED" for item in results),
        "credentials_in_evidence": False,
        "request_urls_in_evidence": False,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in (
        "sample_size", "direct_live_attempts", "live_passes", "provider_unavailable", "not_configured"
    )}, indent=2))
    local_routes_ok = all(
        item["quote_plan"]["status"] in {"supported", "unsupported"}
        and item["history_plan"]["status"] in {"supported", "unsupported"}
        for item in results
    )
    return 0 if len(results) >= 30 and class_counts == TARGETS and local_routes_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
