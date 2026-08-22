from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from PySide6.QtGui import QIcon
except Exception:  # pragma: no cover - exercised only without the optional GUI runtime
    QIcon = None  # type: ignore[assignment]


APP_ICON_RELATIVE_PATH = Path("resources") / "rangescout.ico"


def application_resource_root(*, frozen_root: str | Path | None = None) -> Path:
    """Return the source/package root that owns RangeScout runtime resources.

    PyInstaller exposes its bundled data root through ``sys._MEIPASS``. Source
    executions resolve two parents above this module, which is the repository
    root containing ``resources/``.
    """

    if frozen_root is not None:
        return Path(frozen_root)
    packaged_root = getattr(sys, "_MEIPASS", None)
    if packaged_root:
        return Path(packaged_root)
    return Path(__file__).resolve().parents[2]


def application_icon_path(*, frozen_root: str | Path | None = None) -> Path:
    return application_resource_root(frozen_root=frozen_root) / APP_ICON_RELATIVE_PATH


def load_application_icon(*, frozen_root: str | Path | None = None) -> Any | None:
    """Load the single RangeScout icon shared by the app, window and tray."""

    if QIcon is None:
        return None
    icon_path = application_icon_path(frozen_root=frozen_root)
    if not icon_path.is_file():
        return QIcon()
    return QIcon(str(icon_path))
