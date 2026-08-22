from __future__ import annotations

from decimal import Decimal

from PySide6.QtWidgets import QFrame, QLabel, QPushButton

from app.application.live_trading_runtime import LiveSymbolState
from app.streaming.ticker import plan_ticker_subscriptions

from tests.unit.test_ui_v12_composition import window


EVIDENCE_SYMBOLS = ["AAPL", "TSLA", "AMZN", "NVDA", "MSFT", "SPY", "QQQ", "BA", "RTX"]


def _apply_profile(window) -> None:
    window.watchlist_store.create("visual-qa", "My Watchlist")
    for symbol in EVIDENCE_SYMBOLS:
        window.watchlist_store.add_symbol("visual-qa", symbol)
    window.note_store.add("BA", "Disposable QA note for visual evidence only.")
    window.set_active_symbol("BA", source="unit-test")
    states = {
        symbol: LiveSymbolState(
            symbol,
            price=Decimal("101") if index % 2 == 0 else Decimal("99"),
            previous_close=Decimal("100"),
        )
        for index, symbol in enumerate(EVIDENCE_SYMBOLS)
    }
    window.runtime_ticker_state(states, plan_ticker_subscriptions(EVIDENCE_SYMBOLS, None))


def test_r4_authoritative_shell_geometry(window) -> None:
    root = window._qt_window
    assert root.findChild(QFrame, "active_symbol_header").height() == 56
    assert root.findChild(QFrame, "navigation_rail").width() == 140
    assert root.findChild(QFrame, "status_footer").height() == 58
    assert window.ticker_ribbon.height() == 44


def test_r4_evidence_ticker_is_dense_single_row_and_ba_is_active(window) -> None:
    _apply_profile(window)
    ticker_buttons = [
        button
        for button in window.ticker_ribbon.findChildren(QPushButton)
        if button.objectName() == "ticker_symbol"
    ]
    identities = [button.findChild(QLabel, "ticker_identity") for button in ticker_buttons]
    values = [button.findChild(QLabel, "ticker_value") for button in ticker_buttons]
    assert [label.text() for label in identities] == EVIDENCE_SYMBOLS
    assert all(button.y() == ticker_buttons[0].y() for button in ticker_buttons)
    assert next(button for button, label in zip(ticker_buttons, identities) if label.text() == "BA").isChecked()
    assert all(label.property("identityNeutral") is True for label in identities)
    assert {label.property("tickerDirection") for label in values} == {"up", "down"}
    assert window.current_symbol == "BA"
    next(button for button, label in zip(ticker_buttons, identities) if label.text() == "TSLA").click()
    assert window.current_symbol == "TSLA"
    assert window.tabs.currentIndex() == 1


def test_r4_research_label_retains_literal_ampersand(window) -> None:
    labels = [window.research_tabs.tabText(index) for index in range(window.research_tabs.count())]
    assert "Catalysts & News" in labels
    assert "Catalysts News" not in labels


def test_normal_startup_has_no_preloaded_visual_state(window) -> None:
    assert not window.watchlist_store.list()
    assert window.current_symbol == "AAPL"


def test_empty_live_candle_state_preserves_loaded_historical_chart(window, monkeypatch) -> None:
    calls: list[list[float]] = []
    monkeypatch.setattr(window.live_chart, "set_series", lambda values, **_kwargs: calls.append(list(values)))
    window.current_bars = [object()]
    window.runtime_live_state(LiveSymbolState("AAPL", price=Decimal("100")))
    assert calls == []
