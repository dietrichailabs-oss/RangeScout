from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app import PRODUCT
from app.application.bootstrap import RangeScoutApplication
from app.application.live_trading_runtime import LiveTradingRuntime
from app.configuration.settings import load_user_settings
from app.providers.byo_provider import AlpacaProvider
from app.providers.configuration import ProviderConfigurationService
from app.providers.registry import ProviderRegistry, default_provider_registry
from app.security.credentials import InMemoryCredentialStore

try:
    from PySide6.QtWidgets import QApplication, QWidget
except Exception:  # pragma: no cover
    QApplication = None
    QWidget = None


class _Sink:
    def stream_status(self, status, display_text):  # noqa: ANN001
        pass

    def live_state(self, state):  # noqa: ANN001
        pass

    def ticker_state(self, states, plan):  # noqa: ANN001
        pass

    def scanner_hits(self, hits):  # noqa: ANN001
        pass

    def runtime_alert(self, alert_type, event_id, title, message, symbol):  # noqa: ANN001
        pass


class _ExplodingStore(InMemoryCredentialStore):
    def load(self, provider_id: str):
        raise AssertionError(f"credential lookup must not occur for {provider_id}")


def test_public_registry_is_exact_and_rejects_alpaca_registration() -> None:
    store = InMemoryCredentialStore()
    registry = default_provider_registry(credential_store=store)
    assert registry.list_available() == ["yahoo", "finnhub"]
    with pytest.raises(KeyError, match="not registered"):
        registry.get("alpaca")
    with pytest.raises(ValueError, match="not available in this public build"):
        registry.register(AlpacaProvider(store.load))


def test_legacy_alpaca_setting_migrates_to_yahoo_and_preserves_unrelated_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "default_provider": "alpaca",
                "provider_policy_version": 4,
                "theme": "dark",
                "window_width": 1444,
                "future_setting": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    settings = load_user_settings(str(tmp_path))
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert settings.default_provider == "yahoo"
    assert settings.provider_policy_version == 6
    assert rewritten["default_provider"] == "yahoo"
    assert rewritten["theme"] == "dark"
    assert rewritten["window_width"] == 1444
    assert rewritten["future_setting"] == {"keep": True}


def test_explicit_hidden_and_injected_alpaca_selection_cannot_enable_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RANGESCOUT_PROVIDER", "alpaca")
    with pytest.raises(ValueError, match="not available in this public build"):
        RangeScoutApplication(data_dir=tmp_path / "direct", provider_id="alpaca")

    store = InMemoryCredentialStore()
    injected = ProviderRegistry()
    public = default_provider_registry(credential_store=store)
    for provider_id in public.list_available():
        injected.register(public.get(provider_id))
    injected.register(AlpacaProvider(store.load))
    app = RangeScoutApplication(data_dir=tmp_path / "injected", registry=injected, credential_store=store)
    try:
        assert app.registry.list_available() == ["yahoo", "finnhub"]
        assert app.provider_id == "yahoo"
        with pytest.raises(KeyError, match="not registered"):
            app.get_provider("alpaca")
    finally:
        app.store.close()


def test_runtime_rejects_alpaca_before_credentials_transport_or_network() -> None:
    transport_calls: list[str] = []
    runtime = LiveTradingRuntime(
        _ExplodingStore(),
        _Sink(),
        lambda provider, credentials: transport_calls.append(provider),
        lambda delay, callback: None,
    )
    with pytest.raises(ValueError, match="not available in this public build"):
        runtime.set_provider("alpaca")
    assert transport_calls == []
    assert runtime.connection is None


def test_public_credential_service_rejects_alpaca() -> None:
    store = InMemoryCredentialStore()
    service = ProviderConfigurationService(default_provider_registry(credential_store=store), store)
    with pytest.raises(ValueError, match="does not accept public credentials"):
        service.save_credentials("alpaca", {"key_id": "K" * 20, "secret_key": "S" * 20})
    assert service.delete_credentials("alpaca") is False
    assert store.load("alpaca") is None


def test_production_composition_sources_have_no_alpaca_construction_path() -> None:
    root = Path(__file__).resolve().parents[2]
    production_sources = (
        root / "app" / "providers" / "registry.py",
        root / "app" / "application" / "bootstrap.py",
        root / "app" / "application" / "live_trading_runtime.py",
        root / "app" / "application" / "runtime_coordinator.py",
        root / "app" / "ui" / "main.py",
        root / "app" / "ui" / "runner.py",
    )
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in production_sources)
    assert "alpacaprovider" not in combined
    assert "alpaca_url" not in combined
    assert "decode_alpaca" not in combined
    assert "provider == \"alpaca\"" not in combined
    assert "provider_id == \"alpaca\"" not in combined
    runner_source = (root / "app" / "ui" / "runner.py").read_text(encoding="utf-8").lower()
    assert "rangescout_provider" not in runner_source
    assert "--provider" not in runner_source
    assert "alpaca" not in (root / "README.md").read_text(encoding="utf-8").lower()
    release_notes = root / f"RELEASE_NOTES_v{PRODUCT.version}.md"
    assert release_notes.is_file(), f"current release notes are missing: {release_notes.name}"
    assert "alpaca" not in release_notes.read_text(encoding="utf-8").lower()


@pytest.mark.skipif(QApplication is None, reason="PySide6 is not installed")
def test_public_settings_ui_has_no_alpaca_selector_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Adapter:
        def __init__(self, root: Path) -> None:
            self.app_name = "RangeScout"
            self.app_data_dir = self.config_dir = self.temp_dir = str(root)
            self.allow_user_install_paths = []

    root = tmp_path / "RangeScout"
    root.mkdir()
    (root / "settings.json").write_text(
        json.dumps({"default_provider": "mock", "provider_policy_version": 4}),
        encoding="utf-8",
    )
    adapter = _Adapter(root)
    ui_module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(ui_module, "platform_adapter", lambda: adapter)
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: adapter)
    qt = QApplication.instance() or QApplication([])
    transport_calls: list[str] = []

    def reject_transport(provider, credentials):  # noqa: ANN001
        transport_calls.append(provider)
        raise AssertionError(f"unexpected transport construction: {provider}")

    window = ui_module.build_window(
        credential_store=InMemoryCredentialStore(),
        runtime_transport_factory=reject_transport,
        catalyst_sources=[],
    )
    window.live_refresh_timer.stop()
    try:
        for combo in (window.provider_combo, window.provider_settings_selector, window.active_provider_combo):
            values = [str(combo.itemData(index) or combo.itemText(index)).lower() for index in range(combo.count())]
            assert "alpaca" not in values
        assert not hasattr(window, "alpaca_credentials_widget")
        assert not hasattr(window, "alpaca_key_id_input")
        assert not hasattr(window, "alpaca_secret_key_input")
        with pytest.raises(KeyError, match="not registered"):
            window._on_provider_changed("alpaca")
        assert transport_calls == []
        visible_copy: list[str] = []
        for child in window._qt_window.findChildren(QWidget):
            if hasattr(child, "text") and callable(child.text):
                visible_copy.append(str(child.text()))
            if hasattr(child, "placeholderText") and callable(child.placeholderText):
                visible_copy.append(str(child.placeholderText()))
        assert "alpaca" not in "\n".join(visible_copy).lower()
    finally:
        window.app.store.close()
        window._qt_window.close()
        if qt is not None:
            qt.processEvents()
