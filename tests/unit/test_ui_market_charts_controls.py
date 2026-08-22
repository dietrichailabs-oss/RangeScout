from __future__ import annotations

import importlib
import json
import pytest

from pathlib import Path

from app.ui.main import build_window
from app.application.bootstrap import RangeScoutApplication
from app.security.credentials import InMemoryCredentialStore
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    QApplication = None  # type: ignore[assignment]


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_market_and_chart_controls_are_distinct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _WritableAdapter:
        def __init__(self, root: Path) -> None:
            self.app_name = "RangeScout"
            self.app_data_dir = str(root)
            self.config_dir = str(root)
            self.temp_dir = str(root)
            self.allow_user_install_paths = []

    adapter = _WritableAdapter(root=tmp_path / "RangeScout")
    adapter.app_data_dir = str(Path(adapter.app_data_dir))
    Path(adapter.app_data_dir).mkdir(parents=True)
    (Path(adapter.app_data_dir) / "settings.json").write_text(
        json.dumps({"default_provider": "mock", "provider_policy_version": 2}),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.platform.platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    monkeypatch.setattr(importlib.import_module("app.ui.main"), "platform_adapter", lambda: adapter)

    app = QApplication.instance() or QApplication([])
    store = InMemoryCredentialStore()
    application = RangeScoutApplication(
        data_dir=Path(adapter.app_data_dir), credential_store=store, registry=build_test_provider_registry(store)
    )
    window = build_window(application=application)
    try:
        assert window.market_symbol_input is not window.chart_symbol_input
        assert window.market_days_input is not window.chart_days_input
        window.market_symbol_input.setText("MSFT")
        window.chart_symbol_input.setText("AAPL")
        assert window.market_symbol_input.text() == "MSFT"
        assert window.chart_symbol_input.text() == "AAPL"
        window.market_days_input.setValue(90)
        window.chart_days_input.setValue(180)
        assert window.market_days_input.value() != window.chart_days_input.value()

        window.chart_symbol_input.setText("AAPL")
        window._on_refresh_chart()
        assert "No bars available for chart" not in str(window.chart_error_text.text())

        window._on_provider_changed("yahoo")
        persisted = json.loads((Path(adapter.app_data_dir) / "settings.json").read_text(encoding="utf-8"))
        assert persisted["default_provider"] == "yahoo"
        assert persisted["provider_policy_version"] == 6
    finally:
        window._qt_window.close()
        if app is not None and not app.closingDown():
            app.quit()
