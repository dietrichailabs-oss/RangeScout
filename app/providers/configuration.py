"""Provider configuration/status service independent of Qt widgets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.providers.registry import ProviderRegistry
from app.providers.public_policy import PUBLIC_CREDENTIAL_PROVIDER_IDS
from app.security.credentials import CredentialStorageError, CredentialStore, ProviderCredentials


@dataclass(frozen=True)
class ProviderConfigurationStatus:
    provider_id: str
    display_name: str
    requires_credentials: bool
    configured: bool
    configuration_text: str
    connection_text: str


class ProviderConfigurationService:
    CREDENTIAL_PROVIDERS = PUBLIC_CREDENTIAL_PROVIDER_IDS | frozenset(
        {"twelve_data", "alpha_vantage", "fred", "congress", "logo_dev"}
    )

    def __init__(self, registry: ProviderRegistry, credential_store: CredentialStore) -> None:
        self.registry = registry
        self.credential_store = credential_store
        self._subscribers: list[Callable[[str, bool], None]] = []

    def subscribe(self, callback: Callable[[str, bool], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback) if callback in self._subscribers else None

    def _notify(self, provider_id: str, configured: bool) -> None:
        for callback in tuple(self._subscribers):
            try:
                callback(provider_id, configured)
            except Exception:
                # The secure write/delete has already succeeded. One optional
                # view refresh must never roll the credential operation back
                # or prevent the remaining subscribers from synchronizing.
                continue

    def status(self, provider_id: str) -> ProviderConfigurationStatus:
        provider = self.registry.get(provider_id)
        requires_credentials = provider_id in self.CREDENTIAL_PROVIDERS
        storage_available = True
        try:
            configured = not requires_credentials or self.credential_store.load(provider_id) is not None
        except CredentialStorageError:
            configured = False
            storage_available = False
        if requires_credentials:
            if not storage_available:
                configuration_text = "Secure credential storage unavailable"
                connection_text = "Not connected"
            else:
                configuration_text = "Configured securely" if configured else "Credentials required"
                connection_text = "Not connected; connection occurs only when the provider is used"
        else:
            configuration_text = "No credentials required"
            connection_text = "Network provider available; connection is on demand"
        return ProviderConfigurationStatus(
            provider_id=provider_id,
            display_name=provider.provider_name,
            requires_credentials=requires_credentials,
            configured=configured,
            configuration_text=configuration_text,
            connection_text=connection_text,
        )

    def list_statuses(self) -> list[ProviderConfigurationStatus]:
        return [self.status(provider_id) for provider_id in self.registry.list_available()]

    def save_credentials(self, provider_id: str, values: dict[str, str]) -> None:
        if provider_id not in self.CREDENTIAL_PROVIDERS:
            raise ValueError(f"Provider '{provider_id}' does not accept public credentials.")
        self.credential_store.save(ProviderCredentials(provider_id=provider_id, values=values))
        self._notify(provider_id, True)

    def delete_credentials(self, provider_id: str) -> bool:
        if provider_id not in self.CREDENTIAL_PROVIDERS:
            return False
        deleted = self.credential_store.delete(provider_id)
        self._notify(provider_id, False)
        return deleted
