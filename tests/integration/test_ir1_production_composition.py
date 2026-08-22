from __future__ import annotations

import json
import importlib
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.catalyst_runtime import CatalystSource
from app.catalysts.feeds.congress import parse_bills
from app.catalysts.feeds.nasdaq_halts import parse_halt_rss
from app.catalysts.feeds.sec import parse_submissions
from app.catalysts.feeds.white_house import parse_feed
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials
from app.streaming.events import StreamState

try:
    from PySide6.QtWidgets import QApplication
except Exception:
    QApplication = None


NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


class Adapter:
    def __init__(self, root: Path):
        self.app_name = "RangeScout"
        self.app_data_dir = self.config_dir = self.temp_dir = str(root)
        self.allow_user_install_paths = []


class ImmediateExecutor:
    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class FakeTransport:
    def __init__(self):
        self.callbacks = {}
        self.sent: list[str] = []
        self.open_count = 0
        self.close_count = 0

    def set_callbacks(self, **callbacks):
        self.callbacks = callbacks

    def open(self):
        self.open_count += 1
        self.callbacks["opened"]()

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.close_count += 1
        self.callbacks["closed"]()

    def message(self, payload):
        self.callbacks["message"](payload)

    def fail(self):
        self.callbacks["failed"]("transport failed with no credential detail")


