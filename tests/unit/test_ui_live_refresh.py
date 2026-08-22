from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.ui.main import build_window
from app.application.bootstrap import RangeScoutApplication
from app.security.credentials import InMemoryCredentialStore
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    QApplication = None  # type: ignore[assignment]


class _WritableAdapter:
    def __init__(self, root: Path) -> None:
        self.app_name = "RangeScout"
        self.app_data_dir = str(root)
        self.config_dir = str(root)
        self.temp_dir = str(root)
        self.allow_user_install_paths = []


class _ImmediatePool:
    def start(self, task: object) -> None:
        task.run()  # type: ignore[attr-defined]


class _ImmediateThreadPool:
    @staticmethod
    def globalInstance() -> _ImmediatePool:
        return _ImmediatePool()


class _HoldingPool:
    def __init__(self) -> None:
        self.tasks: list[object] = []

    def start(self, task: object) -> None:
        self.tasks.append(task)


@pytest.fixture
def live_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if QApplication is None:
        pytest.skip("PySide6 is not installed")
    adapter = _WritableAdapter(tmp_path / "RangeScout")
    Path(adapter.app_data_dir).mkdir(parents=True)
    (Path(adapter.app_data_dir) / "settings.json").write_text(
        json.dumps(
            {
                "default_provider": "mock",
                "provider_policy_version": 2,
                "live_refresh_interval_ms": 10000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.platform.platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    main_module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(main_module, "platform_adapter", lambda: adapter)
    QApplication.instance() or QApplication([])
    store = InMemoryCredentialStore()
    application = RangeScoutApplication(
        data_dir=Path(adapter.app_data_dir), credential_store=store, registry=build_test_provider_registry(store)
    )
    window = build_window(application=application, auto_refresh=False)
    initial = window.market_data.fetch_quote("AAPL")
    window._last_quote_provider_id = initial.metadata.provider_id
    window._apply_quote_success(initial.payload, refresh_collections=False)
    window.live_refresh_timer.stop()
    try:
        yield window, adapter, main_module
    finally:
        window.live_refresh_timer.stop()
        window.app.store.close()
        window._qt_window.close()


@pytest.mark.parametrize("interval_ms", [500, 1000, 10000, 30000])
def test_refresh_setting_persists_and_reconfigures_timer_immediately(
    live_window: tuple[object, _WritableAdapter, object],
    interval_ms: int,
) -> None:
    window, adapter, _main_module = live_window
    window.refresh_interval_combo.setCurrentIndex(window.refresh_interval_combo.findData(interval_ms))
    persisted = json.loads((Path(adapter.app_data_dir) / "settings.json").read_text(encoding="utf-8"))
    assert window.live_refresh_timer.interval() == interval_ms
    assert persisted["live_refresh_interval_ms"] == interval_ms


def test_500ms_auto_refresh_fetches_only_active_quote_not_history_or_watchlist(
    monkeypatch: pytest.MonkeyPatch,
    live_window: tuple[object, _WritableAdapter, object],
) -> None:
    window, _adapter, main_module = live_window
    counts = {"quote": 0, "historical": 0}
    original_quote = window.market_data.fetch_quote

    def fetch_quote(symbol: str):
        counts["quote"] += 1
        assert symbol == window.current_symbol
        return original_quote(symbol)

    def fetch_historical(*_args: object, **_kwargs: object):
        counts["historical"] += 1
        raise AssertionError("auto-refresh must not fetch historical bars")

    monkeypatch.setattr(window.market_data, "fetch_quote", fetch_quote)
    monkeypatch.setattr(window.market_data, "fetch_historical", fetch_historical)
    monkeypatch.setattr(
        window.watchlist_store,
        "list",
        lambda: (_ for _ in ()).throw(AssertionError("auto-refresh must not iterate watchlists")),
    )
    monkeypatch.setattr(main_module, "QThreadPool", _ImmediateThreadPool)
    window.refresh_interval_combo.setCurrentIndex(window.refresh_interval_combo.findData(500))
    window.last_updated_text.setText("Last Updated: -- ET")
    window._on_live_refresh_tick()
    assert counts == {"quote": 1, "historical": 0}
    assert window.last_updated_text.text() != "Last Updated: -- ET"
    assert "network-backed" in window.status_text.text()


def test_auto_refresh_skips_tick_while_request_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    live_window: tuple[object, _WritableAdapter, object],
) -> None:
    window, _adapter, main_module = live_window
    pool = _HoldingPool()

    class HoldingThreadPool:
        @staticmethod
        def globalInstance() -> _HoldingPool:
            return pool

    monkeypatch.setattr(main_module, "QThreadPool", HoldingThreadPool)
    window._request_active_quote_refresh()
    window._request_active_quote_refresh()
    assert len(pool.tasks) == 1
    assert window._quote_refresh_in_flight is True


def test_transient_failure_retains_last_successful_quote_without_modal_error(
    monkeypatch: pytest.MonkeyPatch,
    live_window: tuple[object, _WritableAdapter, object],
) -> None:
    window, _adapter, main_module = live_window
    last_quote = window.current_quote
    last_price = window.price_text.text()
    last_updated = window.last_updated_text.text()

    def fail_quote(_symbol: str):
        raise OSError("temporary network failure")

    monkeypatch.setattr(window.market_data, "fetch_quote", fail_quote)
    monkeypatch.setattr(main_module, "QThreadPool", _ImmediateThreadPool)
    window._quote_tasks.clear()
    window._quote_refresh_in_flight = False
    window._request_active_quote_refresh()
    assert window.current_quote is last_quote
    assert window.price_text.text() == last_price
    assert window.last_updated_text.text() == last_updated
    assert "retaining the last successful" in window.result_text.text()


def test_late_quote_for_previous_active_symbol_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
    live_window: tuple[object, _WritableAdapter, object],
) -> None:
    window, _adapter, main_module = live_window
    pool = _HoldingPool()

    class HoldingThreadPool:
        @staticmethod
        def globalInstance() -> _HoldingPool:
            return pool

    monkeypatch.setattr(main_module, "QThreadPool", HoldingThreadPool)
    window._quote_tasks.clear()
    window._quote_refresh_in_flight = False
    window._request_active_quote_refresh()
    assert len(pool.tasks) == 1
    window.set_active_symbol("MSFT", source="test-race")
    assert window.current_quote is None
    assert "Loading MSFT" in window.price_text.text()
    pool.tasks[0].run()
    assert window.current_symbol == "MSFT"
    assert window.current_quote is None
    assert "AAPL" not in window.price_text.text()
