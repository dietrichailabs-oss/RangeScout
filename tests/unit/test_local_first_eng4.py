from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from contextlib import suppress
from threading import Event, get_ident
from time import perf_counter, sleep
import ast
import inspect
import json

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.models.schemas import (
    AssetType, DataDelay, Instrument, InstrumentIdentifier, OhlcvBar, ProviderMetadata,
    QuoteSnapshot,
)
from app.providers.base import ProviderResult
from app.security.credentials import InMemoryCredentialStore
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtCore import QRunnable, QThreadPool
    from PySide6.QtWidgets import QApplication
except Exception:
    QApplication = QRunnable = QThreadPool = None


def quote(symbol: str = "AAPL", price: str = "190.25") -> QuoteSnapshot:
    return QuoteSnapshot(
        Instrument(InstrumentIdentifier(symbol, "NASDAQ"), "Apple Inc." if symbol == "AAPL" else symbol,
                   AssetType.STOCK, provider="deterministic"),
        Decimal(price), Decimal("188.00"), 123456, datetime.now(timezone.utc), datetime.now(timezone.utc),
        DataDelay.DELAYED, 1,
    )


def bars(symbol: str = "AAPL") -> list[OhlcvBar]:
    return [
        OhlcvBar(InstrumentIdentifier(symbol, "NASDAQ"), date(2026, 8, 19), Decimal("185"), Decimal("190"), Decimal("184"), Decimal("188"), 100, "deterministic"),
        OhlcvBar(InstrumentIdentifier(symbol, "NASDAQ"), date(2026, 8, 20), Decimal("188"), Decimal("192"), Decimal("187"), Decimal("190.25"), 120, "deterministic"),
    ]


class HostileMarketData:
    provider_id = "hostile-fake"

    def __init__(
        self,
        *,
        hang_history: Event | None = None,
        hang_quote: Event | None = None,
        fail_quote: bool = False,
    ) -> None:
        self.hang_history = hang_history
        self.hang_quote = hang_quote
        self.fail_quote = fail_quote
        self.quote_calls = 0
        self.history_calls = 0
        self.thread_ids: list[int] = []
        self.history_started = Event()
        self.history_finished = Event()
        self.quote_started = Event()

    @staticmethod
    def resolve_instrument(symbol: str) -> Instrument:
        return Instrument(InstrumentIdentifier(symbol, "NASDAQ"), symbol, AssetType.STOCK, provider="hostile-fake")

    def fetch_quote(self, symbol: str) -> ProviderResult:
        self.quote_calls += 1
        self.thread_ids.append(get_ident())
        self.quote_started.set()
        if self.hang_quote is not None:
            self.hang_quote.wait(timeout=30)
        if self.fail_quote:
            raise OSError("all providers blocked")
        return ProviderResult("quote", quote(symbol), datetime.now(timezone.utc), ProviderMetadata("fast", "Fast fake"))

    def fetch_historical(self, identifier, start=None, end=None):  # noqa: ARG002
        self.history_calls += 1
        self.thread_ids.append(get_ident())
        self.history_started.set()
        if self.hang_history is not None:
            self.hang_history.wait(timeout=30)
        self.history_finished.set()
        return ProviderResult("historical", (bars(identifier.symbol), []), datetime.now(timezone.utc), ProviderMetadata("history", "History fake"))


@pytest.fixture
def local_app(tmp_path: Path):
    credentials = InMemoryCredentialStore()
    app = RangeScoutApplication(
        data_dir=tmp_path / "RangeScout", credential_store=credentials,
        registry=build_test_provider_registry(credentials),
    )
    app.start_background_services = lambda: None
    yield app
    with suppress(Exception):
        app.shutdown()


def test_preseeded_company_master_is_offline_searchable_and_additive(local_app) -> None:
    snapshot = local_app.local_snapshots.load("AAPL")
    assert snapshot.identity.security_name.startswith("Apple Inc.")
    assert snapshot.identity.mic_code == "XNAS"
    assert snapshot.query_count == 3 and snapshot.elapsed_ms < 250
    assert "AAPL" in {row["canonical_symbol"] for row in local_app.search_instruments("Apple")}
    assert local_app.company_master_report.available >= 15_000
    # Re-provisioning is idempotent and never overwrites the user database.
    from app.company_data.master import provision_company_master
    report = provision_company_master(local_app.store.path)
    assert report.already_current and report.added == 0


