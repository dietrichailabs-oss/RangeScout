from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.configuration.settings import (
    ALLOWED_LIVE_REFRESH_INTERVALS_MS,
    AppSettings,
    load_user_settings,
)
from app.domain.errors import DataRootError


def test_malformed_settings_file_falls_back_to_defaults(tmp_path: Path) -> None:
    settings_path = Path(tmp_path) / "settings.json"
    settings_path.write_text("{bad json", encoding="utf-8")
    app = RangeScoutApplication(data_dir=tmp_path)
    try:
        assert app.settings == AppSettings()
    finally:
        app.store.close()


def test_settings_are_loaded_and_persisted_across_restart(tmp_path: Path) -> None:
    payload = {
        "window_width": 1200,
        "window_height": 700,
        "theme": "dark",
        "default_provider": "mock",
        "provider_policy_version": 3,
        "history_days_default": 111,
        "cache_days_max": 5555,
        "live_timeout_seconds": 8.25,
        "live_refresh_interval_ms": 500,
    }
    (Path(tmp_path) / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    app = RangeScoutApplication(data_dir=tmp_path)
    try:
        assert app.settings.window_width == 1200
        assert app.settings.theme == "dark"
        app.settings = AppSettings(
            **{
                "window_width": 1280,
                "window_height": 810,
                "theme": "light",
                "default_provider": "mock",
                "provider_policy_version": 2,
                "history_days_default": 365,
                "cache_days_max": 3650,
                "live_timeout_seconds": 12.0,
                "live_refresh_interval_ms": 30000,
            }
        )
        app.persist_settings()
    finally:
        app.store.close()

    app2 = RangeScoutApplication(data_dir=tmp_path)
    try:
        assert app2.settings.window_width == 1280
        assert app2.settings.window_height == 810
        assert app2.settings.theme == "light"
        assert app2.settings.live_refresh_interval_ms == 30000
    finally:
        app2.store.close()


class _BlockingAdapter:
    def __init__(self, path: Path) -> None:
        self.app_name = "RangeScout"
        self.app_data_dir = str(path)
        self.config_dir = str(path)
        self.temp_dir = str(path)
        self.allow_user_install_paths = []


def test_read_only_data_root_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    readonly_dir = tmp_path / "unwritable"
    readonly_dir.mkdir()
    monkeypatch.setattr(
        "app.application.bootstrap.RangeScoutApplication._assert_path_writable",
        lambda _self, _path: (_ for _ in ()).throw(OSError("blocked")),
        raising=False,
    )
    monkeypatch.setattr("app.application.bootstrap.platform_adapter", lambda: _BlockingAdapter(readonly_dir))

    with pytest.raises(DataRootError):
        RangeScoutApplication(data_dir=None)


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-root"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(DataRootError):
        RangeScoutApplication(data_dir=link)


def test_no_system_temp_fallback_is_selected(tmp_path: Path) -> None:
    target = tmp_path / "primary"
    app = RangeScoutApplication(data_dir=target)
    try:
        assert Path(app.data_dir).resolve() == target.resolve()
    finally:
        app.store.close()


@pytest.mark.parametrize("legacy_provider", ["mock", "future-provider"])
def test_legacy_saved_provider_migrates_to_live_and_preserves_settings(
    tmp_path: Path,
    legacy_provider: str,
) -> None:
    payload = {
        "window_width": 1444,
        "window_height": 777,
        "theme": "dark",
        "default_provider": legacy_provider,
        "history_days_default": 222,
        "cache_days_max": 4444,
        "live_timeout_seconds": 9.75,
    }
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    app = RangeScoutApplication(data_dir=tmp_path)
    try:
        assert app.provider_id == "yahoo"
        assert app.provider.provider_id == "yahoo"
        assert app.settings.default_provider == "yahoo"
        assert app.settings.provider_policy_version == 6
        assert app.settings.window_width == 1444
        assert app.settings.window_height == 777
        assert app.settings.theme == "dark"
        assert app.settings.history_days_default == 222
        assert app.settings.cache_days_max == 4444
        assert app.settings.live_timeout_seconds == 9.75
    finally:
        app.store.close()

    normalized = json.loads(settings_path.read_text(encoding="utf-8"))
    assert normalized == {
        **payload,
        "default_provider": "yahoo",
        "provider_mode": "smart",
        "provider_policy_version": 6,
        "live_refresh_interval_ms": 10000,
        "ticker_position": "top",
        "company_update_schedule": "weekly",
        "logo_refresh_schedule": "monthly",
        "last_page": 0,
        "research_period": "annual",
        "selected_watchlist": "",
        "window_x": None,
        "window_y": None,
        "splitter_positions": [],
        "recent_symbols": [],
    }


def test_valid_legacy_mock_saved_provider_migrates_to_yahoo(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    original = '{"default_provider":"mock","provider_policy_version":2,"theme":"light"}'
    settings_path.write_text(original, encoding="utf-8")

    app = RangeScoutApplication(data_dir=tmp_path)
    try:
        assert app.provider_id == "yahoo"
    finally:
        app.store.close()

    normalized = json.loads(settings_path.read_text(encoding="utf-8"))
    assert normalized["default_provider"] == "yahoo"
    assert normalized["provider_policy_version"] == 6
    assert normalized["theme"] == "light"
    assert normalized["live_refresh_interval_ms"] == 10000


@pytest.mark.parametrize("interval_ms", ALLOWED_LIVE_REFRESH_INTERVALS_MS)
def test_allowed_refresh_intervals_are_loaded(interval_ms: int, tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"live_refresh_interval_ms": interval_ms, "provider_policy_version": 2}),
        encoding="utf-8",
    )
    assert load_user_settings(str(tmp_path)).live_refresh_interval_ms == interval_ms


@pytest.mark.parametrize("invalid", [0, 501, 5000, 60000, "fast", None, True])
def test_invalid_refresh_interval_normalizes_to_default(invalid: object, tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"live_refresh_interval_ms": invalid, "provider_policy_version": 2}),
        encoding="utf-8",
    )
    assert load_user_settings(str(tmp_path)).live_refresh_interval_ms == 10000
    assert json.loads(settings_path.read_text(encoding="utf-8"))["live_refresh_interval_ms"] == 10000


def test_legacy_refresh_migration_preserves_other_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    legacy = {
        "provider_policy_version": 2,
        "default_provider": "mock",
        "theme": "dark",
        "future_setting": {"keep": True},
    }
    settings_path.write_text(json.dumps(legacy), encoding="utf-8")
    settings = load_user_settings(str(tmp_path))
    normalized = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings.live_refresh_interval_ms == 10000
    assert normalized["live_refresh_interval_ms"] == 10000
    assert normalized["future_setting"] == {"keep": True}


def test_windows_junction_data_root_is_rejected_without_touching_target(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    expected = b"external-sentinel"
    sentinel.write_bytes(expected)
    junction = tmp_path / "junction-root"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"Unable to create Windows junction: {completed.stderr or completed.stdout}")

    with pytest.raises(DataRootError):
        RangeScoutApplication(data_dir=junction)

    assert sentinel.read_bytes() == expected
