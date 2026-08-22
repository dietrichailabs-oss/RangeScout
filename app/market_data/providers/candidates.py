"""Explicitly disabled consumer-site candidates; no network implementation exists."""

from __future__ import annotations

from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FabricRequest,
    FabricResult,
    ProviderDescriptor,
    ProviderTerms,
    RateLimitState,
)


class DisabledCandidateAdapter:
    def __init__(self, provider_id: str, display_name: str, documentation_url: str, reason: str) -> None:
        self.descriptor = ProviderDescriptor(
            provider_id=provider_id,
            display_name=display_name,
            asset_classes=frozenset({AssetClass.UNKNOWN}),
            capabilities=frozenset(),
            requires_credentials=False,
            credential_kind=CredentialKind.NONE,
            delay_class=DelayClass.REFERENCE,
            terms=ProviderTerms(
                documentation_url=documentation_url,
                reviewed_on="2026-08-18",
                automated_access="No approved machine-readable production endpoint.",
                decision="disabled",
                reason=reason,
            ),
            enabled=False,
            max_concurrency=0,
        )

    def normalize_symbol(self, symbol: str) -> str:
        return symbol.strip().upper()

    def provider_symbol_for(self, request: FabricRequest) -> str:
        raise RuntimeError("Provider candidate is disabled.")

    def request(self, request: FabricRequest) -> FabricResult:
        raise RuntimeError("Provider candidate is disabled.")

    def health_check(self) -> bool:
        return False

    def rate_limit_state(self) -> RateLimitState:
        return RateLimitState()

    def list_instruments(self) -> list[dict[str, object]]:
        return []


def disabled_consumer_candidates() -> tuple[DisabledCandidateAdapter, ...]:
    return (
        DisabledCandidateAdapter(
            "google_finance_candidate",
            "Google Finance (unsupported candidate)",
            "https://developers.google.com/gdata/docs/directory",
            "No current official general market-data developer API; consumer-page scraping is prohibited.",
        ),
        DisabledCandidateAdapter(
            "msn_money_candidate",
            "MSN Money (unsupported candidate)",
            "https://support.microsoft.com/en-us/msn/welcome-to-msn-money",
            "No current official general public market-data API; consumer-page scraping is prohibited.",
        ),
        DisabledCandidateAdapter(
            "binance_us_candidate",
            "Binance.US (review required)",
            "https://docs.binance.us/",
            "Exact current public endpoint authorization was not established; global Binance docs are insufficient.",
        ),
    )
