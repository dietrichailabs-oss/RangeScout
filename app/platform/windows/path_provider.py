from __future__ import annotations

from pathlib import Path

from app.platform.contracts import PlatformAdapter


def _windows_appdata_dir() -> Path:
    base = Path.home() / "AppData" / "Roaming"
    return base


class WindowsPathAdapter(PlatformAdapter):
    def __init__(self) -> None:
        app_home = _windows_appdata_dir() / "RangeScout"
        super().__init__(
            app_name="RangeScout",
            app_data_dir=str(app_home),
            config_dir=str(app_home / "config"),
            temp_dir=str(app_home / "temp"),
            allow_user_install_paths=[],
        )