def test_last_known_quote_round_trip_is_explicitly_cached(local_app) -> None:
    local_app.local_snapshots.save_quote(quote(), "deterministic")
    snapshot = local_app.local_snapshots.load("AAPL")
    assert snapshot.quote is not None and snapshot.quote.last == Decimal("190.25")
    assert snapshot.quote.previous_close == Decimal("188.00")
    assert snapshot.quote.freshness.value == "cached"
    assert snapshot.quote_provider_id == "deterministic"


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_startup_and_provider_calls_never_block_qt_thread(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    fake = HostileMarketData(fail_quote=True)
    local_app.market_data_service = fake
    from app.ui.main import build_window

    began = perf_counter()
    window = build_window(application=local_app, auto_refresh=True)
    elapsed = perf_counter() - began
    window.live_refresh_timer.stop()
    try:
        assert elapsed < 1.0
        deadline = perf_counter() + 1.0
        while not fake.thread_ids and perf_counter() < deadline:
            qt.processEvents(); sleep(0.005)
        assert fake.thread_ids and all(thread_id != get_ident() for thread_id in fake.thread_ids)
        assert window.performance_diagnostics()["startup_interactive_ms"] < 1000
    finally:
        window._shutdown_runtime(); window._qt_window.close()
        QThreadPool.globalInstance().waitForDone(2000)


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_quote_renders_while_advertised_thirty_second_history_is_hung(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    release = Event()
    fake = HostileMarketData(hang_history=release)
    local_app.market_data_service = fake
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    window._auto_network_refresh = True
    try:
        began = perf_counter()
        window._request_active_history_refresh(force=True)
        assert fake.history_started.wait(timeout=1)
        window._request_active_quote_refresh()
        deadline = perf_counter() + 1.0
        while window.current_quote is None and perf_counter() < deadline:
            qt.processEvents(); sleep(0.005)
        assert window.current_quote is not None
        assert perf_counter() - began < 1.0
        assert not fake.history_finished.is_set()
        assert "190.25" in window.price_text.text()
    finally:
        release.set()
        deadline = perf_counter() + 1
        while not fake.history_finished.is_set() and perf_counter() < deadline:
            qt.processEvents(); sleep(0.005)
        window._shutdown_runtime(); window._qt_window.close()
        QThreadPool.globalInstance().waitForDone(2000)


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_new_symbol_quote_bypasses_twenty_hung_history_and_slow_logo_research_jobs(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    release = Event()
    began_jobs = Event()
    global_pool = QThreadPool.globalInstance()
    original_limit = global_pool.maxThreadCount()
    global_pool.setMaxThreadCount(4)

    class BlockingBackgroundJob(QRunnable):
        def __init__(self, kind: str) -> None:
            super().__init__()
            self.kind = kind

        def run(self) -> None:
            began_jobs.set()
            release.wait(timeout=30)

    jobs = (
        [BlockingBackgroundJob("history") for _ in range(20)]
        + [BlockingBackgroundJob("logo") for _ in range(4)]
        + [BlockingBackgroundJob("research") for _ in range(4)]
    )
    for job in jobs:
        global_pool.start(job)
    assert began_jobs.wait(timeout=1)

    fake = HostileMarketData()
    local_app.market_data_service = fake
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    window._auto_network_refresh = True
    try:
        began = perf_counter()
        window.set_active_symbol("NVDA", source="priority-acceptance")
        assert fake.quote_started.wait(timeout=0.5)
        while window.current_quote is None and perf_counter() - began < 4.0:
            qt.processEvents(); sleep(0.005)
        elapsed = perf_counter() - began
        diagnostics = window.performance_diagnostics()
        print(json.dumps({
            "scenario": "20 history + 4 logo + 4 research jobs",
            "quote_dispatch_delay_ms": round(float(diagnostics["quote_dispatch_delay_ms"]), 3),
            "quote_wall_clock_ms": round(elapsed * 1000.0, 3),
        }, sort_keys=True))
        assert window.current_quote is not None
        assert window.current_quote.instrument.identifier.symbol == "NVDA"
        assert elapsed <= 4.0
        assert diagnostics["quote_dispatch_delay_ms"] < 500
        assert not release.is_set()
    finally:
        release.set()
        window._shutdown_runtime(); window._qt_window.close()
        global_pool.waitForDone(3000)
        global_pool.setMaxThreadCount(original_limit)


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_new_symbol_reaches_truthful_quote_timeout_within_four_seconds(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    release_quote = Event()
    fake = HostileMarketData(hang_quote=release_quote)
    local_app.market_data_service = fake
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    window._auto_network_refresh = True
    try:
        began = perf_counter()
        window.set_active_symbol("NVDA", source="timeout-acceptance")
        assert fake.quote_started.wait(timeout=0.5)
        while "within 4 seconds" not in window.result_text.text() and perf_counter() - began < 4.0:
            qt.processEvents(); sleep(0.005)
        elapsed = perf_counter() - began
        assert elapsed <= 4.0
        assert "No fresh NVDA quote arrived within 4 seconds" in window.result_text.text()
        assert "Quote timed out" in window.price_text.text()
        assert window.performance_diagnostics()["quote_deadline_outcome"] == "timeout"
    finally:
        release_quote.set()
        deadline = perf_counter() + 1.0
        while window._quote_tasks and perf_counter() < deadline:
            qt.processEvents(); sleep(0.005)
        window._shutdown_runtime(); window._qt_window.close()


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_twenty_rapid_symbol_changes_coalesce_to_latest_quote(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    fake = HostileMarketData()
    local_app.market_data_service = fake
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    window._auto_network_refresh = True
    symbols = [f"Q{index}" for index in range(19)] + ["NVDA"]
    try:
        began = perf_counter()
        for symbol in symbols:
            window.set_active_symbol(symbol, source="rapid-switch")
        while (window.current_quote is None or window.current_quote.instrument.identifier.symbol != "NVDA") and perf_counter() - began < 4:
            qt.processEvents(); sleep(0.005)
        assert window.current_quote is not None
        assert window.current_quote.instrument.identifier.symbol == "NVDA"
        assert fake.quote_calls <= 2
        assert window.performance_diagnostics()["quote_dispatch_delay_ms"] < 500
        assert perf_counter() - began <= 4
    finally:
        window._shutdown_runtime(); window._qt_window.close()
        QThreadPool.globalInstance().waitForDone(2000)


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_duplicate_requests_are_deduplicated_and_research_hidden_does_not_load_analyst(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    release = Event()
    fake = HostileMarketData(hang_history=release)
    local_app.market_data_service = fake
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    try:
        window._request_active_history_refresh(force=True)
        window._request_active_history_refresh(force=True)
        assert fake.history_started.wait(timeout=1)
        assert fake.history_calls == 1
        before = len(window._analyst_tasks)
        window.tabs.setCurrentIndex(0)
        window.set_active_symbol("NVDA", source="test")
        qt.processEvents()
        assert len(window._analyst_tasks) == before == 0
    finally:
        release.set(); window._shutdown_runtime(); window._qt_window.close()
        QThreadPool.globalInstance().waitForDone(2000)


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_offline_cached_quote_is_useful_and_truthfully_labeled(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    local_app.local_snapshots.save_quote(quote("BA", "215.10"), "cached-provider")
    fake = HostileMarketData(fail_quote=True)
    local_app.market_data_service = fake
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    try:
        began = perf_counter()
        window.set_active_symbol("BA", source="offline-test")
        assert perf_counter() - began < 1.0
        assert window.current_quote is not None and window.current_quote.last == Decimal("215.10")
        assert "Cached" in window.shell_freshness_text.text()
        window._request_active_quote_refresh()
        deadline = perf_counter() + 1
        while not window.offline_banner.isVisible() and perf_counter() < deadline:
            qt.processEvents(); sleep(0.005)
        assert not window.offline_banner.isHidden() and "Offline" in window.offline_banner.text()
        assert window.current_quote.last == Decimal("215.10")
    finally:
        window._shutdown_runtime(); window._qt_window.close()
        QThreadPool.globalInstance().waitForDone(2000)


def test_local_snapshot_indexes_cover_hot_paths(local_app) -> None:
    report = local_app.local_snapshots.index_report()
    required = {"idx_rs_instruments_symbol", "idx_rs_alias_lookup", "idx_rs_last_quotes_received"}
    assert required.issubset(set(report["indexes"]))
    assert any("sqlite_autoindex_ohlcv_bars_1" in str(row) for row in report["bars_lookup_plan"])
    assert report["wal"] is True


def test_ui_action_handlers_contain_no_direct_provider_network_calls() -> None:
    from app.ui.main import RangeScoutWindow

    tree = ast.parse(inspect.getsource(__import__("app.ui.main", fromlist=["x"])))
    forbidden = {"fetch_quote", "fetch_historical", "get_json", "urlopen", "request"}
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (node.name.startswith("_on_") or node.name in {"set_active_symbol", "show"}):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in forbidden:
                violations.append(f"{node.name}:{child.func.attr}")
    assert violations == []
    assert RangeScoutWindow is not None


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_symbol_selection_does_not_refresh_company_directory_or_market_calendar_network(local_app) -> None:
    qt = QApplication.instance() or QApplication([])
    calls = []
    local_app.refresh_instrument_discovery = lambda: calls.append("discovery")
    from app.ui.main import build_window

    window = build_window(application=local_app, auto_refresh=False)
    window.live_refresh_timer.stop()
    try:
        window.set_active_symbol("MSFT", source="local-search")
        window._update_market_status()
        qt.processEvents()
        assert calls == []
        assert "Microsoft Corporation" in window.market_company_text.text()
        assert window.market_status_text.text().startswith("MARKET ")
    finally:
        window._shutdown_runtime(); window._qt_window.close()
