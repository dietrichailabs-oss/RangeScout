"""Stateful streaming connection orchestration.

The transport is callback-driven so socket I/O never blocks the Qt UI thread.
Tests use a deterministic fake transport; production uses ``QtWebSocketTransport``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from app.security.credentials import ProviderCredentials
from app.security.secrets import redact_secrets
from app.streaming.events import StreamState, StreamStatus, StreamingError, TradeEvent
from app.streaming.reconnect import ReconnectPolicy
from app.streaming.subscriptions import SubscriptionBook


class StreamTransport(Protocol):
    def set_callbacks(
        self,
        *,
        opened: Callable[[], None],
        message: Callable[[str], None],
        closed: Callable[[], None],
        failed: Callable[[str], None],
    ) -> None: ...

    def open(self) -> None: ...
    def send(self, payload: str) -> None: ...
    def close(self) -> None: ...


class StreamingConnection:
    def __init__(
        self,
        provider: str,
        credentials: ProviderCredentials,
        transport: StreamTransport,
        decode: Callable[[str, datetime], tuple[list[TradeEvent], bool]],
        authentication_payload: Callable[[ProviderCredentials], str | None],
        subscribe_payload: Callable[[tuple[str, ...]], str | None],
        unsubscribe_payload: Callable[[tuple[str, ...]], str | None],
        *,
        subscription_limit: int | None = None,
        stale_after_seconds: float = 15.0,
        reconnect_policy: ReconnectPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        schedule: Callable[[float, Callable[[], None]], object] | None = None,
    ) -> None:
        self.provider = provider.strip().lower()
        self._credentials = credentials
        self._transport = transport
        self._decode = decode
        self._authentication_payload = authentication_payload
        self._subscribe_payload = subscribe_payload
        self._unsubscribe_payload = unsubscribe_payload
        self._book = SubscriptionBook(subscription_limit)
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._policy = reconnect_policy or ReconnectPolicy()
        self._clock = clock
        self._schedule = schedule
        self._state = StreamState.DISCONNECTED
        self._attempt = 0
        self._last_message_at: datetime | None = None
        self._manual_close = False
        self._reconnect_pending = False
        self._trade_listeners: list[Callable[[TradeEvent], None]] = []
        self._status_listeners: list[Callable[[StreamStatus], None]] = []
        transport.set_callbacks(opened=self._on_open, message=self._on_message, closed=self._on_close, failed=self._on_error)

    @property
    def state(self) -> StreamState:
        return self._state

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._book.symbols

    def add_trade_listener(self, listener: Callable[[TradeEvent], None]) -> None:
        self._trade_listeners.append(listener)

    def add_status_listener(self, listener: Callable[[StreamStatus], None]) -> None:
        self._status_listeners.append(listener)

    def connect(self) -> None:
        if self._state not in {StreamState.DISCONNECTED, StreamState.ERROR, StreamState.STALE}:
            return
        self._manual_close = False
        self._reconnect_pending = False
        self._set_state(StreamState.CONNECTING, "Connecting to provider stream.")
        self._transport.open()

    def disconnect(self) -> None:
        self._manual_close = True
        self._reconnect_pending = False
        self._set_state(StreamState.STOPPING, "Closing provider stream.")
        self._transport.close()

    def subscribe(self, *symbols: str) -> None:
        change = self._book.subscribe(set(symbols))
        if change.added and self._state == StreamState.CONNECTED:
            self._send(self._subscribe_payload(change.added))

    def unsubscribe(self, *symbols: str) -> None:
        change = self._book.unsubscribe(set(symbols))
        if change.removed and self._state == StreamState.CONNECTED:
            self._send(self._unsubscribe_payload(change.removed))

    def health_check(self) -> bool:
        if self._state != StreamState.CONNECTED or self._last_message_at is None:
            return self._state == StreamState.CONNECTED
        if self._clock() - self._last_message_at <= self._stale_after:
            return True
        self._set_state(StreamState.STALE, "Provider stream is stale; reconnecting.")
        self._transport.close()
        self._request_reconnect()
        return False

    def _on_open(self) -> None:
        self._reconnect_pending = False
        self._attempt = 0
        authentication = self._authentication_payload(self._credentials)
        if authentication:
            self._set_state(StreamState.AUTHENTICATING, "Authenticating provider stream.")
            self._send(authentication)
        else:
            self._mark_connected()

    def _mark_connected(self) -> None:
        self._last_message_at = self._clock()
        self._set_state(StreamState.CONNECTED, "Provider stream connected.")
        self._send(self._subscribe_payload(self.symbols))

    def _on_message(self, payload: str) -> None:
        received_at = self._clock()
        try:
            events, authenticated = self._decode(payload, received_at)
        except Exception:
            self._on_error("Provider returned an invalid streaming message.")
            return
        self._last_message_at = received_at
        if authenticated and self._state == StreamState.AUTHENTICATING:
            self._mark_connected()
        for event in events:
            for listener in tuple(self._trade_listeners):
                listener(event)

    def _on_close(self) -> None:
        if self._manual_close:
            self._set_state(StreamState.DISCONNECTED, "Provider stream disconnected.")
            return
        self._request_reconnect()

    def _on_error(self, message: str) -> None:
        safe = redact_secrets(str(message))
        self._set_state(StreamState.ERROR, safe if safe else "Provider streaming error.")
        if not self._manual_close:
            self._request_reconnect()

    def _request_reconnect(self) -> None:
        if self._reconnect_pending or self._manual_close:
            return
        self._reconnect_pending = True
        self._attempt += 1
        if not self._policy.permits(self._attempt):
            self._reconnect_pending = False
            self._set_state(StreamState.ERROR, "Provider reconnect limit reached.")
            return
        self._set_state(StreamState.RECONNECTING, "Provider stream reconnect scheduled.")
        if self._schedule is None:
            return
        self._schedule(self._policy.delay(self._attempt), self._reconnect)

    def _reconnect(self) -> None:
        if self._manual_close:
            self._reconnect_pending = False
            return
        self._reconnect_pending = False
        self._set_state(StreamState.CONNECTING, "Reconnecting to provider stream.")
        self._transport.open()

    def _send(self, payload: str | None) -> None:
        if payload:
            try:
                self._transport.send(payload)
            except Exception:
                raise StreamingError("Provider stream send failed safely.") from None

    def _set_state(self, state: StreamState, message: str) -> None:
        self._state = state
        status = StreamStatus(self.provider, state, redact_secrets(message), self._clock(), self._attempt)
        for listener in tuple(self._status_listeners):
            listener(status)
