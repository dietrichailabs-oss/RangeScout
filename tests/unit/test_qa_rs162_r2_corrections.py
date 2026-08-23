from __future__ import annotations

from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.alerts.dispatcher import AlertNotification, AlertType
from app.application.active_symbol import SymbolRequest
from app.application.bootstrap import RangeScoutApplication
from app.application.catalyst_runtime import CatalystRuntime, CatalystSource, build_congress_source
from app.application.live_trading_runtime import LiveSymbolState
from app.catalysts.storage import CatalystStore
from app.catalysts.symbol_mapping import SymbolCatalog
from app.models.schemas import AssetType, DataDelay, Instrument, InstrumentIdentifier, OhlcvBar, QuoteSnapshot
from app.market_calendar.us_equities import NEW_YORK
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials
from app.streaming.ticker import plan_ticker_subscriptions
from app.ui.main import QApplication, build_window


class PendingExecutor:
    def __init__(self) -> None:
        self.futures: list[Future] = []

    def submit(self, _function, *_args):
        future = Future()
        self.futures.append(future)
        return future


def test_congress_source_add_delete_cancels_and_rejects_stale_results(tmp_path) -> None:
    credentials = InMemoryCredentialStore()
    executor = PendingExecutor()
    base = [CatalystSource(name, 60, lambda _symbols: []) for name in ("sec", "nasdaq", "white_house")]
    runtime = CatalystRuntime(
        base, executor, lambda callback: callback(), SymbolCatalog(),
        CatalystStore(tmp_path / "catalysts.json"), lambda _events: None,
        lambda *_args: None, lambda *_args: None,
    )
    runtime.poll_due(force=True)
    assert [source.name for source in runtime.sources] == ["sec", "nasdaq", "white_house"]
    assert len(executor.futures) == 3

    credentials.save(ProviderCredentials("congress", {"api_key": "K" * 24}))
    congress = build_congress_source(credentials, "RangeScout deterministic test qa@example.test")
    assert congress is not None
    runtime.replace_source("congress", congress)
    runtime.poll_due(force=True)
    assert [source.name for source in runtime.sources][-1] == "congress"
    assert len(executor.futures) == 4
    congress_future = executor.futures[-1]

    credentials.delete("congress")
    runtime.replace_source("congress", build_congress_source(credentials, "RangeScout deterministic test qa@example.test"))
    assert congress_future.cancelled()
    assert "congress" not in [source.name for source in runtime.sources]
    assert "congress" not in runtime.source_status
    runtime.poll_due(force=True)
    assert len(executor.futures) == 4
    restarted = build_congress_source(credentials, "RangeScout deterministic test qa@example.test")
    assert restarted is None


@pytest.fixture()
def correction_window(tmp_path):
    if QApplication is None:
        pytest.skip("PySide6 unavailable")
    qt = QApplication.instance() or QApplication([])
    credentials = InMemoryCredentialStore()
    application = RangeScoutApplication(data_dir=tmp_path / "RangeScout", credential_store=credentials)
    window = build_window(application=application, auto_refresh=False, catalyst_sources=[])
    try:
        yield window, qt
    finally:
        window._shutdown_runtime()


def _quote(symbol: str = "AAPL", previous_close: Decimal | None = Decimal("214.1999969482421875")) -> QuoteSnapshot:
    now = datetime.now(timezone.utc)
    return QuoteSnapshot(
        instrument=Instrument(InstrumentIdentifier(symbol, "NASDAQ"), "Apple Inc.", AssetType.STOCK),
        last=Decimal("215.25"), previous_close=previous_close, volume=1000,
        timestamp=now, provider_timestamp=now, delay_label=DataDelay.REALTIME,
        delay_seconds=0, currency="USD",
    )


def test_scanner_all_live_survives_empty_hits_and_adds_watchlist_state(correction_window) -> None:
    window, _qt = correction_window
    window._auto_network_refresh = True
    window.current_bars = []
    window._refresh_scanner_latest_row(_quote())
    assert len(window._scanner_rows) == 1
    window.runtime_scanner_hits([])
    assert len(window._scanner_rows) == 1
    assert window.scanner_total_text.text() == "1"
    assert window.scanner_results.count() == 1
    assert "AAPL" in window.scanner_results.item(0).text()

    window.watchlist_store.create("qa", "QA")
    window.watchlist_store.add_symbol("qa", "TSLA")
    state = LiveSymbolState(
        "TSLA", price=Decimal("220"), previous_close=Decimal("200"),
        last_trade_at=datetime.now(timezone.utc), feed_state="LIVE",
    )
    window.runtime_ticker_state(
        {"TSLA": state}, plan_ticker_subscriptions(("AAPL", "TSLA"), None)
    )
    assert {row.symbol for row in window._scanner_rows} == {"AAPL", "TSLA"}
    assert window.scanner_total_text.text() == "2"
    assert window.current_symbol == "AAPL"


