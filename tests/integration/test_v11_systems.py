from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.alerts.dispatcher import AlertDispatcher, AlertNotification, AlertPreferences, AlertType
from app.analytics.trading_indicators import calculate_indicators
from app.catalysts.correlation import CatalystCorrelator
from app.catalysts.normalization import normalize_event
from app.catalysts.symbol_mapping import SymbolCatalog
from app.providers.registry import default_provider_registry
from app.scanner.engine import ScannerObservation, permitted_scan_universe, scan_observations
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials
from app.streaming.candle_aggregator import CandleAggregator
from app.streaming.events import TradeEvent
from app.streaming.ticker import plan_ticker_subscriptions


def test_offline_v11_provider_to_catalyst_alert_scanner_flow() -> None:
    store = InMemoryCredentialStore()
    store.save(ProviderCredentials("finnhub", {"api_key": "F" * 24}))
    registry = default_provider_registry(credential_store=store)
    assert registry.list_available() == ["yahoo", "finnhub"]

    base = datetime(2026, 8, 17, 13, 30, tzinfo=timezone.utc)
    candles = []
    aggregator = CandleAggregator(60)
    for index in range(22):
        update = aggregator.process(TradeEvent("mock", "AAPL", Decimal(100 + index), Decimal(100 if index < 21 else 300), base + timedelta(minutes=index), str(index)))
        if update.completed: candles.append(update.completed)
    candles.append(aggregator.current("AAPL"))
    indicators = calculate_indicators(candles, Decimal("99"))
    assert indicators.ema9 > indicators.ema20 and indicators.rvol >= Decimal("1")

    catalog = SymbolCatalog(); catalog.register("AAPL", "Apple Inc", "Technology", "Apple")
    catalyst = normalize_event("SEC", "https://www.sec.gov/aapl", base, "Apple Inc filed 8-K", received_at=base)
    correlated = CatalystCorrelator(catalog).correlate([catalyst], "AAPL", {"MSFT"}, {"Technology"})
    assert correlated[0].event.symbols == ("AAPL",)

    visual = []
    dispatcher = AlertDispatcher(AlertPreferences(), visual=visual.append, clock=lambda: base)
    assert dispatcher.dispatch(AlertNotification(catalyst.event_id, AlertType.SEC_FILING, "SEC", catalyst.title, "AAPL", base))
    assert len(visual) == 1

    universe = permitted_scan_universe("AAPL", ("MSFT",), ["NVDA"])
    hits = scan_observations([ScannerObservation("AAPL", candles[-1].close, rvol=Decimal(3), sec_catalyst=True)], universe)
    assert {hit.rule for hit in hits} == {"unusual_volume", "sec_catalyst"}
    ticker = plan_ticker_subscriptions(["AAPL", "MSFT", "NVDA"], 2)
    assert ticker.subscribed == ("AAPL", "MSFT") and ticker.overflow == ("NVDA",)
