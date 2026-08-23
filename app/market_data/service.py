"""Production market-data service backed exclusively by ``MarketDataRouter``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

from app.market_data.contracts import AssetClass, Capability, DelayClass, FabricRequest, FabricResult
from app.market_data.router import MarketDataRouter
from app.configuration.settings import normalize_provider_mode
from app.models.schemas import (
    AdjustmentMode,
    AssetType,
    DataDelay,
    Instrument,
    InstrumentIdentifier,
    OhlcvBar,
    ProviderMetadata,
    QuoteSnapshot,
)
from app.providers.base import ProviderCapability, ProviderResult, ProviderUnavailable


def _model_delay(value: DelayClass) -> DataDelay:
    return {
        DelayClass.REALTIME: DataDelay.REALTIME,
        DelayClass.DELAYED: DataDelay.DELAYED,
        DelayClass.END_OF_DAY: DataDelay.END_OF_DAY,
        DelayClass.REFERENCE: DataDelay.END_OF_DAY,
    }[value]


class FabricMarketDataService:
    """Legacy-shaped facade whose quote/history calls always enter the fabric."""

    provider_id = "fabric"
    provider_name = "RangeScout Provider Fabric"
    capabilities = ProviderCapability(
        can_lookup_symbol=True,
        can_fetch_quote=True,
        can_fetch_historical=True,
        can_fetch_actions=False,
        delay=DataDelay.DELAYED,
        supports_realtime=True,
        supports_adjusted=False,
        supports_indices=True,
        supports_etf=True,
        supports_stock=True,
    )

    def __init__(self, router: MarketDataRouter, provider_mode: str = "smart") -> None:
        self.router = router
        self.provider_mode = normalize_provider_mode(provider_mode)

    def set_provider_mode(self, provider_mode: str) -> str:
        self.provider_mode = normalize_provider_mode(provider_mode)
        return self.provider_mode

    def _forced_provider_id(self) -> str | None:
        return None if self.provider_mode == "smart" else self.provider_mode

    @staticmethod
    def resolve_instrument(
        symbol: str, *, asset_class: AssetClass | None = None, exchange: str | None = None,
    ) -> Instrument:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("Symbol is required.")
        canonical_class = asset_class or infer_asset_class(normalized)
        return Instrument(
            identifier=InstrumentIdentifier(normalized, exchange),
            name=normalized,
            asset_type=_model_asset_type(canonical_class),
            currency="USD",
            provider="fabric",
        )

    def fetch_quote(
        self,
        symbol: str,
        *,
        asset_class: AssetClass | None = None,
        canonical_instrument_id: str | None = None,
        provider_symbols: tuple[tuple[str, str], ...] = (),
        cancellation_event: Event | None = None,
    ) -> ProviderResult:
        asset_class = asset_class or infer_asset_class(symbol)
        instrument = self.resolve_instrument(symbol, asset_class=asset_class)
        request = FabricRequest(
            canonical_instrument_id=canonical_instrument_id or f"{asset_class.value}:{instrument.identifier.symbol}",
            canonical_symbol=instrument.identifier.symbol,
            asset_class=asset_class,
            capability=Capability.QUOTE,
            caller_context="production-ui",
            provider_symbol_overrides=tuple(provider_symbols),
        )
        # A visible quote must never keep the UI waiting on a stalled provider.
        return self._provider_result(
            request,
            self.router.fetch(
                request,
                budget_seconds=3.0,
                forced_provider_id=self._forced_provider_id(),
                cancellation_event=cancellation_event,
            ),
        )

    def fetch_historical(
        self,
        identifier: InstrumentIdentifier,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: AdjustmentMode = AdjustmentMode.RAW,
        *,
        asset_class: AssetClass | None = None,
        canonical_instrument_id: str | None = None,
        provider_symbols: tuple[tuple[str, str], ...] = (),
        cancellation_event: Event | None = None,
        interval: str = "1day",
    ) -> ProviderResult:
        asset_class = asset_class or infer_asset_class(identifier.symbol)
        request = FabricRequest(
            canonical_instrument_id=canonical_instrument_id or f"{asset_class.value}:{identifier.symbol.upper()}",
            canonical_symbol=identifier.symbol.upper(),
            asset_class=asset_class,
            capability=Capability.HISTORICAL,
            venue=identifier.exchange,
            start=start,
            end=end,
            interval=interval,
            adjustment=adjusted.value,
            caller_context="production-ui",
            provider_symbol_overrides=tuple(provider_symbols),
        )
        # History is independent/background work and receives a separate budget.
        return self._provider_result(
            request, self.router.fetch(
                request, budget_seconds=15.0, forced_provider_id=self._forced_provider_id(),
                cancellation_event=cancellation_event,
            )
        )

    def fetch_actions(self, identifier: InstrumentIdentifier) -> ProviderResult:  # noqa: ARG002
        raise ProviderUnavailable("Corporate actions are unavailable through the production fabric.")

    def fetch_news(self, symbol: str, *, days: int = 7) -> ProviderResult:
        instrument = self.resolve_instrument(symbol)
        end = datetime.now(timezone.utc)
        request = FabricRequest(
            canonical_instrument_id=f"equity:{instrument.identifier.symbol}",
            canonical_symbol=instrument.identifier.symbol,
            asset_class=AssetClass.EQUITY,
            capability=Capability.NEWS,
            start=end - timedelta(days=max(1, min(days, 30))),
            end=end,
            caller_context="progressive-news-enrichment",
        )
        result = self.router.fetch(request, budget_seconds=8.0)
        return self._provider_result(request, result)

    def capabilities_report(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            supports_real_time=True,
            supports_adjusted=False,
            delay_label=DataDelay.DELAYED,
            capabilities={"routing": "validated multi-provider race", "network_required": True},
        )

    def _provider_result(self, request: FabricRequest, result: FabricResult) -> ProviderResult:
        metadata = ProviderMetadata(
            provider_id=result.provider_id,
            provider_name=self._provider_display_name(result.provider_id),
            supports_real_time=result.delay_class == DelayClass.REALTIME,
            supports_adjusted=request.adjustment == AdjustmentMode.ADJUSTED.value,
            delay_label=_model_delay(result.delay_class),
            capabilities={
                "fabric_request_id": result.request_id,
                "provider_symbol": result.provider_symbol,
                "attribution": result.attribution,
                "warnings": list(result.warnings),
            },
        )
        payload = result.payload
        if request.capability == Capability.QUOTE and not isinstance(payload, QuoteSnapshot):
            payload = self._quote_from_mapping(request, result)
        elif request.capability in {Capability.HISTORICAL, Capability.CANDLES}:
            payload = self._history_payload(request, result)
        return ProviderResult(
            kind=request.capability.value,
            payload=payload,
            timestamp=result.received_at,
            metadata=metadata,
            warnings=list(result.warnings),
        )

    def _provider_display_name(self, provider_id: str) -> str:
        try:
            return self.router.registry.get(provider_id).descriptor.display_name
        except KeyError:
            return provider_id

    def _quote_from_mapping(self, request: FabricRequest, result: FabricResult) -> QuoteSnapshot:
        if not isinstance(result.payload, dict):
            raise ProviderUnavailable(f"{result.provider_id} returned an unsupported quote shape.")
        raw_price = result.payload.get("price", result.payload.get("last"))
        if raw_price is None:
            raise ProviderUnavailable(f"{result.provider_id} returned no usable quote price.")
        raw_volume = result.payload.get("volume")
        try:
            volume = int(Decimal(str(raw_volume))) if raw_volume not in (None, "") else None
        except Exception:
            volume = None
        instrument = self.resolve_instrument(
            request.canonical_symbol, asset_class=request.asset_class, exchange=request.venue
        )
        instrument = Instrument(
            identifier=instrument.identifier,
            name=instrument.name,
            asset_type=_model_asset_type(request.asset_class),
            currency=result.currency,
            provider=result.provider_id,
        )
        return QuoteSnapshot(
            instrument=instrument,
            last=Decimal(str(raw_price)),
            previous_close=None,
            volume=volume,
            timestamp=result.received_at,
            provider_timestamp=result.provider_timestamp,
            delay_label=_model_delay(result.delay_class),
            delay_seconds=None,
            currency=result.currency,
        )

    def _history_payload(self, request: FabricRequest, result: FabricResult):
        if isinstance(result.payload, tuple) and len(result.payload) == 2:
            return result.payload
        if not isinstance(result.payload, dict):
            raise ProviderUnavailable(f"{result.provider_id} returned an unsupported history shape.")
        raw_bars = result.payload.get("bars")
        if isinstance(raw_bars, dict):
            rows = [dict(values, datetime=day) for day, values in raw_bars.items() if isinstance(values, dict)]
        elif isinstance(raw_bars, list):
            rows = raw_bars
        else:
            raise ProviderUnavailable(f"{result.provider_id} returned no usable historical bars.")
        bars: list[OhlcvBar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                stamp = _bar_datetime(row)
                bars.append(
                    OhlcvBar(
                        instrument=InstrumentIdentifier(request.canonical_symbol, request.venue),
                        date=stamp.date(),
                        open=Decimal(str(row["open"])),
                        high=Decimal(str(row["high"])),
                        low=Decimal(str(row["low"])),
                        close=Decimal(str(row["close"])),
                        volume=int(Decimal(str(row.get("volume") or 0))),
                        provider=result.provider_id,
                        adjusted=request.adjustment == AdjustmentMode.ADJUSTED.value,
                        source=result.provider_id,
                        provider_timestamp=stamp,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not bars:
            raise ProviderUnavailable(f"{result.provider_id} returned no valid historical bars.")
        return sorted(bars, key=lambda bar: bar.date), []


def _bar_datetime(row: dict[str, object]) -> datetime:
    raw = row.get("datetime", row.get("date", row.get("timestamp")))
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, timezone.utc)
    if not isinstance(raw, str):
        raise ValueError("Bar timestamp is missing.")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_FIAT_CODES = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY", "HKD", "SGD", "MXN", "BRL", "INR"})
_CRYPTO_CODES = frozenset({"BTC", "XBT", "ETH", "SOL", "XRP", "DOGE", "ADA", "USDT", "USDC"})
_METAL_CODES = frozenset({"XAU", "XAG", "XPT", "XPD"})


def _model_asset_type(asset_class: AssetClass) -> AssetType:
    return {
        AssetClass.EQUITY: AssetType.STOCK,
        AssetClass.ADR: AssetType.STOCK,
        AssetClass.OTC: AssetType.STOCK,
        AssetClass.PREFERRED: AssetType.PREFERRED,
        AssetClass.ETF: AssetType.ETF,
        AssetClass.CLOSED_END_FUND: AssetType.CLOSED_END_FUND,
        AssetClass.INDEX: AssetType.INDEX,
        AssetClass.COMMODITY_SPOT: AssetType.COMMODITY_SPOT,
        AssetClass.FUTURE: AssetType.FUTURE,
        AssetClass.FX: AssetType.FOREX,
        AssetClass.CRYPTO_SPOT: AssetType.CRYPTO,
    }.get(asset_class, AssetType.UNKNOWN)


def infer_asset_class(symbol: str) -> AssetClass:
    """Legacy fallback only; canonical callers must pass their resolved class explicitly."""

    normalized = symbol.strip().upper().replace("=X", "").replace("/", "-")
    if normalized.startswith("^"):
        return AssetClass.INDEX
    if "-" in normalized:
        base, quote = normalized.rsplit("-", 1)
        if base in _METAL_CODES and quote in _FIAT_CODES:
            return AssetClass.COMMODITY_SPOT
        if base in _FIAT_CODES and quote in _FIAT_CODES:
            return AssetClass.FX
        if base in _CRYPTO_CODES or quote in _CRYPTO_CODES:
            return AssetClass.CRYPTO_SPOT
    if symbol.strip().upper().endswith("=X") and len(normalized) == 6:
        return AssetClass.FX
    return AssetClass.EQUITY