def test_congress_credentials_synchronize_existing_ui_runtime(correction_window) -> None:
    window, _qt = correction_window
    assert "congress" not in [source.name for source in window.runtime.catalysts.sources]
    window.app.provider_configuration.save_credentials("congress", {"api_key": "C" * 24})
    assert "congress" in [source.name for source in window.runtime.catalysts.sources]
    window.app.provider_configuration.delete_credentials("congress")
    assert "congress" not in [source.name for source in window.runtime.catalysts.sources]
    assert "congress" not in window.runtime.catalysts.source_status


def test_automatic_alerts_never_contaminate_user_rules_and_are_humanized(correction_window) -> None:
    window, _qt = correction_window
    original = [window.alert_list.item(index).text() for index in range(window.alert_list.count())]
    now = datetime.now(timezone.utc)
    window.runtime_alert_notification(AlertNotification(
        "gov", AlertType.GOVERNMENT_CATALYST, "Congress.gov", "GOVERNMENT CATALYST", "BA", now
    ))
    window.runtime_alert_notification(AlertNotification(
        "halt", AlertType.TRADE_HALT, "Nasdaq", "RESUMPTION PENDING — BA", "BA", now
    ))
    assert [window.alert_list.item(index).text() for index in range(window.alert_list.count())] == original
    assert any("Government Catalyst" in window.alert_history_list.item(index).text() for index in range(window.alert_history_list.count()))
    assert any("Resumption Pending" in window.market_alert_list.item(index).text() for index in range(window.market_alert_list.count()))
    assert not any("RESUMPTION PENDING" in window.market_alert_list.item(index).text() for index in range(window.market_alert_list.count()))


def test_previous_close_uses_shared_financial_formatter(correction_window, monkeypatch) -> None:
    window, _qt = correction_window
    monkeypatch.setattr(window, "_request_company_logo", lambda *_args, **_kwargs: None)
    raw = Decimal("214.1999969482421875")
    quote = _quote(previous_close=raw)
    window._apply_quote_success(quote, refresh_collections=False)
    assert window.current_quote.previous_close == raw
    assert "$214.20" in window.metrics_text.text()
    assert "214.1999969482421875" not in window.metrics_text.text()


def _bar(symbol: str, day: date, close: int) -> OhlcvBar:
    value = Decimal(close)
    return OhlcvBar(
        InstrumentIdentifier(symbol, "NASDAQ"), day, value, value + 1, value - 1, value,
        1000, "fake",
    )


def test_chart_calendar_coverage_auto_enriches_and_newest_range_wins(correction_window, monkeypatch) -> None:
    window, _qt = correction_window
    window._auto_network_refresh = True
    today = datetime.now(NEW_YORK).date()
    recent = [_bar("AAPL", today - timedelta(days=offset), 100 + offset) for offset in range(30)]
    window.app.store.upsert_bars(recent, "fake")
    scheduled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        window, "_request_active_history_refresh",
        lambda *, force=False: scheduled.append((window._market_range_revision, window.market_days_input.value())),
    )
    window._on_market_range_selected(1095)
    assert scheduled[-1][1] == 1095

    long_history = [_bar("AAPL", today - timedelta(days=offset), 200 + offset) for offset in range(1461)]
    window.app.store.upsert_bars(long_history, "fake")
    window._on_market_range_selected(30)
    assert window.current_bars
    assert all(today - timedelta(days=30) <= bar.date <= today for bar in window.current_bars)
    assert len(window.current_bars) <= 31

    old_revision = window._market_range_revision
    window._on_market_range_selected(1095)
    window._on_market_range_selected(180)
    assert window.market_days_input.value() == 180
    authoritative = list(window.current_bars)
    stale_request = SymbolRequest(
        symbol=window.current_symbol,
        generation=window.active_symbol.state.generation,
        request_id=99999,
        source=f"history-background-refresh:{old_revision}",
        requested_at=datetime.now(timezone.utc),
    )
    window._on_active_history_finished(stale_request, "fake", "Fake", long_history, None)
    assert window.market_days_input.value() == 180
    assert window.current_bars == authoritative
