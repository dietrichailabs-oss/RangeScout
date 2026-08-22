from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock

from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FabricRequest,
    FabricResult,
    FreshnessPolicy,
    ProviderDescriptor,
    ProviderTerms,
    RateLimitState,
)
from app.market_data.health import ProviderHealth
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter


class BurstAdapter:
    descriptor = ProviderDescriptor(
        "burst", "Burst fake", frozenset({AssetClass.EQUITY}), frozenset({Capability.QUOTE}),
        False, CredentialKind.NONE, DelayClass.REALTIME,
        ProviderTerms("https://example.invalid", "2026-08-18", "deterministic test only", decision="enabled"),
        enabled=True, max_concurrency=8,
    )

    def __init__(self) -> None:
        self.calls = 0
        self.lock = Lock()

    def request(self, request: FabricRequest) -> FabricResult:
        with self.lock:
            self.calls += 1
        now = datetime.now(timezone.utc)
        return FabricResult(
            request.request_id, self.descriptor.provider_id, request.canonical_symbol,
            request.canonical_instrument_id, request.canonical_symbol, request.capability,
            now, now, DelayClass.REALTIME, "USD", request.venue,
            {"price": "100"}, "deterministic fake", 1,
        )

    def normalize_symbol(self, symbol):
        return symbol.upper()

    def provider_symbol_for(self, request):
        return request.canonical_symbol

    def health_check(self):
        return True

    def rate_limit_state(self):
        return RateLimitState()

    def list_instruments(self):
        return []


def test_120_independent_symbol_requests_are_bounded_and_complete() -> None:
    adapter = BurstAdapter()
    registry = FabricRegistry()
    registry.register(adapter)
    health = ProviderHealth(max_samples=200)
    router = MarketDataRouter(registry, max_workers=8, max_fanout=2, health=health)

    def fetch(index: int):
        symbol = f"S{index:03d}"
        return router.fetch(
            FabricRequest(
                f"equity:test:{symbol}", symbol, AssetClass.EQUITY, Capability.QUOTE,
                venue="TEST", freshness=FreshnessPolicy(timedelta(minutes=1), allow_delayed=False),
            )
        )

    try:
        with ThreadPoolExecutor(max_workers=24) as executor:
            results = list(executor.map(fetch, range(120)))
        assert len(results) == 120
        assert {value.canonical_symbol for value in results} == {f"S{index:03d}" for index in range(120)}
        assert adapter.calls == 120
        metrics = health.window("burst", "equity", "quote").metrics()
        assert metrics["request_count"] == 120 and metrics["success_rate"] == 1.0
        assert len(router.cache) <= 120
    finally:
        router.shutdown()
