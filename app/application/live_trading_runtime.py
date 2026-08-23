"""Production ownership of streaming, candles, indicators, scanner, and alerts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Callable, Protocol

from app.alerts.dispatcher import AlertType
from app.analytics.trading_indicators import IndicatorSnapshot, calculate_indicators
from app.scanner.engine import ScanHit, ScannerObservation, permitted_scan_universe, scan_observations
from app.providers.public_policy import require_public_provider
from app.security.credentials import CredentialStore, ProviderCredentials
from app.streaming.candle_aggregator import CandleAggregator, LiveCandle
from app.streaming.connection import StreamTransport, StreamingConnection
from app.streaming.events import StreamState, StreamStatus, TradeEvent
from app.streaming.providers import (
    decode_finnhub,
    finnhub_authentication,
    finnhub_subscribe,
    finnhub_unsubscribe,
)
from app.streaming.ticker import TickerSubscriptionPlan, plan_ticker_subscriptions


STREAM_LIMITS = {"finnhub": 50}
CANDLE_HISTORY_LIMIT = 600


class LiveTradingSink(Protocol):
    def stream_status(self, status: StreamStatus | None, display_text: str) -> None: ...
    def live_state(self, state: "LiveSymbolState") -> None: ...
    def ticker_state(self, states: dict[str, "LiveSymbolState"], plan: TickerSubscriptionPlan) -> None: ...
    def scanner_hits(self, hits: list[ScanHit]) -> None: ...
    def runtime_alert(self, alert_type: AlertType, event_id: str, title: str, message: str, symbol: str | None) -> None: ...


@dataclass(frozen=True, slots=True)
class LiveSymbolState:
    symbol: str
    price: Decimal | None = None
    previous_price: Decimal | None = None
    previous_close: Decimal | None = None
    last_trade_at: datetime | None = None
    current_candle: LiveCandle | None = None
    completed_candles: tuple[LiveCandle, ...] = ()
    indicators: IndicatorSnapshot | None = None
    feed_state: str = "DISCONNECTED"
    halt_status: str | None = None


class LiveTradingRuntime:
    def __init__(
        self,
        credential_store: CredentialStore,
        sink: LiveTradingSink,
        transport_factory: Callable[[str, ProviderCredentials], StreamTransport],
        schedule: Callable[[float, Callable[[], None]], object],
        *,
        interval_seconds: int = 1,
        history_limit: int = CANDLE_HISTORY_LIMIT,
        publish_interval_seconds: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credential_store = credential_store
        self._sink = sink
        self._transport_factory = transport_factory
        self._schedule = schedule
        self._interval = interval_seconds
        self._history_limit = max(20, history_limit)
        self._publish_interval = max(0.0, publish_interval_seconds)
        self._monotonic = monotonic
        self._last_publish: dict[str, float] = {}
        self._aggregator = CandleAggregator(interval_seconds)
        self._history: dict[str, deque[LiveCandle]] = {}
        self._states: dict[str, LiveSymbolState] = {}
        self._connection: StreamingConnection | None = None
        self._provider = "yahoo"
        self._active_symbol = "AAPL"
        self._watchlist_symbols: list[str] = []
        self._plan = plan_ticker_subscriptions((self._active_symbol,), None)
        self._shutdown = False
        self._last_connected_once = False

    @property
    def connection(self) -> StreamingConnection | None:
        return self._connection

    @property
    def subscription_plan(self) -> TickerSubscriptionPlan:
        return self._plan

    @property
    def states(self) -> dict[str, LiveSymbolState]:
        return dict(self._states)

    def start(self, provider: str, active_symbol: str, watchlist_symbols: list[str]) -> None:
        self._shutdown = False
        self._active_symbol = active_symbol.strip().upper() or "AAPL"
        self._watchlist_symbols = list(watchlist_symbols)
        self.set_provider(provider)

    def set_provider(self, provider: str) -> None:
        normalized = require_public_provider(provider)
        if self._connection is not None:
            self._connection.disconnect()
            self._connection = None
        self._provider = normalized
        self._last_connected_once = False
        self._replan()
        if normalized not in STREAM_LIMITS:
            text = "SNAPSHOT MODE" if normalized == "yahoo" else "STREAMING NOT ACTIVE"
            self._set_feed_state(text)
            self._sink.stream_status(None, text)
            return
        try:
            credentials = self._credential_store.load(normalized)
        except Exception:
            self._set_feed_state("SECURE STORAGE UNAVAILABLE")
            self._sink.stream_status(None, "SECURE STORAGE UNAVAILABLE")
            return
        if credentials is None:
            self._set_feed_state("CREDENTIALS REQUIRED")
            self._sink.stream_status(None, "CREDENTIALS REQUIRED")
            return
        transport = self._transport_factory(normalized, credentials)
        codecs = (decode_finnhub, finnhub_authentication, finnhub_subscribe, finnhub_unsubscribe)
        self._connection = StreamingConnection(
            normalized,
            credentials,
            transport,
            *codecs,
            subscription_limit=STREAM_LIMITS[normalized],
            schedule=self._schedule,
        )
        self._connection.add_status_listener(self._on_status)
        self._connection.add_trade_listener(self._on_trade)
        self._connection.subscribe(*self._plan.subscribed)
        self._connection.connect()

    def set_symbols(self, active_symbol: str, watchlist_symbols: list[str]) -> None:
        self._active_symbol = active_symbol.strip().upper() or self._active_symbol
        self._watchlist_symbols = list(watchlist_symbols)
        old = set(self._plan.subscribed)
        self._replan()
        if self._connection is not None:
            new = set(self._plan.subscribed)
            self._connection.unsubscribe(*(old - new))
            self._connection.subscribe(*(new - old))

    def set_interval(self, interval_seconds: int) -> None:
        if interval_seconds == self._interval:
            return
        self._interval = interval_seconds
        self._aggregator.set_interval(interval_seconds)
        self._history.clear()
        self._states = {
            symbol: LiveSymbolState(
                symbol=symbol,
                price=state.price,
                previous_price=state.previous_price,
                previous_close=state.previous_close,
                last_trade_at=state.last_trade_at,
                feed_state=state.feed_state,
                halt_status=state.halt_status,
            )
            for symbol, state in self._states.items()
        }

    def update_snapshot(self, symbol: str, price: Decimal, previous_close: Decimal | None, timestamp: datetime | None) -> None:
        normalized = symbol.strip().upper()
        prior = self._states.get(normalized, LiveSymbolState(normalized))
        # A connected trade stream is the live-price authority. Snapshot
        # refreshes may still improve previous-close context, but must not
        # replace a streaming tick (or its candle/indicator state) with a REST
        # quote that happens to complete later.
        if self._provider in STREAM_LIMITS and prior.feed_state == "CONNECTED" and prior.price is not None:
            state = replace(
                prior,
                previous_close=previous_close if previous_close is not None else prior.previous_close,
            )
            self._states[normalized] = state
            self._publish(state)
            return
        state = LiveSymbolState(
            symbol=normalized,
            price=price,
            previous_price=prior.price,
            previous_close=previous_close,
            last_trade_at=timestamp,
            current_candle=prior.current_candle,
            completed_candles=prior.completed_candles,
            indicators=prior.indicators,
            feed_state=prior.feed_state if self._provider in STREAM_LIMITS else "SNAPSHOT MODE",
            halt_status=prior.halt_status,
        )
        self._states[normalized] = state
        self._publish(state)

    def set_halt_status(self, symbol: str, status: str | None) -> None:
        normalized = symbol.strip().upper()
        prior = self._states.get(normalized, LiveSymbolState(normalized))
        updated = replace(prior, halt_status=status)
        self._states[normalized] = updated
        if normalized == self._active_symbol:
            self._sink.live_state(updated)
        self._sink.ticker_state(self.states, self._plan)
        self._publish(updated)

    def health_check(self) -> bool:
        healthy = self._connection.health_check() if self._connection is not None else True
        now = self._monotonic()
        for symbol, state in tuple(self._states.items()):
            if now - self._last_publish.get(symbol, 0.0) >= self._publish_interval:
                self._publish(state)
                self._last_publish[symbol] = now
        return healthy

    def shutdown(self) -> None:
        self._shutdown = True
        if self._connection is not None:
            self._connection.disconnect()
            self._connection = None

    def _replan(self) -> None:
        ordered = [self._active_symbol, *self._watchlist_symbols]
        self._plan = plan_ticker_subscriptions(ordered, STREAM_LIMITS.get(self._provider))
        self._sink.ticker_state(self.states, self._plan)

    def _on_status(self, status: StreamStatus) -> None:
        if self._shutdown:
            return
        display = status.state.value.upper()
        self._set_feed_state(display)
        self._sink.stream_status(status, display)
        if status.state == StreamState.STALE:
            self._alert(AlertType.STALE_STREAM, f"stream-stale:{status.provider}", "Stale stream", status.message, None)
        elif status.state in {StreamState.ERROR, StreamState.DISCONNECTED}:
            self._alert(AlertType.PROVIDER_DISCONNECT, f"stream-disconnect:{status.provider}:{status.attempt}", "Provider disconnected", status.message, None)
        elif status.state == StreamState.CONNECTED:
            if self._last_connected_once:
                self._alert(AlertType.PROVIDER_RECONNECT, f"stream-reconnect:{status.provider}:{status.changed_at.isoformat()}", "Provider reconnected", status.message, None)
            self._last_connected_once = True

    def _set_feed_state(self, text: str) -> None:
        self._states = {
            symbol: replace(state, feed_state=text)
            for symbol, state in self._states.items()
        }

    def _on_trade(self, trade: TradeEvent) -> None:
        if self._shutdown or trade.symbol not in self._plan.subscribed:
            return
        update = self._aggregator.process(trade)
        if not update.accepted:
            return
        history = self._history.setdefault(trade.symbol, deque(maxlen=self._history_limit))
        if update.completed is not None:
            history.append(update.completed)
        prior = self._states.get(trade.symbol, LiveSymbolState(trade.symbol))
        now = self._monotonic()
        should_publish = update.completed is not None or now - self._last_publish.get(trade.symbol, 0.0) >= self._publish_interval
        indicator = prior.indicators
        if should_publish and prior.previous_close not in (None, Decimal(0)):
            try:
                indicator = calculate_indicators([*history, update.current], prior.previous_close)
            except Exception:
                indicator = None
        state = LiveSymbolState(
            symbol=trade.symbol,
            price=trade.price,
            previous_price=prior.price,
            previous_close=prior.previous_close,
            last_trade_at=trade.timestamp,
            current_candle=update.current,
            completed_candles=tuple(history) if update.completed is not None else prior.completed_candles,
            indicators=indicator,
            feed_state="CONNECTED",
            halt_status=prior.halt_status,
        )
        self._states[trade.symbol] = state
        if should_publish:
            self._publish(state)
            self._last_publish[trade.symbol] = now

    def _publish(self, state: LiveSymbolState) -> None:
        if state.symbol == self._active_symbol:
            self._sink.live_state(state)
        self._sink.ticker_state(self.states, self._plan)
        observations = [self._observation(item) for item in self._states.values() if item.price is not None]
        universe = permitted_scan_universe(self._active_symbol, self._plan.subscribed, self._watchlist_symbols)
        hits = scan_observations(observations, universe)
        self._sink.scanner_hits(hits)
        for hit in hits:
            alert_type = {
                "unusual_volume": AlertType.VOLUME_SPIKE,
                "vwap_cross": AlertType.VWAP_CROSS,
                "opening_range_break": AlertType.OPENING_RANGE_BREAK,
                "new_day_high": AlertType.NEW_DAY_HIGH,
                "new_day_low": AlertType.NEW_DAY_LOW,
            }.get(hit.rule)
            if alert_type is not None:
                self._alert(alert_type, f"scan:{hit.symbol}:{hit.rule}:{hit.detail}", hit.rule.replace("_", " ").title(), hit.detail, hit.symbol)

    @staticmethod
    def _observation(state: LiveSymbolState) -> ScannerObservation:
        indicators = state.indicators
        opening = indicators.opening_range_1m if indicators else None
        return ScannerObservation(
            state.symbol,
            state.price or Decimal(0),
            vwap=indicators.vwap if indicators else None,
            previous_price=state.previous_price,
            opening_range_high=opening[0] if opening else None,
            opening_range_low=opening[1] if opening else None,
            day_high=indicators.day_high if indicators else None,
            day_low=indicators.day_low if indicators else None,
            rvol=indicators.rvol if indicators else None,
            gap_percent=indicators.gap_percent if indicators else None,
            halt_status=state.halt_status,
        )

    def _alert(self, alert_type: AlertType, event_id: str, title: str, message: str, symbol: str | None) -> None:
        self._sink.runtime_alert(alert_type, event_id, title, message, symbol)
