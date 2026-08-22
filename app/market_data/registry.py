"""Terms-aware registry for provider-fabric adapters."""

from __future__ import annotations

from threading import RLock

from app.market_data.contracts import AssetClass, Capability, MarketDataAdapter


class FabricRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, MarketDataAdapter] = {}
        self._lock = RLock()

    def register(self, adapter: MarketDataAdapter) -> None:
        descriptor = adapter.descriptor
        if descriptor.terms.decision not in {"enabled", "byo_enabled"} and descriptor.enabled:
            raise ValueError(f"Provider '{descriptor.provider_id}' lacks an enabled terms decision.")
        with self._lock:
            self._adapters[descriptor.provider_id] = adapter

    def unregister(self, provider_id: str) -> None:
        with self._lock:
            self._adapters.pop(provider_id, None)

    def get(self, provider_id: str) -> MarketDataAdapter:
        with self._lock:
            if provider_id not in self._adapters:
                raise KeyError(f"Provider '{provider_id}' is not registered.")
            return self._adapters[provider_id]

    def eligible(self, asset_class: AssetClass, capability: Capability) -> list[MarketDataAdapter]:
        with self._lock:
            return [
                adapter
                for adapter in self._adapters.values()
                if adapter.descriptor.enabled
                and asset_class in adapter.descriptor.asset_classes
                and capability in adapter.descriptor.capabilities
            ]

    def snapshot(self) -> tuple[MarketDataAdapter, ...]:
        with self._lock:
            return tuple(self._adapters.values())
