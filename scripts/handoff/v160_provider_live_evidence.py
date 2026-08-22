#!/usr/bin/env python
"""Real-network provider-mode smoke for Engineering evidence (never used by tests)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application.bootstrap import RangeScoutApplication


def quote_record(app: RangeScoutApplication, symbol: str, mode: str) -> dict[str, object]:
    app.set_provider_mode(mode)
    began = monotonic()
    result = app.market_data_service.fetch_quote(symbol)
    elapsed_ms = round((monotonic() - began) * 1000.0, 3)
    quote = result.payload
    request_id = str(result.metadata.capabilities.get("fabric_request_id"))
    diagnostic = app.market_data_router.diagnostics(request_id)
    return {
        "symbol": symbol,
        "requested_mode": mode,
        "provider_id": result.metadata.provider_id,
        "provider_name": result.metadata.provider_name,
        "accepted_price": str(quote.last),
        "latency_ms": elapsed_ms,
        "provider_timestamp": quote.provider_timestamp.isoformat() if quote.provider_timestamp else None,
        "routing_mode": diagnostic.get("routing_mode"),
        "winner": diagnostic.get("winning_provider"),
        "cache": diagnostic.get("cache"),
        "attempts": diagnostic.get("attempts", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    app = RangeScoutApplication(data_dir=Path(args.profile))
    try:
        smart = quote_record(app, "AAPL", "smart")
        forced = quote_record(app, "BA", "yahoo")
        payload = {
            "schema": "rangescout.provider-ux.live-evidence.v1",
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "smart_search": smart,
            "forced_provider": forced,
            "pass": (
                smart["provider_id"] == "yahoo"
                and smart["routing_mode"] == "smart"
                and forced["provider_id"] == "yahoo"
                and forced["routing_mode"] == "yahoo"
            ),
        }
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not payload["pass"]:
            raise SystemExit(1)
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
