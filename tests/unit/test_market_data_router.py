from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from time import sleep

import pytest

from app.market_data.cache import ResultCache
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
    RateLimited,
    RateLimitState,
)
from app.market_data.health import CircuitState, HealthWindow, ProviderHealth
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter, NoEligibleProvider


NOW = datetime(2026, 8, 18, 16, tzinfo=timezone.utc)


def request(**changes) -> FabricRequest:
    base = FabricRequest(
        canonical_instrument_id="equity:NASDAQ:AAPL",
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        capability=Capability.QUOTE,
        venue="NASDAQ",
        freshness=FreshnessPolicy(timedelta(minutes=20), allow_delayed=True),
        request_id="request-1",
    )
    return replace(base, **changes)


class FakeAdapter:
    def __init__(self, provider_id: str, *, delay: float = 0, failure=None, mutate=None):
        self.delay = delay
        self.failure = failure
        self.mutate = mutate
        self.calls = 0
        self.descriptor = ProviderDescriptor(
            provider_id=provider_id,
            display_name=provider_id,
            asset_classes=frozenset({AssetClass.EQUITY}),
            capabilities=frozenset({Capability.QUOTE}),
            requires_credentials=False,
            credential_kind=CredentialKind.NONE,
            delay_class=DelayClass.DELAYED,
            terms=ProviderTerms("https://example.invalid", "2026-08-18", "test", decision="enabled"),
            enabled=True,
        )

    def request(self, req: FabricRequest) -> FabricResult:
        self.calls += 1
        sleep(self.delay)
        if self.failure:
            raise self.failure
        result = FabricResult(
            request_id=req.request_id,
            provider_id=self.descriptor.provider_id,
            provider_symbol=req.canonical_symbol,
            canonical_instrument_id=req.canonical_instrument_id,
            canonical_symbol=req.canonical_symbol,
            capability=req.capability,
            provider_timestamp=NOW - timedelta(minutes=1),
            received_at=NOW,
            delay_class=DelayClass.DELAYED,
            currency="USD",
            venue=req.venue,
            payload={"price": "100.00"},
            attribution=self.descriptor.display_name,
            cache_ttl_seconds=30,
        )
        return self.mutate(result) if self.mutate else result

    def health_check(self):
        return True

    def rate_limit_state(self):
        return RateLimitState()

    def list_instruments(self):
        return []


def registry(*adapters: FakeAdapter) -> FabricRegistry:
    value = FabricRegistry()
    for adapter in adapters:
        value.register(adapter)
    return value


def test_fastest_valid_response_wins() -> None:
    fast = FakeAdapter("fast", delay=0.001)
    slow = FakeAdapter("slow", delay=0.05)
    with MarketDataRouter(registry(fast, slow)) as router:
        assert router.fetch(request()).provider_id == "fast"


def test_forced_provider_uses_only_selected_provider_and_never_falls_back() -> None:
    selected = FakeAdapter("selected", failure=TimeoutError("unavailable"))
    fallback = FakeAdapter("fallback")
    with MarketDataRouter(registry(selected, fallback)) as router:
        with pytest.raises(NoEligibleProvider, match="did not fall back"):
            router.fetch(request(), forced_provider_id="selected")
    assert selected.calls == 1
    assert fallback.calls == 0


def test_forced_provider_does_not_reuse_cache_from_another_provider() -> None:
    first = FakeAdapter("first")
    forced = FakeAdapter("forced")
    with MarketDataRouter(registry(first, forced), max_fanout=1) as router:
        assert router.fetch(request()).provider_id == "first"
        result = router.fetch(replace(request(), request_id="forced-request"), forced_provider_id="forced")
    assert result.provider_id == "forced"
    assert forced.calls == 1


def test_sanitized_request_diagnostic_records_real_attempt_topology_without_error_text() -> None:
    failed = FakeAdapter("failed", failure=OSError("token=DO_NOT_RECORD https://secret.invalid"))
    winner = FakeAdapter("winner", delay=0.01)
    with MarketDataRouter(registry(failed, winner)) as router:
        result = router.fetch(request())
        diagnostic = router.diagnostics(result.request_id)
    assert diagnostic["winning_provider"] == "winner"
    assert diagnostic["capability"] == "quote"
    assert isinstance(diagnostic["fetch_thread_id"], int)
    assert {item["provider_id"] for item in diagnostic["attempts"]} == {"failed", "winner"}
    assert all(isinstance(item["thread_id"], int) for item in diagnostic["attempts"])
    serialized = str(diagnostic)
    assert "DO_NOT_RECORD" not in serialized and "secret.invalid" not in serialized


