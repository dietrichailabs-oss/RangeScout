from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FreshnessPolicy,
    ProviderDescriptor,
    ProviderTerms,
)
from app.market_data.providers.candidates import disabled_consumer_candidates
from app.market_data.registry import FabricRegistry


def descriptor(*, provider_id: str = "test", enabled: bool = True, decision: str = "enabled"):
    return ProviderDescriptor(
        provider_id=provider_id,
        display_name="Test",
        asset_classes=frozenset({AssetClass.EQUITY}),
        capabilities=frozenset({Capability.QUOTE}),
        requires_credentials=False,
        credential_kind=CredentialKind.NONE,
        delay_class=DelayClass.DELAYED,
        terms=ProviderTerms("https://example.invalid/docs", "2026-08-18", "official test", decision=decision),
        enabled=enabled,
    )


class Adapter:
    def __init__(self, value):
        self.descriptor = value


def test_registry_rejects_enabled_adapter_without_terms_decision() -> None:
    registry = FabricRegistry()
    with pytest.raises(ValueError, match="terms decision"):
        registry.register(Adapter(descriptor(decision="disabled")))


def test_registry_filters_by_asset_and_capability() -> None:
    registry = FabricRegistry()
    registry.register(Adapter(descriptor()))
    assert len(registry.eligible(AssetClass.EQUITY, Capability.QUOTE)) == 1
    assert registry.eligible(AssetClass.CRYPTO_SPOT, Capability.QUOTE) == []


def test_disabled_consumer_candidates_have_no_capabilities_or_execution() -> None:
    candidates = disabled_consumer_candidates()
    assert {item.descriptor.provider_id for item in candidates} == {
        "google_finance_candidate",
        "msn_money_candidate",
        "binance_us_candidate",
    }
    assert all(not item.descriptor.enabled and not item.descriptor.capabilities for item in candidates)
    assert all(item.list_instruments() == [] and not item.health_check() for item in candidates)


def test_freshness_policy_rejects_stale_future_and_disallowed_delay() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    policy = FreshnessPolicy(timedelta(minutes=5), allow_delayed=False)
    assert policy.accepts(now - timedelta(minutes=1), DelayClass.REALTIME, now)
    assert not policy.accepts(now - timedelta(minutes=6), DelayClass.REALTIME, now)
    assert not policy.accepts(now + timedelta(minutes=6), DelayClass.REALTIME, now)
    assert not policy.accepts(now - timedelta(minutes=1), DelayClass.DELAYED, now)
