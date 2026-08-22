"""Fabric adapters for the approved Yahoo and Finnhub production providers."""

from __future__ import annotations

from datetime import datetime, time, timezone

from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FabricProviderError,
    FabricRequest,
    FabricResult,
    ProviderDescriptor,
    ProviderTerms,
    RateLimitState,
)
from app.models.schemas import DataDelay, OhlcvBar
from app.providers.base import MarketDataProvider


def _delay(value: DataDelay) -> DelayClass:
    return {
        DataDelay.REALTIME: DelayClass.REALTIME,
        DataDelay.DELAYED: DelayClass.DELAYED,
        DataDelay.END_OF_DAY: DelayClass.END_OF_DAY,
    }.get(value, DelayClass.DELAYED)


class LegacyProviderFabricAdapter:
    """Narrow compatibility bridge; all execution still goes through the fabric."""

    def __init__(self, provider: MarketDataProvider, descriptor: ProviderDescriptor) -> None:
        if descriptor.provider_id != provider.provider_id:
            raise ValueError("Bridge descriptor/provider identity mismatch.")
        self.provider = provider
        self.descriptor = descriptor

    def normalize_symbol(self, symbol: str) -> str:
        normalizer = getattr(self.provider, "normalize_symbol", None)
        return normalizer(symbol) if callable(normalizer) else symbol.strip().upper()

    def provider_symbol_for(self, request: FabricRequest) -> str:
        return self.normalize_symbol(request.canonical_symbol)

    def request(self, request: FabricRequest) -> FabricResult:
        provider_symbol = self.provider_symbol_for(request)
        if request.capability == Capability.QUOTE:
            legacy = self.provider.fetch_quote(provider_symbol)
            quote = legacy.payload
            provider_timestamp = quote.provider_timestamp or legacy.timestamp
            payload = quote
            ttl = 15
        elif request.capability in {Capability.HISTORICAL, Capability.CANDLES}:
            instrument = self.provider.resolve_instrument(provider_symbol)
            legacy = self.provider.fetch_historical(
                instrument.identifier,
                start=request.start,
                end=request.end,
            )
            bars, _actions = legacy.payload
            if not bars:
                raise FabricProviderError(f"{self.provider.provider_name} returned no historical bars.")
            provider_timestamp = _newest_bar_timestamp(bars)
            payload = legacy.payload
            ttl = 300
        else:
            raise FabricProviderError(
                f"{self.provider.provider_name} does not support {request.capability.value} through the fabric."
            )
        received = datetime.now(timezone.utc)
        return FabricResult(
            request_id=request.request_id,
            provider_id=self.descriptor.provider_id,
            provider_symbol=provider_symbol,
            canonical_instrument_id=request.canonical_instrument_id,
            canonical_symbol=request.canonical_symbol,
            capability=request.capability,
            provider_timestamp=provider_timestamp,
            received_at=received,
            delay_class=_delay(legacy.metadata.delay_label),
            currency=getattr(payload, "currency", "USD"),
            venue=request.venue,
            payload=payload,
            attribution=self.descriptor.terms.attribution,
            cache_ttl_seconds=ttl,
            warnings=tuple(legacy.warnings),
        )

    def health_check(self) -> bool:
        if self.descriptor.requires_credentials:
            loader = getattr(self.provider, "_credential_loader", None)
            if callable(loader):
                try:
                    return loader(self.descriptor.provider_id) is not None
                except Exception:
                    return False
        return True

    def rate_limit_state(self) -> RateLimitState:
        return RateLimitState()

    def list_instruments(self) -> list[dict[str, object]]:
        return []


def _newest_bar_timestamp(bars: list[OhlcvBar]) -> datetime:
    timestamps = [bar.provider_timestamp for bar in bars if bar.provider_timestamp is not None]
    if timestamps:
        return max(value if value.tzinfo else value.replace(tzinfo=timezone.utc) for value in timestamps)
    newest = max(bar.date for bar in bars)
    return datetime.combine(newest, time.min, timezone.utc)


def yahoo_descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="yahoo",
        display_name="Yahoo Finance",
        asset_classes=frozenset(
            {
                AssetClass.EQUITY,
                AssetClass.ETF,
                AssetClass.ETN,
                AssetClass.ADR,
                AssetClass.PREFERRED,
                AssetClass.WARRANT,
                AssetClass.RIGHT,
                AssetClass.UNIT,
                AssetClass.INDEX,
                AssetClass.OTC,
            }
        ),
        capabilities=frozenset({Capability.QUOTE, Capability.HISTORICAL, Capability.CANDLES}),
        requires_credentials=False,
        credential_kind=CredentialKind.NONE,
        delay_class=DelayClass.DELAYED,
        terms=ProviderTerms(
            documentation_url="https://finance.yahoo.com/",
            reviewed_on="2026-08-19",
            automated_access="Existing approved RangeScout Yahoo chart transport.",
            attribution="Yahoo Finance",
            caching="Short-lived normalized application cache.",
            redistribution="User-directed in-application display only.",
            decision="enabled",
            reason="Preserved released RangeScout production provider through a fabric bridge.",
        ),
        enabled=True,
        max_concurrency=2,
        minimum_request_interval_seconds=0.5,
    )


def finnhub_descriptor() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id="finnhub",
        display_name="Finnhub",
        asset_classes=frozenset({AssetClass.EQUITY, AssetClass.ETF, AssetClass.ADR}),
        capabilities=frozenset({Capability.QUOTE}),
        requires_credentials=True,
        credential_kind=CredentialKind.API_KEY,
        delay_class=DelayClass.REALTIME,
        terms=ProviderTerms(
            documentation_url="https://finnhub.io/docs/api/quote",
            reviewed_on="2026-08-19",
            automated_access="Official BYO-key quote API.",
            attribution="Finnhub",
            caching="Short-lived quote cache.",
            redistribution="User plan terms apply.",
            decision="byo_enabled",
            reason="Preserved released BYO-key provider through a fabric bridge.",
        ),
        enabled=True,
        max_concurrency=1,
        minimum_request_interval_seconds=1.0,
    )
