from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.security.credentials import InMemoryCredentialStore
from app.application.bootstrap import RangeScoutApplication
from tests.fakes.mock_provider import build_test_provider_registry
from app.ui.main import build_window

try:
    from PySide6.QtWidgets import QApplication, QLineEdit
except Exception:  # pragma: no cover
    QApplication = QLineEdit = None  # type: ignore[assignment]


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_provider_settings_save_select_delete_and_never_prefill_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _WritableAdapter:
        def __init__(self, root: Path) -> None:
            self.app_name = "RangeScout"
            self.app_data_dir = str(root)
            self.config_dir = str(root)
            self.temp_dir = str(root)
            self.allow_user_install_paths = []

    adapter = _WritableAdapter(tmp_path / "RangeScout")
    data_dir = Path(adapter.app_data_dir)
    data_dir.mkdir(parents=True)
    (data_dir / "settings.json").write_text(
        json.dumps({"default_provider": "mock", "provider_policy_version": 3}),
        encoding="utf-8",
    )
    store = InMemoryCredentialStore()
    monkeypatch.setattr("app.platform.platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.default_credential_store", lambda: store)
    monkeypatch.setattr(importlib.import_module("app.ui.main"), "platform_adapter", lambda: adapter)

    qt_app = QApplication.instance() or QApplication([])
    application = RangeScoutApplication(data_dir=data_dir, credential_store=store, registry=build_test_provider_registry(store))
    window = build_window(application=application)
    secret = "FINNHUB_UI_SECRET_123456789"
    try:
        window._open_data_providers()
        dialog = window._data_providers_dialog
        assert dialog is not None
        assert dialog.key_input.echoMode() == QLineEdit.EchoMode.Password
        assert not hasattr(window, "alpaca_key_id_input")
        assert not hasattr(window, "alpaca_secret_key_input")
        finnhub_row = next(index for index, row in enumerate(dialog._rows) if row.provider_id == "finnhub")
        dialog.table.selectRow(finnhub_row)
        dialog.key_input.setText(secret)
        dialog._save()
        assert store.load("finnhub") is not None
        assert store.load("finnhub").values["api_key"] == secret
        assert dialog.key_input.text() == ""
        assert secret not in (data_dir / "settings.json").read_text(encoding="utf-8")
        forced_index = dialog.mode_combo.findData("finnhub")
        assert forced_index >= 0
        dialog.mode_combo.setCurrentIndex(forced_index)
        persisted = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
        assert persisted["provider_mode"] == "finnhub"
        assert persisted["provider_policy_version"] == 6
        dialog._delete()
        assert store.load("finnhub") is None
    finally:
        window._qt_window.close()
        if qt_app is not None and not qt_app.closingDown():
            qt_app.quit()


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_provider_fabric_settings_disclose_keys_delays_and_disabled_candidates(tmp_path: Path) -> None:
    store = InMemoryCredentialStore()
    application = RangeScoutApplication(
        data_dir=tmp_path / "RangeScout",
        credential_store=store,
        registry=build_test_provider_registry(store),
    )
    qt_app = QApplication.instance() or QApplication([])
    window = build_window(application=application, auto_refresh=False)
    try:
        window._open_data_providers()
        dialog = window._data_providers_dialog
        assert dialog is not None
        ids = {row.provider_id for row in dialog._rows}
        unavailable_ids = {
            str(dialog.unavailable_table.item(row, 0).text())
            for row in range(dialog.unavailable_table.rowCount())
        }
        assert {"coinbase_exchange", "kraken", "coinpaprika", "twelve_data", "alpha_vantage", "fred"} <= ids
        assert any(value.startswith("Google Finance") for value in unavailable_ids)
        assert any(value.startswith("MSN Money") for value in unavailable_ids)
        assert any(value.startswith("Binance.US") for value in unavailable_ids)
        twelve_row = next(index for index, row in enumerate(dialog._rows) if row.provider_id == "twelve_data")
        dialog.table.selectRow(twelve_row)
        assert "Missing API key" in dialog._rows[twelve_row].status
        secret = "TWELVE_UI_SECRET_123456789"
        dialog.key_input.setText(secret)
        dialog._save()
        assert store.load("twelve_data").values["api_key"] == secret
        assert dialog.key_input.text() == ""
        assert next(row for row in dialog._rows if row.provider_id == "twelve_data").status == "Configured"
        assert secret not in (Path(application.data_dir) / "settings.json").read_text(encoding="utf-8")
        google_row = next(
            row for row in range(dialog.unavailable_table.rowCount())
            if dialog.unavailable_table.item(row, 0).text().startswith("Google Finance")
        )
        assert dialog.unavailable_table.item(google_row, 1).text().startswith("Disabled")
    finally:
        window._qt_window.close()
        if qt_app is not None and not qt_app.closingDown():
            qt_app.quit()
