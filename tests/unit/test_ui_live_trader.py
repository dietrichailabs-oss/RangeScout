from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.catalysts.correlation import CorrelatedEvent
from app.catalysts.normalization import normalize_event
from app.application.bootstrap import RangeScoutApplication
from app.security.credentials import InMemoryCredentialStore
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtWidgets import QApplication, QLabel
except Exception:
    QApplication = QLabel = None


class Adapter:
    def __init__(self, root: Path):
        self.app_name = "RangeScout"
        self.app_data_dir = self.config_dir = self.temp_dir = str(root)
        self.allow_user_install_paths = []


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if QApplication is None:
        pytest.skip("PySide6 is not installed")
    root = tmp_path / "RangeScout"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps({"default_provider": "mock", "provider_policy_version": 3, "theme": "dark"}), encoding="utf-8"
    )
    adapter = Adapter(root)
    module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(module, "platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    QApplication.instance() or QApplication([])
    store = InMemoryCredentialStore()
    application = RangeScoutApplication(data_dir=root, credential_store=store, registry=build_test_provider_registry(store))
    value = module.build_window(application=application, auto_refresh=False)
    initial = value.market_data.fetch_quote("AAPL")
    value._last_quote_provider_id = initial.metadata.provider_id
    value._apply_quote_success(initial.payload, refresh_collections=False)
    value.live_refresh_timer.stop()
    try:
        yield value
    finally:
        value.app.store.close()
        value._qt_window.close()


def test_live_trader_has_required_fields_and_intervals(window) -> None:
    assert window.tabs.tabText(1) == "Live Trader"
    assert [window.live_candle_interval.itemData(i) for i in range(window.live_candle_interval.count())] == [1, 5, 15, 30, 60, 300]
    assert window.live_symbol_text.text() == "AAPL"
    assert window.live_provider_text.text() == "Yahoo Test Fixture"
    assert window.live_last_update_text.text() != "--"


@pytest.mark.parametrize("theme", ["system", "light", "dark"])
def test_market_status_is_explicit_bold_and_theme_readable(window, theme: str) -> None:
    window._apply_theme(theme)
    assert window.market_status_text.text().startswith("MARKET ")
    assert "font-weight: 700" in window.market_status_text.styleSheet()
    assert "color:" in window.market_status_text.styleSheet()
    assert window.live_market_status_text.text() == window.market_status_text.text()


def test_halted_is_stronger_red_warning_and_keeps_text(window) -> None:
    label = QLabel("HALTED — volatility pause")
    window._style_market_status(label, "HALTED")
    assert "background-color: #991b1b" in label.styleSheet()
    assert "HALTED" in label.text()


def test_ticker_position_and_click_route_to_live_trader(window) -> None:
    window.watchlist_store.create("daily", "Daily")
    window.watchlist_store.add_symbol("daily", "MSFT")
    window._refresh_watchlists_widget()
    window._open_live_symbol("MSFT")
    assert window.tabs.currentWidget() is window.live_trader_tab
    assert window.live_symbol_text.text() == "MSFT"
    window.ticker_position_combo.setCurrentIndex(window.ticker_position_combo.findData("hidden"))
    assert window.ticker_ribbon.isHidden()


def test_catalyst_sidebar_shows_source_category_symbols_and_disclosure(window) -> None:
    event = normalize_event("SEC", "https://www.sec.gov/a", datetime.now(timezone.utc), "ACME filed 8-K")
    event = replace(event, symbols=("ACME",), category="sec_filing")
    window.set_active_symbol("ACME", source="test")
    window.set_catalyst_events([CorrelatedEvent(event, "group")])
    text = window.catalyst_list.item(0).text()
    assert "SEC" in text and "ACME" in text and "Sec Filing" in text
    assert "not price prediction" in window.catalyst_disclosure.text()


def test_active_symbol_catalysts_hide_other_tickers_and_preserve_official_link(window, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    ba = replace(
        normalize_event("Nasdaq Trader", "https://www.nasdaqtrader.com/ba", now, "HALTED — BA"),
        symbols=("BA",),
        category="halt",
    )
    rtx = replace(
        normalize_event("Nasdaq Trader", "https://www.nasdaqtrader.com/rtx", now, "HALTED — RTX"),
        symbols=("RTX",),
        category="halt",
    )
    broad = normalize_event(
        "White House",
        "https://www.whitehouse.gov/briefing-room/policy/",
        now,
        "National infrastructure policy update",
    )
    correlated = [CorrelatedEvent(ba, "ba"), CorrelatedEvent(rtx, "rtx"), CorrelatedEvent(broad, "broad")]
    opened: list[str] = []

    class DesktopServices:
        @staticmethod
        def openUrl(url):
            opened.append(url.toString())
            return True

    module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(module, "QDesktopServices", DesktopServices)

    window.set_active_symbol("BA", source="test")
    window.set_catalyst_events(correlated)
    ba_text = "\n".join(window.catalyst_list.item(i).text() for i in range(window.catalyst_list.count()))
    assert "HALTED — BA" in ba_text
    assert "Broad Market" in ba_text
    assert "HALTED — RTX" not in ba_text
    assert "HALTED — RTX" not in "\n".join(
        window.research_catalyst_list.item(i).text() for i in range(window.research_catalyst_list.count())
    )
    ba_item = next(window.catalyst_list.item(i) for i in range(window.catalyst_list.count()) if "HALTED — BA" in window.catalyst_list.item(i).text())
    window._open_catalyst_item(ba_item)
    assert window.current_symbol == "BA"
    assert opened == ["https://www.nasdaqtrader.com/ba"]

    window.set_active_symbol("RTX", source="test")
    window.set_catalyst_events(correlated)
    rtx_text = "\n".join(window.catalyst_list.item(i).text() for i in range(window.catalyst_list.count()))
    assert "HALTED — RTX" in rtx_text
    assert "Broad Market" in rtx_text
    assert "HALTED — BA" not in rtx_text
