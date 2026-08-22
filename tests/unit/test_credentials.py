from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.configuration.settings import AppSettings, save_user_settings
from app.configuration.settings import load_user_settings
from app.security.credentials import (
    CredentialStorageError,
    InMemoryCredentialStore,
    ProviderCredentials,
    WindowsCredentialStore,
)


def test_provider_credentials_validate_fields_and_hide_values() -> None:
    secret = "FINNHUB_TEST_SECRET_123456789"
    credentials = ProviderCredentials("finnhub", {"api_key": secret})
    assert credentials.provider_id == "finnhub"
    assert secret not in repr(credentials)
    assert secret not in str(credentials)
    with pytest.raises(ValueError, match="required"):
        ProviderCredentials("alpaca", {"key_id": "only-one-field"})


def test_memory_store_round_trip_and_delete() -> None:
    store = InMemoryCredentialStore()
    credentials = ProviderCredentials(
        "alpaca",
        {"key_id": "ALPACA_TEST_KEY_123456", "secret_key": "ALPACA_TEST_SECRET_123456"},
    )
    assert store.load("alpaca") is None
    store.save(credentials)
    assert store.load("alpaca") == credentials
    assert store.delete("alpaca") is True
    assert store.load("alpaca") is None
    assert store.delete("alpaca") is False


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager evidence")
def test_windows_credential_manager_round_trip_uses_isolated_target() -> None:
    prefix = f"RangeScout/M1Test/{uuid.uuid4()}"
    store = WindowsCredentialStore(target_prefix=prefix)
    secret = "FINNHUB_WINDOWS_TEST_123456789"
    credentials = ProviderCredentials("finnhub", {"api_key": secret})
    saved = False
    try:
        assert store.load("finnhub") is None
        try:
            store.save(credentials)
            saved = True
        except CredentialStorageError as exc:
            if "error 1312" in str(exc):
                pytest.skip("test process has no interactive Windows credential logon session")
            raise
        loaded = store.load("finnhub")
        assert loaded == credentials
    finally:
        if saved:
            store.delete("finnhub")
    assert store.load("finnhub") is None


def test_settings_json_contains_no_provider_secret_fields(tmp_path: Path) -> None:
    save_user_settings(str(tmp_path), AppSettings(default_provider="alpaca"))
    raw = (tmp_path / "settings.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["default_provider"] == "yahoo"
    assert payload["provider_policy_version"] == 6
    for forbidden in ("api_key", "key_id", "secret_key", "token", "credential"):
        assert forbidden not in raw.lower()


def test_loading_settings_removes_plaintext_credential_fields(tmp_path: Path) -> None:
    sentinel = "PLAINTEXT_MUST_BE_REMOVED_123456"
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "default_provider": "finnhub",
                "provider_policy_version": 3,
                "finnhub_api_key": sentinel,
                "provider_credentials": {"finnhub": sentinel},
                "providers": {"finnhub": {"api_key": sentinel}},
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_user_settings(str(tmp_path))
    rewritten = path.read_text(encoding="utf-8")
    assert loaded.default_provider == "finnhub"
    assert loaded.theme == "dark"
    assert sentinel not in rewritten
    assert "finnhub_api_key" not in rewritten
    assert "provider_credentials" not in rewritten
    assert "api_key" not in rewritten
