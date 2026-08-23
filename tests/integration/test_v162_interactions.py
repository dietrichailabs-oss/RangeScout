from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.models.schemas import InstrumentIdentifier, OhlcvBar
from app.security.credentials import InMemoryCredentialStore
from app.ui.main import QApplication, build_window


@pytest.fixture()
def ux_window(tmp_path):
    if QApplication is None:
        pytest.skip("PySide6 unavailable")
    qt = QApplication.instance() or QApplication([])
    credentials = InMemoryCredentialStore()
    application = RangeScoutApplication(data_dir=tmp_path / "RangeScout", credential_store=credentials)
    window = build_window(application=application, auto_refresh=False, catalyst_sources=[])
    try:
        yield window, credentials, qt
    finally:
        window._shutdown_runtime()


def test_provider_selector_is_unified_and_two_way(ux_window) -> None:
    window, _credentials, _qt = ux_window
    assert window.provider_combo.itemData(0) == "smart"
    yahoo = window.provider_combo.findData("yahoo")
    assert yahoo >= 0
    window.provider_combo.setCurrentIndex(yahoo)
    assert window.app.settings.provider_mode == "yahoo"
    window._open_data_providers()
    dialog = window._data_providers_dialog
    smart = dialog.mode_combo.findData("smart")
    dialog.mode_combo.setCurrentIndex(smart)
    window._sync_market_provider_mode()
    assert window.provider_combo.currentData() == "smart"


def test_watchlist_action_adds_without_navigation_and_is_idempotent(ux_window) -> None:
    window, _credentials, _qt = ux_window
    initial_tab = window.tabs.currentIndex()
    window.set_active_symbol("AAPL", source="test")
    window._on_add_active_symbol_to_watchlist()
    window._on_add_active_symbol_to_watchlist()
    records = window.watchlist_store.list()
    assert len(records) == 1
    assert records[0].symbols.count("AAPL") == 1
    assert window.tabs.currentIndex() == initial_tab
    assert "Watchlisted" in window.market_watchlist_button.text()


def test_ticker_positions_and_chart_ranges_apply_immediately(ux_window) -> None:
    window, _credentials, _qt = ux_window
    top = window.ticker_position_combo.findData("top")
    bottom = window.ticker_position_combo.findData("bottom")
    window.ticker_position_combo.setCurrentIndex(bottom)
    assert window.root_layout.indexOf(window.ticker_ribbon) > window.root_layout.indexOf(window.tabs)
    window.ticker_position_combo.setCurrentIndex(top)
    assert window.root_layout.indexOf(window.ticker_ribbon) < window.root_layout.indexOf(window.tabs)

    bars = [
        OhlcvBar(InstrumentIdentifier("AAPL"), date(2026, 7, 1), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"), 10, "fake"),
        OhlcvBar(InstrumentIdentifier("AAPL"), date(2026, 8, 1), Decimal("2"), Decimal("3"), Decimal("2"), Decimal("3"), 20, "fake"),
    ]
    window.app.store.upsert_bars(bars, "fake")
    window._on_market_range_selected(365)
    assert window.market_days_input.value() == 365
    assert window.market_range_buttons[365].isChecked()
    assert sum(button.isChecked() for button in window.market_range_buttons.values()) == 1


def test_credentials_synchronize_and_notes_update_same_record(ux_window) -> None:
    window, credentials, _qt = ux_window
    window.provider_settings_selector.setCurrentIndex(window.provider_settings_selector.findData("finnhub"))
    window.app.provider_configuration.save_credentials("finnhub", {"api_key": "K" * 24})
    assert credentials.load("finnhub") is not None
    assert "Configured" in window.provider_configuration_text.text()
    window.app.provider_configuration.delete_credentials("finnhub")
    assert credentials.load("finnhub") is None

    window.notes_symbol_input.setText("AAPL")
    window.notes_text.setPlainText("first persisted body")
    assert window._on_save_note()
    note_id = window._selected_note_id
    window.notes_text.setPlainText("edited persisted body")
    assert window._on_save_note()
    assert window._selected_note_id == note_id
    assert len(window.note_store.list_for("AAPL")) == 1
    window._on_reload_notes(preserve_selection=True, bypass_unsaved=True)
    item = next(window.notes_list.item(i) for i in range(window.notes_list.count()) if window.notes_list.item(i).data(256))
    window._on_note_selected(item)
    assert window.notes_text.toPlainText() == "edited persisted body"
    assert "T" not in item.text().splitlines()[0]


def test_canonical_slash_symbol_remains_active_without_invalid_stream_subscription(ux_window) -> None:
    window, _credentials, qt = ux_window
    state = window.set_active_symbol("XAUUSD", source="global-search")
    qt.processEvents()
    assert state.symbol == "XAU/USD"
    assert state.asset_class == "commodity_spot"
    assert "XAU/USD" not in window.runtime.live.subscription_plan.subscribed
    assert "XAU/USD" in window._ticker_identity_labels
    assert window.current_symbol == "XAU/USD"
