from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import perf_counter, sleep

import pytest

from app.market_data.contracts import (
    AssetClass, Capability, CredentialKind, DelayClass, FabricRequest, FabricResult,
    ProviderDescriptor, ProviderTerms, RateLimitState,
)
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter
from app.market_data.execution import RequestCancelled


class SaturatedProvider:
    def __init__(self, provider_id: str = "priority_test", *, max_concurrency: int = 2) -> None:
        self.descriptor = ProviderDescriptor(
            provider_id, provider_id, frozenset({AssetClass.EQUITY}),
            frozenset({Capability.QUOTE, Capability.HISTORICAL}), False,
            CredentialKind.NONE, DelayClass.DELAYED,
            ProviderTerms("https://example.invalid", "2026-08-21", "deterministic test", decision="enabled"),
            enabled=True, max_concurrency=max_concurrency, minimum_request_interval_seconds=0.0,
        )
        self.release = Event()
        self.quote_started = Event()
        self.history_started = 0
        self.quote_calls = 0
        self.lock = Lock()

    def provider_symbol_for(self, request):
        return request.canonical_symbol

    def health_check(self):
        return True

    def rate_limit_state(self):
        return RateLimitState()

    def list_instruments(self):
        return []

    def request(self, request):
        if request.capability == Capability.HISTORICAL:
            with self.lock:
                self.history_started += 1
            self.release.wait(10)
            payload = {"bars": []}
        else:
            with self.lock:
                self.quote_calls += 1
            self.quote_started.set()
            payload = {"price": "100"}
        now = datetime.now(timezone.utc)
        return FabricResult(
            request.request_id, self.descriptor.provider_id, request.canonical_symbol,
            request.canonical_instrument_id, request.canonical_symbol, request.capability,
            now, now, DelayClass.DELAYED, "USD", None, payload, "test", 0,
        )


def _request(symbol: str, capability: Capability) -> FabricRequest:
    return FabricRequest(f"equity:{symbol}", symbol, AssetClass.EQUITY, capability)


def test_provider_executor_reserves_quote_capacity_under_eight_blocked_history_jobs() -> None:
    adapter = SaturatedProvider()
    registry = FabricRegistry(); registry.register(adapter)
    router = MarketDataRouter(registry, max_workers=8, max_fanout=1)
    threads = [
        Thread(target=lambda i=i: _ignore_failure(router, _request(f"H{i}", Capability.HISTORICAL), 8.0), daemon=True)
        for i in range(8)
    ]
    try:
        for thread in threads:
            thread.start()
        deadline = perf_counter() + 1
        while adapter.history_started < 1 and perf_counter() < deadline:
            sleep(0.005)
        began = perf_counter()
        result = router.fetch(_request("QA_NEW", Capability.QUOTE), budget_seconds=3.0)
        elapsed = perf_counter() - began
        assert result.provider_id == "priority_test"
        assert adapter.quote_started.is_set()
        assert elapsed < 0.5
        diagnostic = router.diagnostics(result.request_id)
        assert diagnostic["outcome"] == "fresh"
        assert diagnostic["attempts"][0]["executor_queue_wait_ms"] < 500
        assert diagnostic["attempts"][0]["gate_wait_ms"] < 500
    finally:
        adapter.release.set()
        for thread in threads:
            thread.join(1)
        router.shutdown(wait=False)


def test_smart_and_forced_provider_modes_preserve_routing() -> None:
    first, second = SaturatedProvider("first"), SaturatedProvider("second")
    registry = FabricRegistry(); registry.register(first); registry.register(second)
    with MarketDataRouter(registry, max_fanout=2) as router:
        smart = router.fetch(_request("SMART", Capability.QUOTE), budget_seconds=1.0)
        forced = router.fetch(
            _request("FORCED", Capability.QUOTE), budget_seconds=1.0, forced_provider_id="second"
        )
    assert smart.provider_id in {"first", "second"}
    assert forced.provider_id == "second"


def test_router_observes_superseded_quote_cancellation_promptly() -> None:
    adapter = SaturatedProvider()
    adapter.release.clear()
    original = adapter.request

    def blocking_quote(request):
        if request.capability == Capability.QUOTE:
            adapter.quote_started.set()
            adapter.release.wait(10)
        return original(request)

    adapter.request = blocking_quote
    registry = FabricRegistry(); registry.register(adapter)
    router = MarketDataRouter(registry, max_fanout=1)
    cancelled = Event()
    observed: list[object] = []

    def fetch() -> None:
        try:
            router.fetch(
                _request("STALE", Capability.QUOTE),
                budget_seconds=3.0,
                cancellation_event=cancelled,
            )
        except Exception as exc:
            observed.append(exc)

    thread = Thread(target=fetch, daemon=True)
    try:
        thread.start()
        assert adapter.quote_started.wait(0.5)
        began = perf_counter(); cancelled.set(); thread.join(0.5)
        assert not thread.is_alive()
        assert perf_counter() - began < 0.5
        assert observed and isinstance(observed[0], RequestCancelled)
        assert router.diagnostics()["outcome"] == "cancelled"
    finally:
        adapter.release.set()
        thread.join(1)
        router.shutdown(wait=False)


def _ignore_failure(router: MarketDataRouter, request: FabricRequest, budget: float) -> None:
    try:
        router.fetch(request, budget_seconds=budget)
    except Exception:
        pass