def test_visible_quote_budget_returns_without_waiting_for_stalled_provider() -> None:
    stalled = FakeAdapter("stalled", delay=0.5)
    router = MarketDataRouter(registry(stalled))
    began = __import__("time").monotonic()
    try:
        with pytest.raises(NoEligibleProvider, match="request budget exceeded"):
            router.fetch(request(), budget_seconds=0.05)
        assert __import__("time").monotonic() - began < 0.25
    finally:
        router.shutdown(wait=False)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: replace(value, payload={"price": "broken"}),
        lambda value: replace(value, provider_timestamp=NOW - timedelta(hours=2)),
        lambda value: replace(value, canonical_symbol="MSFT"),
        lambda value: replace(value, venue="NYSE"),
        lambda value: replace(value, request_id="old-request"),
    ],
    ids=["malformed", "stale", "wrong-symbol", "wrong-venue", "wrong-request"],
)
def test_fast_invalid_response_loses(mutate) -> None:
    invalid = FakeAdapter("invalid", delay=0.001, mutate=mutate)
    fallback = FakeAdapter("fallback", delay=0.02)
    with MarketDataRouter(registry(invalid, fallback)) as router:
        assert router.fetch(request()).provider_id == "fallback"


def test_timeout_and_rate_limit_fall_back_without_secret_text() -> None:
    timeout = FakeAdapter("timeout", failure=TimeoutError("secret=DO_NOT_LOG"))
    limited = FakeAdapter("limited", failure=RateLimited(60))
    fallback = FakeAdapter("fallback", delay=0.01)
    health = ProviderHealth(failure_threshold=1)
    with MarketDataRouter(registry(timeout, limited, fallback), health=health) as router:
        result = router.fetch(request())
        assert result.provider_id == "fallback"
        snapshot = str(health.snapshot())
        assert "DO_NOT_LOG" not in snapshot
        assert health.window("timeout", "equity", "quote").state == CircuitState.OPEN
        assert health.window("limited", "equity", "quote").rate_limited_until is not None


def test_circuit_breaker_opens_and_half_open_probe_recovers() -> None:
    window = HealthWindow(failure_threshold=2, cooldown_seconds=5)
    window.failure("timeout", NOW)
    window.failure("timeout", NOW)
    assert window.state == CircuitState.OPEN and not window.allow(NOW + timedelta(seconds=4))
    assert window.allow(NOW + timedelta(seconds=5)) and window.state == CircuitState.HALF_OPEN
    window.success(4.0, NOW + timedelta(seconds=5))
    assert window.state == CircuitState.CLOSED


def test_cache_hit_avoids_second_race_and_cache_is_bounded() -> None:
    adapter = FakeAdapter("only")
    cache = ResultCache(max_entries=1)
    with MarketDataRouter(registry(adapter), cache=cache) as router:
        first = router.fetch(request())
        second = router.fetch(replace(request(), request_id="request-2"))
        assert first.provider_id == second.provider_id
        assert "cache hit" in second.warnings
        assert adapter.calls == 1
    assert len(cache) == 1


def test_cache_expiration() -> None:
    cache = ResultCache(max_entries=2)
    req = request()
    result = replace(FakeAdapter("x").request(req), cache_ttl_seconds=1)
    cache.put(req, result, NOW)
    assert cache.get(req, NOW + timedelta(milliseconds=999)) is result
    assert cache.get(req, NOW + timedelta(seconds=1)) is None


def test_cross_check_surfaces_disagreement_and_never_averages() -> None:
    left = FakeAdapter("left", mutate=lambda value: replace(value, payload={"price": "100"}))
    right = FakeAdapter("right", delay=0.01, mutate=lambda value: replace(value, payload={"price": "120"}))
    with MarketDataRouter(registry(left, right)) as router:
        result = router.fetch(request(), cross_check=True)
    assert result.payload["price"] == "100"
    assert any("disagree" in warning for warning in result.warnings)


def test_incompatible_delay_classes_are_disclosed_not_averaged() -> None:
    delayed = FakeAdapter("delayed")
    realtime = FakeAdapter(
        "realtime",
        delay=0.01,
        mutate=lambda value: replace(value, delay_class=DelayClass.REALTIME, payload={"price": "101"}),
    )
    with MarketDataRouter(registry(delayed, realtime)) as router:
        result = router.fetch(request(), cross_check=True)
    assert any("not directly comparable" in warning for warning in result.warnings)


def test_provider_removed_during_request_loses() -> None:
    started = Event()
    removed = FakeAdapter("removed", delay=0.02)
    original = removed.request

    def blocked(req):
        started.set()
        return original(req)

    removed.request = blocked
    fallback = FakeAdapter("fallback", delay=0.05)
    providers = registry(removed, fallback)
    with MarketDataRouter(providers) as router:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(router.fetch, request())
            assert started.wait(1)
            providers.unregister("removed")
            assert future.result().provider_id == "fallback"


def test_shutdown_and_offline_state_are_truthful() -> None:
    router = MarketDataRouter(FabricRegistry())
    with pytest.raises(NoEligibleProvider, match="No authorized healthy provider"):
        router.fetch(request())
    router.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        router.fetch(request())
