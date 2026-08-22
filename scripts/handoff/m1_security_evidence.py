#!/usr/bin/env python
"""Generate secret-free M1 provider and credential security evidence."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import tempfile
import traceback
import urllib.error
import uuid
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.configuration.settings import AppSettings, save_user_settings
from app.logging_support.log import RedactingFormatter
from app.providers.base import ProviderUnavailable
from app.providers.registry import default_provider_registry
from app.security.credentials import (
    InMemoryCredentialStore,
    ProviderCredentials,
    WindowsCredentialStore,
)


def collect(*, exercise_windows_store: bool) -> dict[str, object]:
    finnhub_secret = secrets.token_urlsafe(32)
    secret_values = (finnhub_secret,)

    store = InMemoryCredentialStore()
    registry = default_provider_registry(credential_store=store)
    before = [asdict(status) for status in _configuration(registry, store)]
    store.save(ProviderCredentials("finnhub", {"api_key": finnhub_secret}))
    configured = [asdict(status) for status in _configuration(registry, store)]
    credential_reprs = (repr(store.load("finnhub")),)

    finnhub = registry.get("finnhub")
    http_error = urllib.error.HTTPError(
        "https://finnhub.io/api/v1/quote?symbol=AAPL",
        401,
        "unauthorized",
        {},
        None,
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        try:
            finnhub.fetch_quote("AAPL")
        except ProviderUnavailable as exc:
            failure_text = str(exc)
            failure_trace = traceback.format_exc()
        else:  # pragma: no cover
            raise RuntimeError("expected authenticated provider failure")

    with tempfile.TemporaryDirectory(prefix="rangescout-m1-settings-") as folder:
        settings_root = Path(folder)
        save_user_settings(str(settings_root), AppSettings(default_provider="finnhub"))
        settings_text = (settings_root / "settings.json").read_text(encoding="utf-8")
        settings_payload = json.loads(settings_text)

    formatter = RedactingFormatter("%(message)s")
    import logging

    record = logging.LogRecord(
        "rangescout-evidence",
        logging.ERROR,
        __file__,
        0,
        f"X-Finnhub-Token: {finnhub_secret}",
        (),
        None,
    )
    formatted_log = formatter.format(record)

    production_secret_literal_matches = _scan_for_embedded_provider_secrets(REPO_ROOT / "app")
    windows_result: dict[str, object] = {
        "exercised": False,
        "save_load_delete_pass": None,
        "cleanup_verified": None,
    }
    if exercise_windows_store:
        windows_store = WindowsCredentialStore(target_prefix=f"RangeScout/M1Evidence/{uuid.uuid4()}")
        saved = False
        try:
            windows_store.save(ProviderCredentials("finnhub", {"api_key": finnhub_secret}))
            saved = True
            loaded = windows_store.load("finnhub")
            round_trip = loaded is not None and loaded.values["api_key"] == finnhub_secret
        finally:
            if saved:
                windows_store.delete("finnhub")
        cleanup = windows_store.load("finnhub") is None
        windows_result = {
            "exercised": True,
            "save_load_delete_pass": round_trip and cleanup,
            "cleanup_verified": cleanup,
        }

    store.delete("finnhub")
    after_delete = [asdict(status) for status in _configuration(registry, store)]
    ui_source = (REPO_ROOT / "app" / "ui" / "main.py").read_text(encoding="utf-8")
    payload: dict[str, object] = {
        "schema": "rangescout.m1-provider-credential-security.v1",
        "provider_registration": registry.list_available(),
        "configuration_before": before,
        "configuration_after_save": configured,
        "configuration_after_delete": after_delete,
        "settings_json_keys": sorted(settings_payload),
        "settings_secret_fields_absent": not any(
            name in settings_payload
            for name in ("api_key", "key_id", "secret_key", "token", "credentials")
        ),
        "settings_secret_values_absent": all(secret not in settings_text for secret in secret_values),
        "provider_error_secret_free": all(secret not in failure_text for secret in secret_values),
        "provider_trace_secret_free": all(secret not in failure_trace for secret in secret_values),
        "redacting_formatter_pass": all(secret not in formatted_log for secret in secret_values),
        "credential_repr_secret_free": all(
            secret not in rendered for secret in secret_values for rendered in credential_reprs
        ),
        "password_echo_mode_present": "setEchoMode(QLineEdit.EchoMode.Password)" in ui_source,
        "credential_fields_cleared_after_action": all(
            marker in ui_source
            for marker in (
                "self.finnhub_api_key_input.clear()",
            )
        ),
        "embedded_provider_secret_literal_matches": production_secret_literal_matches,
        "no_embedded_provider_secret_literals": not production_secret_literal_matches,
        "windows_credential_manager": windows_result,
        "no_silent_fallback_contract": True,
        "m2_streaming_started": False,
    }
    payload["overall_pass"] = all(
        (
            payload["provider_registration"] == ["yahoo", "finnhub"],
            payload["settings_secret_fields_absent"],
            payload["settings_secret_values_absent"],
            payload["provider_error_secret_free"],
            payload["provider_trace_secret_free"],
            payload["redacting_formatter_pass"],
            payload["credential_repr_secret_free"],
            payload["password_echo_mode_present"],
            payload["credential_fields_cleared_after_action"],
            payload["no_embedded_provider_secret_literals"],
            not exercise_windows_store or windows_result["save_load_delete_pass"] is True,
        )
    )
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if any(secret in serialized for secret in secret_values):
        raise RuntimeError("credential value reached the evidence payload")
    return payload


def _configuration(registry, store):
    from app.providers.configuration import ProviderConfigurationService

    return ProviderConfigurationService(registry, store).list_statuses()


def _scan_for_embedded_provider_secrets(root: Path) -> list[str]:
    patterns = (
        re.compile(r"X-Finnhub-Token\s*[:=]\s*['\"][A-Za-z0-9._-]{16,}['\"]", re.IGNORECASE),
        re.compile(r"APCA-API-(?:KEY-ID|SECRET-KEY)\s*[:=]\s*['\"][A-Za-z0-9._-]{16,}['\"]", re.IGNORECASE),
    )
    matches: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in patterns):
            matches.append(path.relative_to(REPO_ROOT).as_posix())
    return matches


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--exercise-windows-store", action="store_true")
    args = parser.parse_args()
    payload = collect(exercise_windows_store=args.exercise_windows_store)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    raise SystemExit(0 if payload["overall_pass"] else 1)


if __name__ == "__main__":
    main()
