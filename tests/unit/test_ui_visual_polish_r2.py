from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QPushButton, QScrollArea, QWidget

from tests.unit.test_ui_v12_composition import window


def test_workstation_shell_and_nine_surfaces_are_visually_composed(window) -> None:
    root = window._qt_window
    assert root.findChild(QFrame, "navigation_rail") is not None
    assert root.findChild(QWidget, "active_symbol_header") is not None
    assert root.findChild(QWidget, "global_symbol_search") is window.active_symbol_input
    assert root.findChild(QWidget, "ticker_ribbon") is window.ticker_ribbon
    assert root.findChild(QFrame, "status_footer") is not None
    assert window.navigation.count() == 9
    assert len([card for card in root.findChildren(QFrame) if card.property("dashboardCard")]) >= 30


def test_visual_recovery_keeps_each_surface_distinct_and_data_honest(window) -> None:
    assert window.live_chart.minimumHeight() >= 350
    assert window.research_chart.minimumHeight() >= 270
    overview_scroll = window._qt_window.findChild(QScrollArea, "research_overview_scroll")
    assert overview_scroll is not None
    assert overview_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert window.research_overview_peers.count() >= 1
    assert window.research_overview_catalysts.count() >= 1
    assert window.watchlist_symbol_table.columnCount() == 6
    assert window.scanner_detail_chart.minimumHeight() >= 220
    assert window.alert_history_list.count() >= 1
    assert window.notes_text.placeholderText()
    assert window.export_history_list.count() >= 1
    assert window.provider_settings_selector.count() == 2


def test_visual_recovery_does_not_add_brokerage_execution_or_removed_providers(window) -> None:
    button_text = "\n".join(button.text().strip().lower() for button in window._qt_window.findChildren(QPushButton))
    assert "buy" not in button_text
    assert "sell" not in button_text
    assert "place order" not in button_text

    visible_text = []
    for child in window._qt_window.findChildren(QWidget):
        getter = getattr(child, "text", None)
        if callable(getter):
            visible_text.append(str(getter()).lower())
    joined = "\n".join(visible_text)
    assert "alpaca" not in joined
    assert "mock provider" not in joined
