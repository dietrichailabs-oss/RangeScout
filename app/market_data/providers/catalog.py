"""Production provider-fabric catalog. It does not alter the frozen legacy selector."""

from __future__ import annotations

from app.market_data.providers.candidates import disabled_consumer_candidates
from app.market_data.providers.crypto_public import CoinbaseExchangeAdapter, CoinPaprikaAdapter, KrakenAdapter
from app.market_data.providers.byo_free_tier import AlphaVantageAdapter, FredAdapter, TwelveDataAdapter
from app.market_data.providers.legacy_bridge import (
    LegacyProviderFabricAdapter,
    finnhub_descriptor,
    yahoo_descriptor,
)
from app.market_data.registry import FabricRegistry
from app.providers.registry import ProviderRegistry, default_provider_registry
from app.security.credentials import CredentialStore


def default_fabric_registry(
    credential_store: CredentialStore | None = None,
    legacy_registry: ProviderRegistry | None = None,
) -> FabricRegistry:
    registry = FabricRegistry()
    legacy = legacy_registry or default_provider_registry(credential_store=credential_store)
    registry.register(LegacyProviderFabricAdapter(legacy.get("yahoo"), yahoo_descriptor()))
    registry.register(LegacyProviderFabricAdapter(legacy.get("finnhub"), finnhub_descriptor()))
    registry.register(CoinbaseExchangeAdapter())
    registry.register(KrakenAdapter())
    registry.register(CoinPaprikaAdapter())
    if credential_store is not None:
        registry.register(TwelveDataAdapter(credential_store))
        registry.register(AlphaVantageAdapter(credential_store))
        registry.register(FredAdapter(credential_store))
    for candidate in disabled_consumer_candidates():
        registry.register(candidate)
    return registry
