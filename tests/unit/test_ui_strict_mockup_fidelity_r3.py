from __future__ import annotations

from PySide6.QtWidgets import QFrame, QPushButton

from tests.unit.test_ui_v12_composition import window


def test_strict_shell_geometry_and_density_match_reference_contract(window) -> None:
    root = window._qt_window
    header = root.findChild(QFrame, "active_symbol_header")
    rail = root.findChild(QFrame, "navigation_rail")
    footer = root.findChild(QFrame, "status_footer")

    # R4_LAYOUT_BLUEPRINT.json is authoritative for the locked shell.
    assert header is not None and header.height() == 56
    assert rail is not None and rail.width() == 140
    assert footer is not None and footer.height() == 58
    assert window.ticker_ribbon.height() == 44
    assert window.navigation.count() == 9


def test_ticker_has_selector_manage_active_outline_and_click_routing(window) -> None:
    window._refresh_ticker_ribbon()
    selector = window.ticker_ribbon.findChild(QPushButton, "ticker_selector")
    manage = window.ticker_ribbon.findChild(QPushButton, "ticker_manage")
    ticker_buttons = [
        button
        for button in window.ticker_ribbon.findChildren(QPushButton)
        if button.isCheckable()
    ]

    assert selector is not None and "Watchlist" in selector.text()
    assert manage is not None and "Manage" in manage.text()
    assert ticker_buttons
    assert window._ticker_identity_labels["AAPL"].text() == "AAPL"
    assert window._ticker_identity_labels["AAPL"].property("identityNeutral") is True
    assert window._ticker_buttons["AAPL"].isChecked()

    ticker_buttons[0].click()
    assert window.current_symbol == "AAPL"
    assert window.tabs.currentIndex() == 1


def test_all_nine_surfaces_remain_wired_to_the_strict_shell(window) -> None:
    for index in range(9):
        window.tabs.setCurrentIndex(index)
        assert window.tabs.currentWidget() is not None
        assert window.navigation.currentRow() == index
        assert window.ticker_ribbon.parentWidget() is not None
