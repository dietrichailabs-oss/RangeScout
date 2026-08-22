"""System-theme resolution that is testable without changing Windows settings."""

from __future__ import annotations

import sys
from typing import Callable


LIGHT = "light"
DARK = "dark"
SYSTEM = "system"


def normalize_theme(value: object) -> str:
    normalized = str(value).strip().lower()
    return normalized if normalized in {SYSTEM, LIGHT, DARK} else DARK


def windows_apps_use_light_theme(reader: Callable[[], int] | None = None) -> bool | None:
    if reader is not None:
        try:
            return bool(int(reader()))
        except (TypeError, ValueError, OSError):
            return None
    if sys.platform != "win32":
        return None
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(int(value))
    except (OSError, ValueError, TypeError):
        return None


def resolve_effective_theme(
    preference: object,
    *,
    qt_color_scheme: object | None = None,
    windows_light_reader: Callable[[], int] | None = None,
) -> str:
    selected = normalize_theme(preference)
    if selected in {LIGHT, DARK}:
        return selected
    if qt_color_scheme is not None:
        text = str(qt_color_scheme).strip().lower()
        if "dark" in text or text in {"2", "colorscheme.dark"}:
            return DARK
        if "light" in text or text in {"1", "colorscheme.light"}:
            return LIGHT
    windows_light = windows_apps_use_light_theme(windows_light_reader)
    if windows_light is not None:
        return LIGHT if windows_light else DARK
    return DARK
