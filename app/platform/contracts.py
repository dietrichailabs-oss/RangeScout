from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformAdapter:
    app_name: str
    app_data_dir: str
    config_dir: str
    temp_dir: str
    allow_user_install_paths: list[str]
