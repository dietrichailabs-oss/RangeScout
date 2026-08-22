from __future__ import annotations

import json
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.alerts.dispatcher import AlertDispatcher, AlertNotification, AlertPreferences, AlertType
from app.catalysts.dedupe import EventDeduplicator
from app.catalysts.normalization import normalize_event
from app.scanner.engine import ScannerObservation, scan_observations
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials
from app.streaming.candle_aggregator import CandleAggregator
from app.streaming.connection import StreamingConnection
from app.streaming.events import StreamState, TradeEvent
from app.streaming.providers import alpaca_authentication, alpaca_subscribe, alpaca_unsubscribe, decode_alpaca
from app.streaming.subscriptions import SubscriptionBook


class Transport:
    def set_callbacks(self, **callbacks): self.callbacks = callbacks
    def open(self): pass
    def send(self, payload): pass
    def close(self): pass


def connection(schedule):
    credentials = ProviderCredentials("alpaca", {"key_id": "K" * 20, "secret_key": "S" * 20})
    transport = Transport()
    value = StreamingConnection("alpaca", credentials, transport, decode_alpaca, alpaca_authentication, alpaca_subscribe, alpaca_unsubscribe, schedule=schedule)
    return value, transport


def test_network_error_and_close_schedule_exactly_one_reconnect() -> None:
    scheduled = []
    value, transport = connection(lambda delay, callback: scheduled.append((delay, callback)))
    value.connect(); transport.callbacks["failed"]("timeout"); transport.callbacks["closed"]()
    assert value.state == StreamState.RECONNECTING
    assert len(scheduled) == 1


def test_malformed_message_is_sanitized_and_does_not_storm() -> None:
    scheduled, statuses = [], []
    value, transport = connection(lambda delay, callback: scheduled.append(callback))
    value.add_status_listener(statuses.append); value.connect(); transport.callbacks["opened"]()
    transport.callbacks["message"]("not-json")
    transport.callbacks["closed"]()
    assert len(scheduled) == 1
    assert all("SSSS" not in status.message and "KKKK" not in status.message for status in statuses)


def test_long_trade_session_has_bounded_duplicate_memory_and_runtime() -> None:
    count = 100_000
    engine = CandleAggregator(1, duplicate_window=1000)
    base = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
    tracemalloc.start(); before = tracemalloc.take_snapshot(); started = time.perf_counter()
    for index in range(count):
        engine.process(TradeEvent("mock", "AAPL", Decimal(100 + index % 10), Decimal(1), base + timedelta(milliseconds=index), str(index)))
    elapsed = time.perf_counter() - started; after = tracemalloc.take_snapshot(); tracemalloc.stop()
    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    assert len(engine._seen) == 1000
    assert growth < 20_000_000
    assert elapsed < 15


def test_catalyst_burst_dedupe_and_alert_memory_remain_bounded() -> None:
    dedupe = EventDeduplicator(capacity=500)
    base = datetime(2026, 8, 17, tzinfo=timezone.utc)
    for index in range(5000):
        assert dedupe.accept(normalize_event("SEC", f"https://www.sec.gov/{index}", base, f"Filing {index}", received_at=base))
    assert len(dedupe._seen) == 500
    now = [base]; dispatcher = AlertDispatcher(AlertPreferences(duplicate_cooldown_seconds=1), clock=lambda: now[0])
    for index in range(5000):
        dispatcher.dispatch(AlertNotification(str(index), AlertType.SEC_FILING, "SEC", "filing", None, now[0])); now[0] += timedelta(seconds=2)
    assert len(dispatcher._sent) <= 1801


def test_long_watchlist_enforces_subscription_limit_without_request_storm() -> None:
    book = SubscriptionBook(limit=30)
    book.subscribe({f"S{index}" for index in range(30)})
    try:
        book.subscribe({"OVER"})
    except ValueError as exc:
        assert "at most 30" in str(exc)
    assert len(book.symbols) == 30


def test_missing_credentials_invalid_credentials_and_no_mock_fallback() -> None:
    store = InMemoryCredentialStore()
    assert store.load("alpaca") is None
    store.save(ProviderCredentials("alpaca", {"key_id": "invalid-key", "secret_key": "invalid-secret"}))
    with pytest.raises(ValueError):
        store.load("mock")
    assert store.load("alpaca").provider_id == "alpaca"


def test_scanner_burst_is_linear_and_scoped() -> None:
    observations = [ScannerObservation(f"S{index}", Decimal(10), rvol=Decimal(3)) for index in range(5000)]
    allowed = {f"S{index}" for index in range(100)}
    started = time.perf_counter(); hits = scan_observations(observations, allowed); elapsed = time.perf_counter() - started
    assert len(hits) == 100
    assert elapsed < 3
