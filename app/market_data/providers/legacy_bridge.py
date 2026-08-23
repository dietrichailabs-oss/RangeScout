"""Fabric adapters for the approved Yahoo and Finnhub production providers."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlparse

from app.catalysts.entities import CatalystEvent, Relevance

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
        elif request.capability == Capability.NEWS and self.descriptor.provider_id == "finnhub":
            fetch_news = getattr(self.provider, "fetch_company_news", None)
            if not callable(fetch_news):
                raise FabricProviderError("Finnhub news capability is unavailable.")
            end = request.end or datetime.now(timezone.utc)
            start = request.start or end - timedelta(days=7)
            rows = fetch_news(provider_symbol, start, end)
            payload = _normalize_finnhub_news(rows, request.canonical_symbol, received_at=datetime.now(timezone.utc))
            provider_timestamp = max((event.published_at for event in payload), default=datetime.now(timezone.utc))
            ttl = 900
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
                AssetClass.CLOSED_END_FUND,
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
        capabilities=frozenset({Capability.QUOTE, Capability.NEWS}),
        requires_credentials=True,
        credential_kind=CredentialKind.API_KEY,
        delay_class=DelayClass.REALTIME,
        terms=ProviderTerms(
            documentation_url="https://finnhub.io/docs/api/company-news",
            reviewed_on="2026-08-19",
            automated_access="Official BYO-key quote and company-news APIs.",
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


def _normalize_finnhub_news(
    rows: list[dict[str, object]], symbol: str, *, received_at: datetime
) -> list[CatalystEvent]:
    """Normalize provider metadata, reject unsafe article URLs, and deduplicate."""
    normalized_symbol = symbol.strip().upper()
    events: list[CatalystEvent] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        headline = str(row.get("headline") or "").strip()
        url = str(row.get("url") or "").strip()
        parsed = urlparse(url)
        if not headline or parsed.scheme.lower() != "https" or not parsed.hostname:
            continue
        key = (headline.casefold(), url)
        if key in seen:
            continue
        seen.add(key)
        try:
            published = datetime.fromtimestamp(int(row.get("datetime") or 0), timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        if published.year < 2000 or published > received_at + timedelta(minutes=5):
            continue
        source = str(row.get("source") or "Finnhub").strip() or "Finnhub"
        identifier = str(row.get("id") or f"{normalized_symbol}:{int(published.timestamp())}:{len(events)}")
        events.append(
            CatalystEvent(
                event_id=f"finnhub-news:{identifier}", source=source, source_url=url,
                published_at=published, received_at=received_at, title=headline,
                symbols=(normalized_symbol,), category="news", relevance=Relevance.MEDIUM,
                retention="metadata_only", metadata={"provider": "finnhub"},
            )
        )
    return sorted(events, key=lambda event: event.published_at, reverse=True)
