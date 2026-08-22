from __future__ import annotations

from pathlib import Path

from app.platform.contracts import PlatformAdapter


class LinuxPathAdapter(PlatformAdapter):
    def __init__(self) -> None:
        base = Path.home() / ".local" / "share" / "rangescout"
        super().__init__(
            app_name="RangeScout",
            app_data_dir=str(base),
            config_dir=str(base / "config"),
            temp_dir=str(base / "temp"),
            allow_user_install_paths=[],
        )
