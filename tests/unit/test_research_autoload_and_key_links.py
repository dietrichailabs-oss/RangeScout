from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.research.analyst.models import AnalystResult, AnalystState
from app.research.models import CompanyProfile, ResearchSnapshot
from app.security.credentials import InMemoryCredentialStore
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtWidgets import QApplication
except Exception:
    QApplication = None


class ResearchFake:
    def load(self, symbol, generation, period_mode):  # noqa: ANN001
        profile = CompanyProfile(symbol, "0000000001", f"{symbol} Corp", "NYSE", "1", "Industry")
        return ResearchSnapshot(symbol, generation, profile, {}, datetime.now(timezone.utc))


class AnalystFake:
    def load(self, symbol, generation, *, force=False):  # noqa: ANN001, ARG002
        return AnalystResult(symbol, generation, {}, {"finnhub": AnalystState.NOT_CONFIGURED, "alpha_vantage": AnalystState.NOT_CONFIGURED}, messages=("Analyst data not configured.",))

    def invalidate_provider(self, provider_id):  # noqa: ANN001, ARG002
        return None


@pytest.fixture
def window(tmp_path: Path):
    if QApplication is None:
        pytest.skip("PySide6 is not installed")
    QApplication.instance() or QApplication([])
    store = InMemoryCredentialStore()
    app = RangeScoutApplication(data_dir=tmp_path / "RangeScout", credential_store=store, registry=build_test_provider_registry(store))
    from app.ui.main import build_window
    value = build_window(application=app, research_service=ResearchFake(), analyst_service=AnalystFake(), auto_refresh=False)
    value.live_refresh_timer.stop()
    try:
        yield value
    finally:
        value._qt_window.close()


def test_enter_symbol_and_period_changes_schedule_automatic_research(window) -> None:
    assert not window._research_debounce_timer.isActive()
    window.tabs.setCurrentWidget(window.research_tab)
    assert window._research_debounce_timer.isActive()
    window._research_debounce_timer.stop()
    window.set_active_symbol("NVDA", source="ticker")
    assert window._research_debounce_timer.isActive()
    assert window.current_research_snapshot is None
    assert "NVDA" in window.research_company_text.text()
    window._research_debounce_timer.stop()
    window.research_period_combo.setCurrentIndex(1)
    assert window._research_debounce_timer.isActive()
    assert window.research_period_combo.currentData() == "quarterly"


def test_hidden_research_marks_dirty_without_starting_analyst_work(window, monkeypatch) -> None:
    starts = []

    class Pool:
        def start(self, task):
            starts.append(task)

    class ThreadPool:
        @staticmethod
        def globalInstance():
            return Pool()

    monkeypatch.setattr("app.ui.main.QThreadPool", ThreadPool)
    window.tabs.setCurrentWidget(window.market_tab)
    window.set_active_symbol("BA", source="watchlist")
    assert window._research_dirty
    assert not window._research_debounce_timer.isActive()
    assert not any(task.__class__.__name__ == "_AnalystTask" for task in starts)
    window.tabs.setCurrentWidget(window.research_tab)
    assert window._research_debounce_timer.isActive()


def test_late_sec_and_analyst_results_cannot_repaint_new_symbol(window, monkeypatch) -> None:
    tasks = []

    class Pool:
        def start(self, task):
            tasks.append(task)

    class ThreadPool:
        @staticmethod
        def globalInstance():
            return Pool()

    monkeypatch.setattr("app.ui.main.QThreadPool", ThreadPool)
    window.tabs.setCurrentWidget(window.research_tab)
    window._research_debounce_timer.stop()
    window._start_research_load()
    assert len(tasks) == 2
    window.set_active_symbol("MSFT", source="search")
    tasks[0].run()
    tasks[1].run()
    assert window.current_symbol == "MSFT"
    assert window.current_research_snapshot is None
    assert window.current_analyst_result is None


def test_fixed_signup_urls_and_dynamic_fabric_button(window, monkeypatch) -> None:
    opened = []

    class Desktop:
        @staticmethod
        def openUrl(url):  # noqa: ANN001
            opened.append(url.toString())
            return True

    monkeypatch.setattr("app.ui.main.QDesktopServices", Desktop)
    assert window._open_provider_signup("finnhub", window.provider_configuration_text)
    assert window._open_provider_signup("logo_dev", window.company_logo_status_text)
    assert window._open_provider_signup("congress", window.congress_configuration_text)
    for provider, expected in (
        ("alpha_vantage", "https://www.alphavantage.co/support/#api-key"),
        ("twelve_data", "https://twelvedata.com/"),
        ("fred", "https://fred.stlouisfed.org/docs/api/api_key.html"),
    ):
        window.fabric_provider_selector.setCurrentIndex(window.fabric_provider_selector.findData(provider))
        assert window.get_fabric_api_key_btn.isVisible() or not window._qt_window.isVisible()
        assert window._open_provider_signup(provider, window.fabric_provider_status_text)
        assert opened[-1] == expected
    assert opened[:3] == ["https://finnhub.io/register", "https://www.logo.dev/", "https://api.congress.gov/sign-up/"]
    assert all(url.startswith("https://") and "key_for_test_only" not in url for url in opened)
