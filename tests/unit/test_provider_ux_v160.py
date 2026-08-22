from __future__ import annotations

import json
from dataclasses import replace

from app.application.bootstrap import RangeScoutApplication
from app.configuration.settings import AppSettings, load_user_settings, normalize_provider_mode
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials
from app.ui.provider_dialog import SIGNUP_URLS, provider_rows


def test_provider_mode_defaults_to_smart_and_unknown_values_migrate(tmp_path) -> None:
    assert AppSettings().provider_mode == "smart"
    assert normalize_provider_mode("alpaca") == "smart"
    (tmp_path / "settings.json").write_text(json.dumps({"provider_mode": "ALPACA"}), encoding="utf-8")
    assert load_user_settings(str(tmp_path)).provider_mode == "smart"


def test_provider_mode_persists_without_credentials(tmp_path) -> None:
    app = RangeScoutApplication(data_dir=tmp_path, credential_store=InMemoryCredentialStore())
    try:
        assert app.set_provider_mode("yahoo") == "yahoo"
        payload = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert payload["provider_mode"] == "yahoo"
        serialized = json.dumps(payload).lower()
        assert "api_key" not in serialized and "credential" not in serialized
    finally:
        app.shutdown()


def test_provider_inventory_covers_market_research_discovery_and_logos(tmp_path) -> None:
    app = RangeScoutApplication(data_dir=tmp_path, credential_store=InMemoryCredentialStore())
    try:
        rows = provider_rows(app)
        ids = {row.provider_id for row in rows}
        assert {"yahoo", "finnhub", "twelve_data", "alpha_vantage", "coinbase_exchange", "kraken", "coinpaprika"} <= ids
        assert {"sec", "nasdaq", "white_house", "congress", "logo_dev"} <= ids
        assert any(row.provider_id == "binance_us_candidate" and "Disabled" in row.status for row in rows)
    finally:
        app.shutdown()


def test_signup_destinations_are_fixed_official_https_only() -> None:
    assert SIGNUP_URLS
    assert all(url.startswith("https://") for url in SIGNUP_URLS.values())
    assert all("{" not in url and "?key=" not in url.lower() for url in SIGNUP_URLS.values())


def test_configured_key_provider_has_unambiguous_status(tmp_path) -> None:
    store = InMemoryCredentialStore()
    store.save(ProviderCredentials("finnhub", {"api_key": "focused-test-value"}))
    app = RangeScoutApplication(data_dir=tmp_path, credential_store=store)
    try:
        row = next(item for item in provider_rows(app) if item.provider_id == "finnhub")
        assert row.status == "Configured"
    finally:
        app.shutdown()
