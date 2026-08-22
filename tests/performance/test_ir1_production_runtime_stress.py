from __future__ import annotations

import json
import time
import tracemalloc
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.application.catalyst_runtime import CatalystSource
from app.application.runtime_coordinator import RuntimeCoordinator
from app.catalysts.normalization import normalize_event
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials


NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


class View:
    def __init__(self):
        self.status = None
        self.live = None
        self.ticker = None
        self.hits = []
        self.alert = None
        self.catalysts = []

    def runtime_stream_status(self, status, display): self.status = (status, display)
    def runtime_live_state(self, state): self.live = state
    def runtime_ticker_state(self, states, plan): self.ticker = (states, plan)
    def runtime_scanner_hits(self, hits): self.hits = hits
    def runtime_alert_notification(self, notification): self.alert = notification
    def set_catalyst_events(self, events): self.catalysts = events


class Transport:
    def __init__(self): self.callbacks = {}; self.sent = []; self.opens = 0; self.closes = 0
    def set_callbacks(self, **callbacks): self.callbacks = callbacks
    def open(self): self.opens += 1; self.callbacks["opened"]()
    def send(self, payload): self.sent.append(payload)
    def close(self): self.closes += 1; self.callbacks["closed"]()
    def message(self, value): self.callbacks["message"](value)
    def fail(self): self.callbacks["failed"]("network unavailable")


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        try: future.set_result(function(*args))
        except Exception as exc: future.set_exception(exc)
        return future


class DeferredExecutor:
    def __init__(self): self.pending = []
    def submit(self, function, *args):
        future = Future(); future.set_running_or_notify_cancel()
        self.pending.append((future, function, args))
        return future
    def complete(self):
        future, function, args = self.pending.pop(0)
        future.set_result(function(*args))


def runtime(tmp_path, *, sources=None):
    store = InMemoryCredentialStore()
    store.save(ProviderCredentials("finnhub", {"api_key": "K" * 20}))
    view, transport, scheduled = View(), Transport(), []
    value = RuntimeCoordinator(
        view,
        store,
        tmp_path,
        lambda provider, credentials: transport,
        lambda delay, callback: scheduled.append((delay, callback)),
        lambda callback: callback(),
        catalyst_sources=sources or [],
        executor=ImmediateExecutor(),
    )
    value.start("finnhub", "AAPL", [])
    value.update_snapshot("AAPL", Decimal("99"), Decimal("98"), NOW)
    return value, view, transport, scheduled


def trade(index: int, *, symbol: str = "AAPL", size: int = 1) -> str:
    timestamp = int((NOW + timedelta(milliseconds=index)).timestamp() * 1000)
    return json.dumps({"type": "trade", "data": [{"s": symbol, "p": 100 + index % 10, "v": size, "t": timestamp}]})


def test_production_coordinator_long_burst_is_bounded_and_responsive(tmp_path) -> None:
    value, view, transport, _ = runtime(tmp_path)
    tracemalloc.start(); before = tracemalloc.take_snapshot(); started = time.perf_counter()
    for index in range(25_000):
        transport.message(trade(index))
    elapsed = time.perf_counter() - started; after = tracemalloc.take_snapshot(); tracemalloc.stop()
    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    state = value.live.states["AAPL"]
    assert len(state.completed_candles) <= 600
    assert len(value.live._aggregator._seen) <= 10_000
    assert growth < 35_000_000
    assert elapsed < 20
    assert view.live.price is not None
    value.shutdown()


def test_repeated_disconnect_has_one_reconnect_and_no_duplicate_subscription(tmp_path) -> None:
    value, _, transport, scheduled = runtime(tmp_path)
    for _ in range(50):
        transport.fail(); transport.callbacks["closed"]()
        assert len(scheduled) == 1
        _, callback = scheduled.pop()
        callback()
        assert value.live.connection.symbols == ("AAPL",)
    assert transport.opens == 51
    value.shutdown()


def test_provider_limit_active_priority_and_overflow_without_http_polling(tmp_path) -> None:
    value, _, _, _ = runtime(tmp_path)
    watchlist = [f"S{index}" for index in range(60)]
    value.set_symbols("ACTIVE", watchlist)
    plan = value.live.subscription_plan
    assert plan.subscribed[0] == "ACTIVE"
    assert len(plan.subscribed) == 50 and len(plan.overflow) == 11
    assert len(value.live.connection.symbols) == 50
    value.shutdown()


def test_duplicate_out_of_order_interval_change_and_shutdown_during_reconnect(tmp_path) -> None:
    value, view, transport, scheduled = runtime(tmp_path)
    transport.message(trade(1000)); transport.message(trade(1000))
    assert value.live.states["AAPL"].current_candle.trade_count == 1
    transport.message(trade(0))
    assert value.live.states["AAPL"].current_candle.trade_count == 1
    value.set_interval(5)
    assert value.live.states["AAPL"].completed_candles == ()
    transport.fail()
    assert len(scheduled) == 1
    opens = transport.opens
    value.shutdown(); scheduled[0][1]()
    assert transport.opens == opens
    assert "KKKK" not in str(view.status)


def test_catalyst_burst_and_feed_failure_remain_bounded_and_retain_prior(tmp_path) -> None:
    batches = [[normalize_event("SEC", f"https://www.sec.gov/{index}", NOW, f"Filing {index}", received_at=NOW) for index in range(1500)]]
    source = CatalystSource("sec", 900, lambda symbols: batches.pop() if batches else (_ for _ in ()).throw(RuntimeError("offline")))
    value, view, _, _ = runtime(tmp_path, sources=[source])
    value.catalysts.poll_due(force=True)
    assert value.catalysts.event_count == 1000
    prior = list(view.catalysts)
    value.catalysts.poll_due(force=True)
    assert view.catalysts == prior
    assert "retained prior events" in value.catalysts.source_status["sec"]
    assert (tmp_path / "catalysts.json").stat().st_size < 2_000_000
    value.shutdown()


def test_missing_credentials_does_not_connect_or_schedule_reconnect(tmp_path) -> None:
    view, transport, scheduled = View(), Transport(), []
    value = RuntimeCoordinator(view, InMemoryCredentialStore(), tmp_path, lambda p, c: transport, lambda d, c: scheduled.append((d, c)), lambda c: c(), catalyst_sources=[], executor=ImmediateExecutor())
    value.start("finnhub", "AAPL", [])
    assert transport.opens == 0 and scheduled == []
    assert view.status[1] == "CREDENTIALS REQUIRED"
    value.shutdown()


def test_shutdown_during_feed_worker_ignores_late_result(tmp_path) -> None:
    store = InMemoryCredentialStore(); view = View(); executor = DeferredExecutor()
    event = normalize_event("SEC", "https://www.sec.gov/late", NOW, "Late result", received_at=NOW)
    source = CatalystSource("sec", 900, lambda symbols: [event])
    value = RuntimeCoordinator(view, store, tmp_path, lambda p, c: Transport(), lambda d, c: None, lambda c: c(), catalyst_sources=[source], executor=executor)
    value.start("yahoo", "AAPL", [])
    value.catalysts.poll_due(force=True)
    assert value.catalysts.in_flight_count == 1
    value.shutdown(); executor.complete()
    assert view.catalysts == []
    assert not (tmp_path / "catalysts.json").exists()


def test_stale_stream_schedules_only_one_reconnect_through_coordinator(tmp_path) -> None:
    value, view, transport, scheduled = runtime(tmp_path)
    value.live.connection._last_message_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    value.tick(); value.tick()
    assert len(scheduled) == 1
    assert view.status[1] == "RECONNECTING"
    value.shutdown()
