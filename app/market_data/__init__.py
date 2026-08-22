"""Capability-driven market-data fabric for RangeScout 1.3."""

from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FabricRequest,
    FabricResult,
    FreshnessPolicy,
    ProviderDescriptor,
)
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter

__all__ = [
    "AssetClass",
    "Capability",
    "CredentialKind",
    "DelayClass",
    "FabricRequest",
    "FabricResult",
    "FreshnessPolicy",
    "ProviderDescriptor",
    "FabricRegistry",
    "MarketDataRouter",
]
