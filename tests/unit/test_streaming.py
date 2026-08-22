from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.security.credentials import ProviderCredentials
from app.streaming.connection import StreamingConnection
from app.streaming.events import StreamState
from app.streaming.providers import (
    alpaca_authentication,
    alpaca_subscribe,
    alpaca_unsubscribe,
    decode_alpaca,
    decode_finnhub,
)
from app.streaming.reconnect import ReconnectPolicy
from app.streaming.subscriptions import SubscriptionBook


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.opens = 0
        self.closes = 0

    def set_callbacks(self, **callbacks): self.callbacks = callbacks
    def open(self): self.opens += 1
    def send(self, payload): self.sent.append(payload)
    def close(self): self.closes += 1


def test_subscription_book_normalizes_dedupes_and_enforces_limit() -> None:
    book = SubscriptionBook(limit=2)
    assert book.subscribe({"aapl", "MSFT", "AAPL"}).added == ("AAPL", "MSFT")
    assert book.subscribe({"AAPL"}).added == ()
    with pytest.raises(ValueError, match="at most 2"):
        book.subscribe({"NVDA"})
    assert book.unsubscribe({"msft"}).removed == ("MSFT",)


def test_provider_decoders_preserve_subsecond_timestamps() -> None:
    now = datetime(2026, 8, 17, tzinfo=timezone.utc)
    finn, _ = decode_finnhub(json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": 10.5, "v": 2, "t": 1786996800123}]}), now)
    alpaca, authenticated = decode_alpaca(json.dumps([{"T": "success", "msg": "authenticated"}, {"T": "t", "S": "MSFT", "p": 20, "s": 3, "t": "2026-08-17T14:00:00.123456Z", "i": 7}]), now)
    assert finn[0].timestamp.microsecond == 123000
    assert alpaca[0].timestamp.microsecond == 123456
    assert alpaca[0].event_id == "7"
    assert authenticated is True


def test_connection_authenticates_subscribes_and_dispatches_without_secret_leak() -> None:
    transport = FakeTransport()
    secret = "ALPACA_SECRET_SENTINEL_123456789"
    credentials = ProviderCredentials("alpaca", {"key_id": "ALPACA_KEY_SENTINEL_123456", "secret_key": secret})
    connection = StreamingConnection("alpaca", credentials, transport, decode_alpaca, alpaca_authentication, alpaca_subscribe, alpaca_unsubscribe, subscription_limit=30)
    trades, statuses = [], []
    connection.add_trade_listener(trades.append)
    connection.add_status_listener(statuses.append)
    connection.subscribe("AAPL")
    connection.connect()
    transport.callbacks["opened"]()
    assert connection.state == StreamState.AUTHENTICATING
    transport.callbacks["message"](json.dumps([{"T": "success", "msg": "authenticated"}]))
    assert connection.state == StreamState.CONNECTED
    transport.callbacks["message"](json.dumps([{"T": "t", "S": "AAPL", "p": 101, "s": 2, "t": "2026-08-17T14:00:00.001Z"}]))
    assert trades[0].symbol == "AAPL"
    assert secret not in repr(statuses)


def test_stale_detection_schedules_one_bounded_reconnect() -> None:
    clock_value = [datetime(2026, 8, 17, tzinfo=timezone.utc)]
    scheduled = []
    transport = FakeTransport()
    credentials = ProviderCredentials("alpaca", {"key_id": "A" * 20, "secret_key": "B" * 20})
    connection = StreamingConnection("alpaca", credentials, transport, decode_alpaca, alpaca_authentication, alpaca_subscribe, alpaca_unsubscribe, stale_after_seconds=5, reconnect_policy=ReconnectPolicy(jitter_fraction=0), clock=lambda: clock_value[0], schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connection.connect(); transport.callbacks["opened"](); transport.callbacks["message"]('[{"T":"success","msg":"authenticated"}]')
    clock_value[0] += timedelta(seconds=6)
    assert connection.health_check() is False
    assert transport.closes == 1
    assert len(scheduled) == 1
    assert connection.state == StreamState.RECONNECTING


def test_manual_disconnect_does_not_reconnect() -> None:
    scheduled = []
    transport = FakeTransport()
    credentials = ProviderCredentials("alpaca", {"key_id": "A" * 20, "secret_key": "B" * 20})
    connection = StreamingConnection("alpaca", credentials, transport, decode_alpaca, alpaca_authentication, alpaca_subscribe, alpaca_unsubscribe, schedule=lambda delay, callback: scheduled.append(callback))
    connection.connect(); connection.disconnect(); transport.callbacks["closed"]()
    assert connection.state == StreamState.DISCONNECTED
    assert scheduled == []