def _sources() -> list[CatalystSource]:
    sec_payload = {"name": "Apple Inc", "cik": 320193, "filings": {"recent": {"form": ["8-K"], "accessionNumber": ["0000320193-26-000001"], "filingDate": ["2026-08-17"]}}}
    halt_xml = b"<rss><channel><item><title>XYZ Trading Halt</title><link>https://www.nasdaqtrader.com/xyz</link><description>News pending</description><IssueSymbol>XYZ</IssueSymbol></item></channel></rss>"
    white_xml = b"<rss><channel><item><title>Executive Order on Technology</title><link>https://www.whitehouse.gov/a</link><category>Presidential Actions</category></item></channel></rss>"
    congress_payload = {"bills": [{"title": "Technology Act", "url": "https://api.congress.gov/v3/bill/119/hr/1", "updateDate": "2026-08-17T10:00:00Z", "latestAction": {"text": "Signed by President"}}]}
    return [
        CatalystSource("sec", 900, lambda symbols: parse_submissions(sec_payload, "AAPL", NOW)),
        CatalystSource("nasdaq", 60, lambda symbols: parse_halt_rss(halt_xml, NOW)),
        CatalystSource("white_house", 900, lambda symbols: parse_feed(white_xml, NOW)),
        CatalystSource("congress", 900, lambda symbols: parse_bills(congress_payload, NOW)),
    ]


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_real_window_composition_streams_scans_alerts_polls_and_shuts_down(monkeypatch, tmp_path) -> None:
    root = tmp_path / "RangeScout"
    root.mkdir()
    (root / "settings.json").write_text(json.dumps({"default_provider": "mock", "provider_policy_version": 3}), encoding="utf-8")
    adapter = Adapter(root)
    ui_module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(ui_module, "platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    qt = QApplication.instance() or QApplication([])
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("finnhub", {"api_key": "K" * 24}))
    credentials.save(ProviderCredentials("congress", {"api_key": "C" * 24}))
    transports: list[FakeTransport] = []
    scheduled: list[tuple[float, object]] = []

    def transport_factory(provider, supplied_credentials):
        assert provider == "finnhub" and supplied_credentials.provider_id == "finnhub"
        value = FakeTransport()
        transports.append(value)
        return value

    window = ui_module.build_window(
        credential_store=credentials,
        runtime_transport_factory=transport_factory,
        catalyst_sources=_sources(),
        runtime_executor=ImmediateExecutor(),
        runtime_schedule=lambda delay, callback: scheduled.append((delay, callback)),
        runtime_post=lambda callback: callback(),
    )
    window.live_refresh_timer.stop()
    try:
        window.provider_combo.setCurrentIndex(window.provider_combo.findData("finnhub"))
        window.runtime.live.update_snapshot("AAPL", Decimal("100"), Decimal("99"), NOW)
        transport = transports[-1]
        assert window.runtime.live.connection is not None
        assert window.runtime.live.connection.state == StreamState.CONNECTED
        assert window.live_stream_status_text.text() == "CONNECTED"
        assert window.runtime.live.connection.symbols == ("AAPL",)

        for index in range(23):
            timestamp = int((NOW + timedelta(seconds=index)).timestamp() * 1000)
            size = 500 if index == 22 else 10
            transport.message(json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": 100 + index, "v": size, "t": timestamp}]}))
        qt.processEvents()
        assert window.live_price_text.text() == "122"
        assert window.live_trade_time_text.text().startswith("2026-08-17")
        assert "VWAP" in window.live_indicators_text.text()
        assert window._ticker_identity_labels["AAPL"].text() == "AAPL"
        assert "122" in window._ticker_value_labels["AAPL"].text()
        assert any("unusual_volume" in window.scanner_results.item(i).text() for i in range(window.scanner_results.count()))
        assert not any("Volume Spike" in window.alert_list.item(i).text() for i in range(window.alert_list.count()))
        assert any("Volume Spike" in window.alert_history_list.item(i).text() for i in range(window.alert_history_list.count()))

        window.live_candle_interval.setCurrentIndex(window.live_candle_interval.findData(5))
        assert window.runtime.live._aggregator.interval_seconds == 5
        assert window.runtime.live.states["AAPL"].completed_candles == ()

        window.runtime.catalysts.poll_due(force=True)
        qt.processEvents()
        catalyst_text = "\n".join(window.catalyst_list.item(i).text() for i in range(window.catalyst_list.count()))
        assert "AAPL" in catalyst_text and "Executive Order on Technology" in catalyst_text
        assert "XYZ" not in catalyst_text
        assert window.runtime.catalysts.event_count == 4
        stored = (root / "catalysts.json").read_text(encoding="utf-8")
        assert "0000320193-26-000001" in stored and "article body" not in stored

        transport.fail()
        transport.callbacks["closed"]()
        assert len(scheduled) == 1
        assert window.live_stream_status_text.text() == "RECONNECTING"
        scheduled[0][1]()
        assert window.runtime.live.connection.state == StreamState.CONNECTED
        assert window.runtime.live.connection.symbols == ("AAPL",)

        open_count = transport.open_count
        window._qt_window.close()
        scheduled[0][1]()
        qt.processEvents()
        assert transport.open_count == open_count
        assert window.runtime.catalysts.in_flight_count == 0
    finally:
        window.app.store.close()
        window._qt_window.close()


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_real_composition_routes_exact_ohlcv_candles_to_live_chart(monkeypatch, tmp_path) -> None:
    root = tmp_path / "RangeScout"
    root.mkdir()
    adapter = Adapter(root)
    ui_module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(ui_module, "platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    qt = QApplication.instance() or QApplication([])
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("finnhub", {"api_key": "K" * 24}))
    transports: list[FakeTransport] = []

    def transport_factory(provider, supplied_credentials):
        assert provider == "finnhub" and supplied_credentials.provider_id == "finnhub"
        value = FakeTransport()
        transports.append(value)
        return value

    window = ui_module.build_window(
        credential_store=credentials,
        runtime_transport_factory=transport_factory,
        catalyst_sources=[],
        runtime_executor=ImmediateExecutor(),
        runtime_schedule=lambda delay, callback: None,
        runtime_post=lambda callback: callback(),
        auto_refresh=False,
    )
    window.live_refresh_timer.stop()
    try:
        window.provider_combo.setCurrentIndex(window.provider_combo.findData("finnhub"))
        window.live_candle_interval.setCurrentIndex(window.live_candle_interval.findData(5))
        transport = transports[-1]

        def send(offset_ms: int, price: int, size: int) -> None:
            timestamp = int((NOW + timedelta(milliseconds=offset_ms)).timestamp() * 1000)
            transport.message(json.dumps({"type": "trade", "data": [{"s": "AAPL", "p": price, "v": size, "t": timestamp}]}))

        send(0, 100, 2)
        send(100, 103, 3)
        send(200, 99, 4)
        send(300, 102, 5)
        send(5000, 104, 6)
        first = window.runtime.live.states["AAPL"]
        completed = first.completed_candles[0]
        assert (completed.open, completed.high, completed.low, completed.close, completed.volume, completed.trade_count) == (
            Decimal("100"), Decimal("103"), Decimal("99"), Decimal("102"), Decimal("14"), 4
        )
        assert first.current_candle is not None
        assert (first.current_candle.open, first.current_candle.high, first.current_candle.low, first.current_candle.close, first.current_candle.volume) == (
            Decimal("104"), Decimal("104"), Decimal("104"), Decimal("104"), Decimal("6")
        )

        send(10000, 105, 1)
        send(10100, 101, 2)
        send(15000, 106, 1)
        qt.processEvents()
        state = window.runtime.live.states["AAPL"]
        assert state.completed_candles[0] == completed
        assert (state.completed_candles[2].open, state.completed_candles[2].close) == (Decimal("105"), Decimal("101"))
        assert window.live_chart.display_mode == ui_module.MiniLineChart.CANDLESTICK_MODE
        assert window.live_chart._opens == [100.0, 104.0, 105.0, 106.0]
        assert window.live_chart._highs == [103.0, 104.0, 105.0, 106.0]
        assert window.live_chart._lows == [99.0, 104.0, 101.0, 106.0]
        assert window.live_chart._closes == [102.0, 104.0, 101.0, 106.0]
        assert window.live_chart._volumes == [14.0, 6.0, 3.0, 1.0]
        window.live_chart.resize(700, 350)
        window.live_chart.grab()
        assert "up" in window.live_chart.last_rendered_candle_directions
        assert "down" in window.live_chart.last_rendered_candle_directions

        frozen_state = window.runtime.live.states["AAPL"]
        send(15000, 106, 1)
        send(9000, 500, 1)
        assert window.runtime.live.states["AAPL"] == frozen_state
        window.live_candle_interval.setCurrentIndex(window.live_candle_interval.findData(15))
        assert window.runtime.live._aggregator.interval_seconds == 15
        assert window.runtime.live._aggregator.current("AAPL") is None
    finally:
        window.app.store.close()
        window._qt_window.close()
