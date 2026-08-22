"""Production market-data service backed exclusively by ``MarketDataRouter``."""

from __future__ import annotations

from datetime import datetime, timezone
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
    def resolve_instrument(symbol: str) -> Instrument:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("Symbol is required.")
        return Instrument(
            identifier=InstrumentIdentifier(normalized),
            name=normalized,
            asset_type=AssetType.STOCK,
            currency="USD",
            provider="fabric",
        )

    def fetch_quote(
        self,
        symbol: str,
        *,
        asset_class: AssetClass | None = None,
        cancellation_event: Event | None = None,
    ) -> ProviderResult:
        instrument = self.resolve_instrument(symbol)
        asset_class = asset_class or infer_asset_class(instrument.identifier.symbol)
        request = FabricRequest(
            canonical_instrument_id=f"{asset_class.value}:{instrument.identifier.symbol}",
            canonical_symbol=instrument.identifier.symbol,
            asset_class=asset_class,
            capability=Capability.QUOTE,
            caller_context="production-ui",
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
        interval: str = "1day",
    ) -> ProviderResult:
        asset_class = asset_class or infer_asset_class(identifier.symbol)
        request = FabricRequest(
            canonical_instrument_id=f"{asset_class.value}:{identifier.symbol.upper()}",
            canonical_symbol=identifier.symbol.upper(),
            asset_class=asset_class,
            capability=Capability.HISTORICAL,
            venue=identifier.exchange,
            start=start,
            end=end,
            interval=interval,
            adjustment=adjusted.value,
            caller_context="production-ui",
        )
        # History is independent/background work and receives a separate budget.
        return self._provider_result(
            request, self.router.fetch(request, budget_seconds=15.0, forced_provider_id=self._forced_provider_id())
        )

    def fetch_actions(self, identifier: InstrumentIdentifier) -> ProviderResult:  # noqa: ARG002
        raise ProviderUnavailable("Corporate actions are unavailable through the production fabric.")

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
        instrument = self.resolve_instrument(request.canonical_symbol)
        instrument = Instrument(
            identifier=instrument.identifier,
            name=instrument.name,
            asset_type=instrument.asset_type,
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


def infer_asset_class(symbol: str) -> AssetClass:
    normalized = symbol.strip().upper().replace("/", "-")
    if normalized.startswith("^"):
        return AssetClass.INDEX
    if normalized.endswith("=X"):
        return AssetClass.FX
    if "-" in normalized:
        base, quote = normalized.rsplit("-", 1)
        if base and quote in {"USD", "USDT", "USDC", "EUR", "BTC", "ETH"}:
            return AssetClass.CRYPTO_SPOT
    return AssetClass.EQUITY
