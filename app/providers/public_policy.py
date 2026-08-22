"""Legacy equity-selector policy retained for RangeScout 1.3.0."""

from __future__ import annotations


PUBLIC_PROVIDER_IDS = ("yahoo", "finnhub")
PUBLIC_PROVIDER_SET = frozenset(PUBLIC_PROVIDER_IDS)
PUBLIC_CREDENTIAL_PROVIDER_IDS = frozenset({"finnhub"})


def normalize_public_provider(provider_id: object, fallback: str = "yahoo") -> str:
    normalized = str(provider_id).strip().lower()
    return normalized if normalized in PUBLIC_PROVIDER_SET else fallback


def require_public_provider(provider_id: object) -> str:
    normalized = str(provider_id).strip().lower()
    if normalized not in PUBLIC_PROVIDER_SET:
        raise ValueError(f"Provider '{normalized}' is not available in this public build.")
    return normalized
