"""Deterministic mock provider used for offline testing and CI."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal
from app.domain.errors import ValidationError
from app.models.schemas import (
    AdjustmentMode,
    CorporateAction,
    DataDelay,
    Instrument,
    InstrumentIdentifier,
    OhlcvBar,
    ProviderMetadata,
    QuoteSnapshot,
    Split,
)
from app.models.schemas import AssetType
from app.providers.base import MarketDataProvider, ProviderCapability, ProviderError, ProviderResult


def _seed_for(symbol: str, scenario: str) -> int:
    key = f"{symbol}:{scenario}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()
    return int(digest[:8], 16)


def _scenario_from_symbol(symbol: str) -> str:
    base = symbol.upper()
    if base.endswith("UP"): return "trending_up"
    if base.endswith("DN"): return "trending_down"
    if base.endswith("FLAT"): return "flat"
    if base.endswith("HV"): return "high_volatility"
    if base.endswith("LV"): return "low_volatility"
    if base.endswith("DIV"): return "dividend"
    if base.endswith("SPL"): return "split"
    return "stable"


class MockMarketDataProvider:
    provider_id = "mock"
    provider_name = "RangeScout Mock Provider"
    capabilities = ProviderCapability(
        can_lookup_symbol=True,
        can_fetch_quote=True,
        can_fetch_historical=True,
        can_fetch_actions=True,
        delay=DataDelay.DELAYED,
        supports_realtime=False,
        supports_adjusted=True,
        supports_indices=True,
        supports_etf=True,
        supports_stock=True,
    )

    def __init__(self, scenario_overrides: dict[str, str] | None = None) -> None:
        self._scenario_overrides = scenario_overrides or {}

    def resolve_instrument(self, symbol: str) -> Instrument:
        symbol_u = symbol.upper().strip()
        if not symbol_u:
            raise ValidationError("Symbol is required.")
        if symbol_u == "DEL":
            raise ProviderError("Unsupported ticker")
        return Instrument(
            identifier=InstrumentIdentifier(symbol=symbol_u),
            name=f"{symbol_u} Mock Co",
            asset_type=AssetType.STOCK,
            currency="USD",
            provider=self.provider_id,
        )

    def fetch_quote(self, symbol: str) -> ProviderResult:
        instrument = self.resolve_instrument(symbol)
        price = Decimal("100.00")
        if symbol.endswith("DN"):
            price = Decimal("74.50")
        elif symbol.endswith("UP"):
            price = Decimal("142.25")
        return ProviderResult(
            kind="quote",
            payload=QuoteSnapshot(
                instrument=instrument,
                last=price,
                previous_close=price - Decimal("1.20"),
                volume=123_400,
                timestamp=datetime.now(timezone.utc),
                provider_timestamp=datetime.now(timezone.utc),
                delay_label=DataDelay.DELAYED,
                delay_seconds=900,
            ),
            timestamp=datetime.now(timezone.utc),
            metadata=self.capabilities_report(),
        )

    def fetch_historical(
        self,
        identifier: InstrumentIdentifier,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResult:
        symbol = identifier.symbol
        scenario = self._scenario_overrides.get(symbol, _scenario_from_symbol(symbol))
        bars = self._generate_bars(symbol, scenario, start, end)
        actions = self._actions_for_scenario(symbol, scenario)
        return ProviderResult(
            kind="historical",
            payload=(bars, actions),
            timestamp=datetime.now(timezone.utc),
            metadata=self.capabilities_report(),
        )

    def fetch_actions(self, identifier: InstrumentIdentifier) -> ProviderResult:
        symbol = identifier.symbol
        scenario = self._scenario_overrides.get(symbol, _scenario_from_symbol(symbol))
        actions = self._actions_for_scenario(symbol, scenario)
        return ProviderResult(
            kind="actions",
            payload=actions,
            timestamp=datetime.now(timezone.utc),
            metadata=self.capabilities_report(),
        )

    def capabilities_report(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            supports_real_time=False,
            supports_adjusted=True,
            delay_label=DataDelay.DELAYED,
            capabilities={
                "scenarios": [
                    "normal",
                    "trending_up",
                    "trending_down",
                    "flat",
                    "high_volatility",
                    "low_volatility",
                    "split",
                    "dividend",
                ]
            },
        )

    def _generate_bars(
        self,
        symbol: str,
        scenario: str,
        start: datetime | None,
        end: datetime | None,
    ) -> list[OhlcvBar]:
        start_date = (start or datetime.now(timezone.utc) - timedelta(days=365)).date()
        end_date = end.date() if isinstance(end, datetime) else (end.date() if isinstance(end, date) else datetime.now(timezone.utc).date())
        days = (end_date - start_date).days + 1
        max_points = 3650 if scenario in {"trending_up", "trending_down", "flat", "high_volatility", "low_volatility"} else 365
        steps = max(1, min(days, max_points))

        seed = _seed_for(symbol, scenario)
        base = Decimal(str((seed % 80) + 50))
        out: list[OhlcvBar] = []
        minimum_price = Decimal("0.50")
        running = base

        for i in range(steps):
            ts = start_date + timedelta(days=i)
            drift_seed = Decimal(seed >> (i % 12) & 7) / Decimal("1000")
            drift = drift_seed
            if scenario == "trending_up":
                drift += Decimal("0.40")
            elif scenario == "trending_down":
                drift -= Decimal("0.25")
            elif scenario == "flat":
                drift = Decimal("0.01")
            elif scenario == "high_volatility":
                drift = (Decimal((seed % 11) - 5) / Decimal("100"))
            else:
                pass
            if scenario == "low_volatility":
                drift = drift / Decimal("3")
            if scenario == "high_volatility":
                drift = drift * Decimal("2")

            close = (running + drift).quantize(Decimal("0.01"))
            if close < minimum_price:
                close = minimum_price

            open_ = (running + (drift / Decimal("2"))).quantize(Decimal("0.01"))
            if open_ < minimum_price:
                open_ = minimum_price

            spread = (running * Decimal("0.005")).quantize(Decimal("0.01"))
            if spread <= Decimal("0.01"):
                spread = Decimal("0.01")

            high = (max(open_, close) + spread).quantize(Decimal("0.01"))
            low = (min(open_, close) - spread).quantize(Decimal("0.01"))
            low = max(low, minimum_price)
            if low > min(open_, close):
                low = min(open_, close)

            vol = 100_000 + int((seed % 5_000) * (1 + (i % 7)))
            out.append(
                OhlcvBar(
                    instrument=InstrumentIdentifier(symbol=symbol.upper()),
                    date=ts,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=vol,
                    provider=self.provider_id,
                    adjusted=scenario in {"split", "dividend", "trending_up", "trending_down", "flat", "high_volatility", "low_volatility"},
                    source="mock",
                    provider_timestamp=datetime.now(timezone.utc),
                    source_timezone="UTC",
                )
            )
            running = close
        return out

    def _actions_for_scenario(self, symbol: str, scenario: str) -> list[CorporateAction]:
        instrument = InstrumentIdentifier(symbol=symbol.upper())
        if scenario == "split":
            return [
                Split(
                    instrument=instrument,
                    action_type="split",
                    effective_at=(date.today() - timedelta(days=120)),
                    details={"ratio": "2:1"},
                )
            ]
        if scenario == "dividend":
            return [
                CorporateAction(
                    instrument=instrument,
                    action_type="dividend",
                    effective_at=(date.today() - timedelta(days=45)),
                    details={"amount": "0.75", "currency": "USD"},
                )
            ]
        return []


class FakeYahooProvider(MockMarketDataProvider):
    """Deterministic Yahoo-shaped provider available only to the test suite."""

    provider_id = "yahoo"
    provider_name = "Yahoo Test Fixture"


def build_test_provider_registry(credential_store=None):
    from app.providers.byo_provider import FinnhubProvider
    from app.providers.public_policy import PUBLIC_PROVIDER_IDS
    from app.providers.registry import ProviderRegistry
    from app.security.credentials import InMemoryCredentialStore

    store = credential_store or InMemoryCredentialStore()
    registry = ProviderRegistry(PUBLIC_PROVIDER_IDS)
    registry.register(FakeYahooProvider())
    registry.register(FinnhubProvider(store.load))
    return registry
