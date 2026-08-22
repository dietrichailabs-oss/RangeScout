"""Provider registry supporting explicit switching between providers."""

from __future__ import annotations

from typing import Dict

from app.providers.base import MarketDataProvider
from app.providers.byo_provider import FinnhubProvider
from app.providers.live_provider import YahooFinanceProvider
from app.providers.public_policy import PUBLIC_PROVIDER_IDS
from app.security.credentials import CredentialStore


class ProviderRegistry:
    def __init__(self, allowed_provider_ids: tuple[str, ...] | None = None) -> None:
        self._providers: Dict[str, MarketDataProvider] = {}
        self._allowed_provider_ids = frozenset(allowed_provider_ids) if allowed_provider_ids else None

    def register(self, provider: MarketDataProvider) -> None:
        if self._allowed_provider_ids is not None and provider.provider_id not in self._allowed_provider_ids:
            raise ValueError(f"Provider '{provider.provider_id}' is not available in this public build.")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> MarketDataProvider:
        if provider_id not in self._providers:
            raise KeyError(f"Provider '{provider_id}' is not registered.")
        return self._providers[provider_id]

    def list_available(self) -> list[str]:
        return list(self._providers.keys())


def default_provider_registry(
    timeout_seconds: float = 12.0,
    credential_store: CredentialStore | None = None,
) -> ProviderRegistry:
    if credential_store is None:
        from app.security.credentials import default_credential_store

        credential_store = default_credential_store()
    registry = ProviderRegistry(PUBLIC_PROVIDER_IDS)
    registry.register(YahooFinanceProvider(timeout_seconds=timeout_seconds))
    registry.register(FinnhubProvider(credential_store.load, timeout_seconds=timeout_seconds))
    return registry
