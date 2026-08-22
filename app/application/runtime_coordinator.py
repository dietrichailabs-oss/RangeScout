"""Single production composition root for RangeScout live runtime services."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from app.alerts.dispatcher import AlertDispatcher, AlertNotification, AlertPreferences, AlertType
from app.application.catalyst_runtime import (
    CatalystRuntime,
    CatalystSource,
    build_congress_source,
    build_official_sources,
)
from app.application.live_trading_runtime import LiveSymbolState, LiveTradingRuntime, LiveTradingSink
from app.catalysts.correlation import CorrelatedEvent
from app.catalysts.storage import CatalystStore
from app.catalysts.symbol_mapping import SymbolCatalog
from app.scanner.engine import ScanHit
from app.security.credentials import CredentialStore, ProviderCredentials
from app.streaming.connection import StreamTransport
from app.streaming.events import StreamStatus
from app.streaming.ticker import TickerSubscriptionPlan


SEC_USER_AGENT = "RangeScout/1.3 Dietrich AI Labs dietrichailabs@gmail.com"


class RuntimeView(Protocol):
    def runtime_stream_status(self, status: StreamStatus | None, display_text: str) -> None: ...
    def runtime_live_state(self, state: LiveSymbolState) -> None: ...
    def runtime_ticker_state(self, states: dict[str, LiveSymbolState], plan: TickerSubscriptionPlan) -> None: ...
    def runtime_scanner_hits(self, hits: list[ScanHit]) -> None: ...
    def runtime_alert_notification(self, notification: AlertNotification) -> None: ...
    def set_catalyst_events(self, events: list[CorrelatedEvent]) -> None: ...


class RuntimeCoordinator(LiveTradingSink):
    def __init__(
        self,
        view: RuntimeView,
        credential_store: CredentialStore,
        data_dir: Path,
        transport_factory: Callable[[str, ProviderCredentials], StreamTransport],
        schedule: Callable[[float, Callable[[], None]], object],
        post_to_owner: Callable[[Callable[[], None]], None],
        *,
        catalyst_sources: list[CatalystSource] | None = None,
        executor: ThreadPoolExecutor | None = None,
        user_agent: str = SEC_USER_AGENT,
    ) -> None:
        self.view = view
        self._credential_store = credential_store
        self._user_agent = user_agent
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="RangeScoutFeeds")
        self._owns_executor = executor is None
        self.alert_dispatcher = AlertDispatcher(
            AlertPreferences(),
            visual=view.runtime_alert_notification,
            sound=getattr(view, "runtime_alert_sound", None),
            desktop=getattr(view, "runtime_alert_desktop", None),
        )
        self.live = LiveTradingRuntime(credential_store, self, transport_factory, schedule)
        sources = catalyst_sources if catalyst_sources is not None else build_official_sources(credential_store, data_dir, user_agent)
        self.catalysts = CatalystRuntime(
            sources,
            self._executor,
            post_to_owner,
            SymbolCatalog(),
            CatalystStore(data_dir / "catalysts.json", maximum_events=1000),
            view.set_catalyst_events,
            self.runtime_alert,
            self.live.set_halt_status,
        )
        self._shutdown = False

    def refresh_credential_source(self, provider_id: str) -> None:
        """Synchronize credential-backed runtime sources without restarting."""

        if str(provider_id).strip().lower() != "congress" or self._shutdown:
            return
        self.catalysts.replace_source(
            "congress",
            build_congress_source(self._credential_store, self._user_agent),
        )

    def start(self, provider: str, active_symbol: str, watchlist_symbols: list[str]) -> None:
        self._shutdown = False
        self.catalysts.set_context(active_symbol, set(watchlist_symbols))
        self.live.start(provider, active_symbol, watchlist_symbols)

    def set_provider(self, provider: str) -> None:
        self.live.set_provider(provider)

    def set_symbols(self, active_symbol: str, watchlist_symbols: list[str]) -> None:
        self.live.set_symbols(active_symbol, watchlist_symbols)
        self.catalysts.set_context(active_symbol, set(watchlist_symbols))

    def set_interval(self, interval_seconds: int) -> None:
        self.live.set_interval(interval_seconds)

    def update_snapshot(self, symbol, price, previous_close, timestamp) -> None:
        self.live.update_snapshot(symbol, price, previous_close, timestamp)

    def tick(self) -> None:
        if self._shutdown:
            return
        self.live.health_check()
        self.catalysts.poll_due()

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self.catalysts.shutdown()
        self.live.shutdown()
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def configure_alerts(self, preferences: AlertPreferences) -> None:
        self.alert_dispatcher.preferences = preferences

    def stream_status(self, status: StreamStatus | None, display_text: str) -> None:
        self.view.runtime_stream_status(status, display_text)

    def live_state(self, state: LiveSymbolState) -> None:
        self.view.runtime_live_state(state)

    def ticker_state(self, states: dict[str, LiveSymbolState], plan: TickerSubscriptionPlan) -> None:
        self.view.runtime_ticker_state(states, plan)

    def scanner_hits(self, hits: list[ScanHit]) -> None:
        self.view.runtime_scanner_hits(hits)

    def runtime_alert(self, alert_type: AlertType, event_id: str, title: str, message: str, symbol: str | None) -> None:
        notification = AlertNotification(event_id, alert_type, title, message, symbol, datetime.now(timezone.utc))
        self.alert_dispatcher.dispatch(notification)
