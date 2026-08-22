from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.security.credentials import InMemoryCredentialStore
from app.research.models import CompanyProfile, ResearchSnapshot
from datetime import datetime, timezone
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtWidgets import QApplication, QWidget
except Exception:
    QApplication = QWidget = None


class Adapter:
    def __init__(self, root: Path) -> None:
        self.app_name = "RangeScout"
        self.app_data_dir = self.config_dir = self.temp_dir = str(root)
        self.allow_user_install_paths = []


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if QApplication is None:
        pytest.skip("PySide6 is not installed")
    root = tmp_path / "RangeScout"
    root.mkdir()
    adapter = Adapter(root)
    module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(module, "platform_adapter", lambda: adapter)
    store = InMemoryCredentialStore()
    app = RangeScoutApplication(data_dir=root, credential_store=store, registry=build_test_provider_registry(store))
    QApplication.instance() or QApplication([])
    value = module.build_window(application=app, auto_refresh=False)
    value.live_refresh_timer.stop()
    try:
        yield value
    finally:
        value.app.store.close()
        value._qt_window.close()


def test_main_navigation_and_research_subtabs_match_authoritative_order(window) -> None:
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        "Market", "Live Trader", "Research", "Watchlists", "Scanner", "Alerts", "Notes", "Exports", "Settings"
    ]
    assert [window.research_tabs.tabText(index) for index in range(window.research_tabs.count())] == [
        "Overview", "Valuation", "Earnings", "Growth", "Financials", "Financial Health",
        "Performance", "Peers", "Analyst Outlook", "Catalysts & News",
    ]
    assert [surface.name for surface in window.build_window_surfaces()] == [
        "market", "live-trader", "research", "watchlists", "scanner", "alerts", "notes", "exports", "settings"
    ]


def test_all_primary_routes_remain_in_the_single_main_window(window) -> None:
    assert window.tabs.count() == 9
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        assert window.tabs.widget(index).window() is window._qt_window
    assert QApplication.instance() is not None


def test_active_symbol_updates_every_dependent_surface(window) -> None:
    window.set_active_symbol("msft", source="watchlist")
    assert window.current_symbol == "MSFT"
    assert window.active_symbol_title.text() == "MSFT"
    assert window.market_symbol_input.text() == "MSFT"
    assert window.chart_symbol_input.text() == "MSFT"
    assert window.notes_symbol_input.text() == "MSFT"
    assert window.alert_symbol_input.text() == "MSFT"
    assert window.compare_symbol_input.text() == "MSFT"
    assert window.live_symbol_text.text() == "MSFT"
    assert "watchlist" in window.active_symbol_context.text()


def test_curated_peer_click_changes_entire_active_symbol_context(window) -> None:
    window.set_active_symbol("BA", source="global-search")
    assert window.peer_list.item(0).text() == "LMT"
    window._on_peer_activate(window.peer_list.item(0))
    assert window.current_symbol == "LMT"
    assert window.market_symbol_input.text() == "LMT"
    assert window.live_symbol_text.text() == "LMT"
    assert "peer" in window.active_symbol_context.text()


def test_no_public_mock_provider_controls_are_present(window) -> None:
    provider_ids = [window.provider_combo.itemText(index).lower() for index in range(window.provider_combo.count())]
    settings_ids = [str(window.provider_settings_selector.itemData(index)).lower() for index in range(window.provider_settings_selector.count())]
    assert provider_ids == ["yahoo", "finnhub"]
    assert settings_ids == ["yahoo", "finnhub"]
    visible_text = []
    for child in window._qt_window.findChildren(QWidget):
        getter = getattr(child, "text", None)
        if callable(getter):
            visible_text.append(str(getter()))
    assert "mock" not in "\n".join(visible_text).lower()


def test_late_research_response_for_previous_symbol_is_discarded(window, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = []

    class HoldingPool:
        def start(self, task):
            tasks.append(task)

    class HoldingThreadPool:
        @staticmethod
        def globalInstance():
            return HoldingPool()

    class Research:
        def load(self, symbol, generation, period_mode):
            assert period_mode == "annual"
            profile = CompanyProfile(symbol, "0000000001", f"{symbol} Corp", "NYSE", "1", "Industry")
            return ResearchSnapshot(symbol, generation, profile, {}, datetime.now(timezone.utc))

    module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(module, "QThreadPool", HoldingThreadPool)
    window.research_service = Research()
    window._on_research_refresh()
    window.set_active_symbol("NVDA", source="ticker")
    tasks[0].run()
    assert window.current_symbol == "NVDA"
    assert window.current_research_snapshot is None
