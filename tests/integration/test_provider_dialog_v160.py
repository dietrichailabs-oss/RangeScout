from __future__ import annotations

import gc
import json

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from app.application.bootstrap import RangeScoutApplication
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials
from app.ui.main import RangeScoutWindow


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _dispose_window(window: RangeScoutWindow, qt_app: QApplication) -> None:
    dialog = window._data_providers_dialog
    if dialog is not None:
        dialog.hide()
        dialog.deleteLater()
    window._shutdown_runtime()
    window._qt_window.hide()
    window._qt_window.deleteLater()
    qt_app.processEvents()
    gc.collect()


def test_provider_screen_owns_credentials_actions_and_signup_fallback(tmp_path, qt_app, monkeypatch) -> None:
    store = InMemoryCredentialStore()
    application = RangeScoutApplication(data_dir=tmp_path, credential_store=store)
    window = RangeScoutWindow(application=application, auto_refresh=False)
    try:
        assert window.data_providers_btn.text().replace("&&", "&") == "Open Data Providers & API Keys"
        window._open_data_providers()
        dialog = window._data_providers_dialog
        assert dialog is not None
        assert dialog.parent() is window._qt_window
        assert dialog.windowTitle() == "Data Providers & API Keys"
        assert dialog.mode_combo.itemText(0) == "Smart Search (Recommended)"
        assert dialog.table.rowCount() >= 16
        assert window.settings_tab.findChildren(type(window.data_providers_btn), "open_data_providers_button") == [window.data_providers_btn]
        assert not window.settings_tab.findChildren(type(dialog.key_input), "provider_credential_input")
        assert window.congress_api_key_input not in window.settings_tab.findChildren(QLineEdit)
        provider_management_buttons = [
            button for button in window.settings_tab.findChildren(QPushButton) if "Data Providers" in button.text()
        ]
        assert provider_management_buttons == [window.data_providers_btn]

        assert dialog.table.columnCount() == 5
        assert [dialog.table.horizontalHeaderItem(i).text() for i in range(5)] == [
            "Provider", "Used For", "API Key Required", "Status", "Action"
        ]
        qt_app.processEvents()
        expected_actions = sum(row.key_type != "No key required" for row in dialog._rows)
        assert len(dialog.table.viewport().findChildren(QPushButton)) == expected_actions
        assert sum(dialog.table.cellWidget(row, 4) is not None for row in range(dialog.table.rowCount())) == expected_actions
        assert dialog.select_provider("finnhub")
        selected_row = dialog.table.currentRow()
        configure = dialog.table.cellWidget(selected_row, 4)
        assert isinstance(configure, QPushButton)
        assert configure.text() == "Configure"
        dialog.select_provider("yahoo")
        configure.click()
        qt_app.processEvents()
        assert dialog._selected_provider_id() == "finnhub"
        assert dialog.key_input.hasFocus()
        assert dialog.details_text.isHidden()
        dialog.details_button.click()
        assert not dialog.details_text.isHidden()

        assert dialog.select_provider("sec")
        action = dialog.table.item(dialog.table.currentRow(), 4)
        assert action.text() == "No action required"

        store.save(ProviderCredentials("finnhub", {"api_key": "focused-test-value"}))
        dialog.refresh()
        assert dialog.select_provider("finnhub")
        assert dialog.table.item(dialog.table.currentRow(), 3).text() == "Configured"

        for provider_id, expected_url, button_text in (
            ("finnhub", "https://finnhub.io/register", "Get API Key"),
            ("logo_dev", "https://www.logo.dev/", "Get Publishable Key"),
        ):
            opened: list[str] = []
            assert dialog.select_provider(provider_id)
            assert dialog.signup_button.text() == button_text
            monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
            dialog._signup()
            assert opened == [expected_url]
            assert dialog.credential_status.text() == "Opened the official provider signup page."
            monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: False)
            dialog._signup()
            assert dialog.credential_status.text() == f"Open the official provider signup page: {expected_url}"
        assert dialog.select_provider("yahoo")
        assert dialog.signup_button.isHidden()
    finally:
        _dispose_window(window, qt_app)


def test_forced_mode_ui_persists_and_does_not_store_key_material(tmp_path, qt_app) -> None:
    application = RangeScoutApplication(data_dir=tmp_path, credential_store=InMemoryCredentialStore())
    window = RangeScoutWindow(application=application, auto_refresh=False)
    try:
        window._open_data_providers()
        dialog = window._data_providers_dialog
        index = dialog.mode_combo.findData("yahoo")
        assert index >= 0
        dialog.mode_combo.setCurrentIndex(index)
        assert application.settings.provider_mode == "yahoo"
        assert "Forced mode" in dialog.mode_help.text()
        payload = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert payload["provider_mode"] == "yahoo"
        assert "api_key" not in json.dumps(payload).lower()
    finally:
        _dispose_window(window, qt_app)
