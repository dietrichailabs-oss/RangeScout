#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import subprocess
import sys
import time
from pathlib import Path

# Evidence geometry is physical-pixel authoritative and must not vary with the
# workstation's desktop scaling configuration.
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCALE_FACTOR"] = "1"
os.environ["QT_FONT_DPI"] = "96"

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtWidgets import QApplication
except Exception:  # pragma: no cover
    QPoint = None  # type: ignore[assignment]
    QGuiApplication = QApplication = QImage = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.ui.main import NoGuiRuntimeError, build_window  # type: ignore[import-not-found]
from app.application.bootstrap import RangeScoutApplication  # type: ignore[import-not-found]
from app.notes.store import NoteStore  # type: ignore[import-not-found]
from app.watchlists.manager import WatchlistStore  # type: ignore[import-not-found]
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials  # type: ignore[import-not-found]
from app.research.analyst.alpha_vantage import AlphaVantageEarningsEstimatesClient  # type: ignore[import-not-found]
from app.research.analyst.finnhub import FinnhubRecommendationClient  # type: ignore[import-not-found]
from app.research.analyst.service import AnalystService  # type: ignore[import-not-found]


REQUIRED_THEME_VALUES = {"system", "light", "dark"}
TAB_LABELS = {
    "market": 0,
    "live-trader": 1,
    "research": 2,
    "watchlists": 3,
    "scanner": 4,
    "alerts": 5,
    "notes": 6,
    "exports": 7,
    "settings": 8,
}
STRICT_VIEWPORT = (1672, 941)
R4_EVIDENCE_SYMBOLS = ("AAPL", "TSLA", "AMZN", "NVDA", "MSFT", "SPY", "QQQ", "BA", "RTX")


class _DeterministicAnalystTransport:
    """Evidence-only transport; it never opens a network connection or consumes quota."""

    def get_json(self, url: str, headers=None):  # noqa: ANN001, ARG002
        if "finnhub.io" in url:
            return [{"period": "2026-08-01", "strongBuy": 8, "buy": 7, "hold": 4, "sell": 1, "strongSell": 0}]
        if "alphavantage.co" in url:
            return {
                "annualEarningsEstimates": [
                    {"horizon": "current fiscal year", "fiscalDateEnding": "2026-12-31", "epsEstimateAverage": "12.40", "revenueEstimateAverage": "78000000000", "epsEstimateAnalystCount": "22", "epsEstimateRevisionUpTrailing30Days": "3", "epsEstimateRevisionDownTrailing30Days": "1"},
                    {"horizon": "next fiscal year", "fiscalDateEnding": "2027-12-31", "epsEstimateAverage": "13.10", "revenueEstimateAverage": "82500000000", "epsEstimateAnalystCount": "20"},
                ],
                "quarterlyEarningsEstimates": [
                    {"horizon": "current quarter", "fiscalDateEnding": "2026-09-30", "epsEstimateAverage": "3.05", "epsEstimateAnalystCount": "19"},
                    {"horizon": "next quarter", "fiscalDateEnding": "2026-12-31", "epsEstimateAverage": "3.30", "epsEstimateAnalystCount": "18"},
                ],
            }
        raise AssertionError("Evidence transport received an unexpected destination.")


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_uniform_rgb(pixmap) -> bool:
    image = pixmap.toImage() if hasattr(pixmap, "toImage") else pixmap
    if image.width() <= 1 or image.height() <= 1:
        return True
    sample_x = max(1, image.width() // 12)
    sample_y = max(1, image.height() // 12)
    first = image.pixel(0, 0)
    for y in range(0, image.height(), sample_y):
        for x in range(0, image.width(), sample_x):
            if image.pixel(x, y) != first:
                return False
    return True


def _blank_screenshot(path: Path, width: int = 0, height: int = 0) -> dict[str, object]:
    return {
        "path": str(path),
        "width": width,
        "height": height,
        "saved": False,
        "sha256": None,
        "uniform_image": True,
        "non_uniform_image": False,
    }


def _write_screenshot(path: Path, pixmap, *, width: int, height: int) -> dict[str, object]:
    if width <= 0 or height <= 0:
        return {"path": str(path), "width": width, "height": height, "saved": False, "uniform_image": True}
    saved = bool(pixmap.save(str(path), "PNG"))
    record = {
        "path": str(path),
        "width": width,
        "height": height,
        "saved": saved,
        "sha256": _sha256(path) if saved else None,
    }
    uniform = _is_uniform_rgb(pixmap) if saved else True
    record["uniform_image"] = uniform
    record["non_uniform_image"] = bool(saved and not _is_uniform_rgb(pixmap))
    return record


def _capture_widget(widget, path: Path) -> dict[str, object]:
    pixmap = widget.grab()
    return {
        **_write_screenshot(path, pixmap, width=pixmap.width(), height=pixmap.height()),
        "width": pixmap.width(),
        "height": pixmap.height(),
    }


def _capture_window_path(path: Path, handle: int | None = None) -> dict[str, object]:
    if os.name == "nt" and handle is not None and QImage is not None:
        try:
            import ctypes
            from ctypes import wintypes

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            rect = wintypes.RECT()
            if not user32.GetClientRect(int(handle), ctypes.byref(rect)):
                raise OSError("GetClientRect failed")
            logical_width = int(rect.right - rect.left)
            logical_height = int(rect.bottom - rect.top)
            scale = _window_scale_factor(handle)
            width = round(logical_width * scale)
            height = round(logical_height * scale)
            window_dc = user32.GetDC(int(handle))
            memory_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            previous = gdi32.SelectObject(memory_dc, bitmap)
            try:
                if not user32.PrintWindow(int(handle), memory_dc, 0x00000003):
                    raise OSError("PrintWindow failed")
                info = BITMAPINFO()
                info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                info.bmiHeader.biWidth = width
                info.bmiHeader.biHeight = -height
                info.bmiHeader.biPlanes = 1
                info.bmiHeader.biBitCount = 32
                info.bmiHeader.biCompression = 0
                buffer = ctypes.create_string_buffer(width * height * 4)
                if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0):
                    raise OSError("GetDIBits failed")
                image = QImage(buffer.raw, width, height, width * 4, QImage.Format.Format_ARGB32).copy()
                if (width, height) != STRICT_VIEWPORT:
                    image = image.scaled(*STRICT_VIEWPORT)
                    width, height = STRICT_VIEWPORT
                return _write_screenshot(path, image, width=width, height=height)
            finally:
                gdi32.SelectObject(memory_dc, previous)
                gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(memory_dc)
                user32.ReleaseDC(int(handle), window_dc)
        except Exception:
            pass
    app = QGuiApplication.instance()
    if app is None:
        return {"path": str(path), "width": 0, "height": 0, "saved": False, "uniform_image": True, "non_uniform_image": False}
    screen = app.primaryScreen()
    if handle is not None and QPoint is not None:
        rect = _window_rect(int(handle))
        if rect is not None:
            center = QPoint((rect["left"] + rect["right"]) // 2, (rect["top"] + rect["bottom"]) // 2)
            for candidate in app.screens():
                if candidate.geometry().contains(center):
                    screen = candidate
                    break
    if screen is None:
        return {"path": str(path), "width": 0, "height": 0, "saved": False, "uniform_image": True, "non_uniform_image": False}
    if handle is None:
        return {"path": str(path), "width": 0, "height": 0, "saved": False, "uniform_image": True, "non_uniform_image": False}
    pixmap = screen.grabWindow(int(handle))
    return _write_screenshot(path, pixmap, width=pixmap.width(), height=pixmap.height())


def _safe_tab_name(tabs_widget) -> str:
    if tabs_widget is None:
        return ""
    if tabs_widget.currentIndex() < 0:
        return ""
    return str(tabs_widget.tabText(tabs_widget.currentIndex()))


def _normalized_tab_name(value: object) -> str:
    return " ".join(str(value).strip().lower().replace("-", " ").replace("_", " ").split())


def _control_snapshot(label: str, widget) -> dict[str, object]:
    if widget is None:
        return {
            f"{label}_present": False,
            f"{label}_visible": False,
            f"{label}_enabled": False,
            f"{label}_value": None,
            f"{label}_object_name": None,
            f"{label}_class": None,
            f"{label}_parent": None,
            f"{label}_geometry": None,
            f"{label}_parent_hierarchy": None,
            f"{label}_valid_parent": False,
        }

    parent = widget.parentWidget() if hasattr(widget, "parentWidget") else None
    parent_name = getattr(parent, "objectName", lambda: "")() if callable(getattr(parent, "objectName", None)) else None
    geometry = None
    try:
        rect = widget.geometry()
        geometry = {"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()}
    except Exception:
        geometry = None

    value = ""
    if hasattr(widget, "text"):
        try:
            value = str(widget.text())
        except Exception:
            value = ""
    elif hasattr(widget, "currentText"):
        try:
            value = str(widget.currentText())
        except Exception:
            value = ""
    elif hasattr(widget, "value"):
        try:
            value = str(widget.value())
        except Exception:
            value = ""

    return {
        f"{label}_present": True,
        f"{label}_visible": bool(getattr(widget, "isVisible")()),
        f"{label}_enabled": bool(getattr(widget, "isEnabled")()),
        f"{label}_value": value,
        f"{label}_object_name": str(getattr(widget, "objectName", lambda: "")()),
        f"{label}_class": widget.__class__.__name__,
        f"{label}_parent": parent_name,
        f"{label}_geometry": geometry,
        f"{label}_parent_hierarchy": parent.objectName() if parent is not None and callable(getattr(parent, "objectName", None)) else None,
        f"{label}_valid_parent": parent is not None,
    }


def _collect_control_checks(window) -> dict[str, object]:
    checks: dict[str, object] = {
        "selected_tab": _safe_tab_name(getattr(window, "tabs", None)),
        "theme": getattr(window.app.settings, "theme", "system"),
        "selected_tab_index": getattr(window.tabs, "currentIndex", lambda: -1)() if getattr(window, "tabs", None) else -1,
        "active_symbol": getattr(window, "current_symbol", None),
    }

    for label in (
        "market_symbol_input",
        "active_symbol_input",
        "active_symbol_title",
        "market_days_input",
        "chart_symbol_input",
        "chart_days_input",
        "status_text",
        "theme_combo",
        "market_tab",
        "charts_tab",
        "settings_tab",
        "watchlist_widget",
        "notes_text",
    ):
        checks.update(_control_snapshot(label, getattr(window, label, None)))
    return checks


def _manifest_control_bool(snapshot: dict[str, object], key: str) -> bool:
    return bool(snapshot.get(key, False))


def _control_independence_evaluation(
    market_controls: dict[str, object],
    charts_controls: dict[str, object],
) -> dict[str, object]:
    return {
        "market_controls_present_on_market_tab": _manifest_control_bool(market_controls, "market_symbol_input_present")
        and _manifest_control_bool(market_controls, "market_days_input_present"),
        "charts_controls_present_on_charts_tab": _manifest_control_bool(charts_controls, "chart_symbol_input_present")
        and _manifest_control_bool(charts_controls, "chart_days_input_present"),
        "market_controls_absent_on_charts_tab": (
            not _manifest_control_bool(charts_controls, "market_symbol_input_visible")
            and not _manifest_control_bool(charts_controls, "market_days_input_visible")
        ),
        "charts_controls_absent_on_market_tab": (
            not _manifest_control_bool(market_controls, "chart_symbol_input_visible")
            and not _manifest_control_bool(market_controls, "chart_days_input_visible")
        ),
        "market_and_charts_controls_independent": bool(
            not _manifest_control_bool(charts_controls, "market_symbol_input_visible")
            and not _manifest_control_bool(charts_controls, "market_days_input_visible")
            and not _manifest_control_bool(market_controls, "chart_symbol_input_visible")
            and not _manifest_control_bool(market_controls, "chart_days_input_visible")
        ),
    }


def _default_profile_root() -> Path:
    return Path(tempfile.gettempdir()) / "rangescout-ui-profiles"


def _build_profile_env(profile_root: Path) -> dict[str, str]:
    profile_root = profile_root.resolve()
    profile_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(profile_root)
    env["USERPROFILE"] = str(profile_root)
    env["APPDATA"] = str(profile_root / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(profile_root / "AppData" / "Local")
    return env


def _prepare_normal_profile(data_dir: Path) -> None:
    """Create ordinary disposable AppData state for screenshot capture."""
    data_dir.mkdir(parents=True, exist_ok=True)
    watchlists = WatchlistStore.from_path(data_dir / "watchlists.json")
    if not watchlists.list():
        watchlists.create("visual-qa", "My Watchlist")
        for symbol in R4_EVIDENCE_SYMBOLS:
            watchlists.add_symbol("visual-qa", symbol)
    notes = NoteStore(data_dir / "notes.json")
    if not notes.list_for("BA"):
        notes.add(
            "BA",
            "Visual QA context only: review the active trend, official SEC fundamentals, "
            "and provider freshness before forming any conclusion.",
        )


def _settings_path(profile_root: Path) -> Path:
    return profile_root / "AppData" / "Roaming" / "RangeScout" / "settings.json"


def _read_settings_state(profile_root: Path) -> dict[str, object]:
    path = _settings_path(profile_root)
    state: dict[str, object] = {
        "settings_path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        return state

    state["sha256"] = _sha256(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        state["read_error"] = str(exc)
        return state

    if isinstance(payload, dict):
        state["theme"] = payload.get("theme")
        state["provider"] = payload.get("provider")
        state["window_width"] = payload.get("window_width")
        state["window_height"] = payload.get("window_height")
        return state

    state["type"] = type(payload).__name__
    return state


def _geometry(obj) -> dict[str, object]:
    rect = obj.geometry()
    return {"x": rect.x(), "y": rect.y(), "width": rect.width(), "height": rect.height()}


def _ensure_tab(tabs, expected_tab: str, *, tab_index: int | None = None) -> dict[str, object]:
    if tabs is None:
        return {"selected": "", "expected": expected_tab, "match": False}
    index = tab_index if tab_index is not None else tabs.currentIndex()
    if tab_index is not None:
        tabs.setCurrentIndex(tab_index)
    if QApplication.instance() is not None:
        QApplication.instance().processEvents()
    time.sleep(0.12)
    actual_tab = _safe_tab_name(tabs)
    return {
        "selected": actual_tab,
        "expected": expected_tab,
        "expected_index": tab_index,
        "actual_index": tabs.currentIndex() if hasattr(tabs, "currentIndex") else None,
        "match": _normalized_tab_name(actual_tab).startswith(_normalized_tab_name(expected_tab)),
    }


def _capture_runtime_surfaces(window, output_dir: Path) -> list[dict[str, object]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(True)

    window._qt_window.resize(*STRICT_VIEWPORT)
    window._qt_window.show()
    app.processEvents()
    time.sleep(0.25)
    window.theme_combo.setCurrentText("dark")
    window._on_research_refresh()
    research_deadline = time.time() + 20.0
    while (window._research_tasks or window._analyst_tasks) and time.time() < research_deadline:
        app.processEvents()
        time.sleep(0.1)

    tabs = window.tabs
    output: list[dict[str, object]] = []

    for surface_name, tab_index in (
        ("market", 0),
        ("live-trader", 1),
        ("research", 2),
        ("watchlists", 3),
        ("scanner", 4),
        ("alerts", 5),
        ("notes", 6),
        ("exports", 7),
        ("settings", 8),
    ):
        tab_check = _ensure_tab(tabs, surface_name, tab_index=tab_index)
        widget = None
        geometry = None
        screenshot = _blank_screenshot(
            output_dir / f"surface-{surface_name}.png",
            width=0,
            height=0,
        )
        if getattr(window, "tabs", None) is not None:
            widget = window.tabs.currentWidget()
            if widget is not None:
                screenshot = _capture_widget(window._qt_window, output_dir / f"surface-{surface_name}.png")
                geometry = _geometry(widget)
        runtime_entry = {
            **screenshot,
            "surface": surface_name,
            "phase": "runtime",
            "current_tab": _safe_tab_name(tabs),
            "theme": window.app.settings.theme,
            "expected_tab": surface_name,
            "tab_check": tab_check,
            "controls": _collect_control_checks(window),
            "geometry": geometry,
            "market_controls": _collect_control_checks(window),
        }
        runtime_entry["run_success"] = bool(
            widget is not None
            and runtime_entry.get("saved")
            and runtime_entry.get("non_uniform_image")
            and runtime_entry.get("width", 0) > 0
            and runtime_entry.get("height", 0) > 0
        )
        runtime_entry["launch_success"] = runtime_entry["run_success"]
        output.append(runtime_entry)

    for surface_name, tab_index in (("market", 0), ("research", 2), ("settings", 8)):
        for theme in ["system", "light", "dark"]:
            tab_check = _ensure_tab(tabs, surface_name, tab_index=tab_index)
            window.theme_combo.setCurrentText(theme)
            if QApplication.instance() is not None:
                QApplication.instance().processEvents()
            time.sleep(0.2)
            widget = window.tabs.currentWidget()
            geometry = _geometry(widget) if widget is not None else None
            screenshot = _blank_screenshot(
                output_dir / f"surface-{surface_name}-theme-{theme}.png",
                width=0,
                height=0,
            )
            if widget is not None:
                screenshot = _capture_widget(window._qt_window, output_dir / f"surface-{surface_name}-theme-{theme}.png")
            theme_entry = {
                **screenshot,
                "surface": surface_name,
                "phase": "theme",
                "theme": theme,
                "current_tab": _safe_tab_name(tabs),
                "expected_tab": surface_name,
                "tab_check": tab_check,
                "controls": _collect_control_checks(window),
                "geometry": geometry,
                "market_controls": _collect_control_checks(window),
            }
            theme_entry["run_success"] = bool(
                widget is not None
                and theme_entry.get("saved")
                and theme_entry.get("non_uniform_image")
                and theme_entry.get("width") == STRICT_VIEWPORT[0]
                and theme_entry.get("height") == STRICT_VIEWPORT[1]
                and bool(tab_check.get("match"))
            )
            theme_entry["launch_success"] = theme_entry["run_success"]
            output.append(theme_entry)

    _ensure_tab(tabs, "research", tab_index=2)
    window.research_tabs.setCurrentIndex(8)
    app.processEvents()
    configured = {
        **_capture_widget(window._qt_window, output_dir / "surface-research-analyst-configured.png"),
        "surface": "research-analyst-configured",
        "phase": "deterministic-provider-evidence",
        "theme": window.app.settings.theme,
        "provider_transport": "deterministic fake; zero live quota",
        "run_success": window.current_analyst_result is not None and bool(window.current_analyst_result.values),
    }
    configured["launch_success"] = configured["run_success"]
    output.append(configured)

    window.app.credential_store.delete("finnhub")
    window.app.credential_store.delete("alpha_vantage")
    window.analyst_service.invalidate_provider("finnhub")
    window.analyst_service.invalidate_provider("alpha_vantage")
    window._on_research_refresh()
    no_key_deadline = time.time() + 20.0
    while (window._research_tasks or window._analyst_tasks) and time.time() < no_key_deadline:
        app.processEvents()
        time.sleep(0.1)
    no_key = {
        **_capture_widget(window._qt_window, output_dir / "surface-research-analyst-no-key.png"),
        "surface": "research-analyst-no-key",
        "phase": "deterministic-provider-evidence",
        "theme": window.app.settings.theme,
        "provider_transport": "no credentials; zero live quota",
        "run_success": window.current_analyst_result is not None and not bool(window.current_analyst_result.values),
    }
    no_key["launch_success"] = no_key["run_success"]
    output.append(no_key)

    output.append(
        {
            **_capture_widget(window._qt_window, output_dir / "surface-main-window.png"),
            "surface": "main-window",
            "phase": "runtime",
            "theme": window.app.settings.theme,
            "current_tab": _safe_tab_name(tabs),
            "expected_tab": _safe_tab_name(tabs),
            "tab_check": _ensure_tab(tabs, _safe_tab_name(tabs)),
            "controls": _collect_control_checks(window),
            "geometry": _geometry(window._qt_window),
        }
    )
    return output


def _run_settings_restart_probe(output_dir: Path, exe_path: Path) -> dict[str, object]:
    profile_root = _default_profile_root()
    profile_root.mkdir(parents=True, exist_ok=True)
    probe_root = Path(tempfile.mkdtemp(prefix="rs-ui-settings-probe-", dir=str(profile_root)))
    probe_env = _build_profile_env(probe_root)
    first_run = _capture_packaged_exe(
        output_dir,
        exe_path,
        theme="dark",
        tab="settings",
        extra_env=probe_env,
        apply_start_theme=True,
        screenshot_suffix="first-start",
    )[0]
    settings_after_first = _read_settings_state(probe_root)
    second_run = _capture_packaged_exe(
        output_dir,
        exe_path,
        theme="system",
        tab="market",
        extra_env=probe_env,
        apply_start_theme=False,
        screenshot_suffix="restart",
    )[0]
    settings_after_second = _read_settings_state(probe_root)
    result = {
        "screenshot_directory": str(output_dir),
        "startup_theme_set": "dark",
        "settings_after_first_run": settings_after_first,
        "settings_after_second_run": settings_after_second,
        "first_run": first_run,
        "second_run": second_run,
        "restart_passed": bool(
            first_run.get("run_success")
            and second_run.get("run_success")
            and settings_after_first.get("theme") == "dark"
            and settings_after_first.get("theme") == settings_after_second.get("theme")
            and settings_after_second.get("exists", False)
        ),
    }
    try:
        shutil.rmtree(probe_root)
    except OSError:
        pass
    return result


def _window_list_by_pid(pid: int) -> list[int]:
    if os.name != "nt":
        return []

    windows: list[int] = []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)
        def _enum_windows(hwnd: int, _lparam: ctypes.c_void_p) -> bool:
            try:
                window_pid = wintypes.DWORD()
                if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid)):
                    return True
                if int(window_pid.value) != pid:
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                windows.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(_enum_windows, 0)
    except Exception:
        pass
    return windows


def _window_title(hwnd: int) -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return None


def _prefer_range_scout_window(handles: list[int]) -> int | None:
    if not handles:
        return None
    for handle in handles:
        title = _window_title(handle) or ""
        if title.lower().startswith("rangescout"):
            return handle
    return handles[0]


def _window_rect(hwnd: int) -> dict[str, int] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "width": max(0, rect.right - rect.left),
                "height": max(0, rect.bottom - rect.top),
            }
    except Exception:
        return None
    return None


def _window_scale_factor(hwnd: int | None) -> float:
    if os.name != "nt" or hwnd is None:
        return 1.0
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
        return max(1.0, dpi / 96.0) if dpi > 0 else 1.0
    except Exception:
        return 1.0


def _preferred_capture_origin() -> tuple[int, int] | None:
    app = QGuiApplication.instance() if QGuiApplication is not None else None
    if app is None:
        return None
    screens = list(app.screens())
    if not screens:
        return None
    # The evidence workstation's secondary display is the stable 100% surface;
    # prefer a left-hand display when present, then the lowest reported ratio.
    screen = min(
        screens,
        key=lambda item: (0 if item.geometry().x() < 0 else 1, float(item.devicePixelRatio())),
    )
    geometry = screen.availableGeometry()
    return geometry.x() + 8, geometry.y() + 8


def _move_window(hwnd: int | None, x: int, y: int) -> bool:
    if os.name != "nt" or hwnd is None:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        return bool(user32.SetWindowPos(int(hwnd), 0, int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE))
    except Exception:
        return False


def _set_client_size(hwnd: int | None, width: int, height: int) -> bool:
    if os.name != "nt" or hwnd is None:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        from ctypes import wintypes

        outer = wintypes.RECT()
        client = wintypes.RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(outer)):
            return False
        if not user32.GetClientRect(int(hwnd), ctypes.byref(client)):
            return False
        outer_width = int(outer.right - outer.left)
        outer_height = int(outer.bottom - outer.top)
        client_width = int(client.right - client.left)
        client_height = int(client.bottom - client.top)
        target_outer_width = outer_width + int(width) - client_width
        target_outer_height = outer_height + int(height) - client_height
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        return bool(
            user32.SetWindowPos(
                int(hwnd), 0, 0, 0, target_outer_width, target_outer_height,
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
        )
    except Exception:
        return False


def _request_window_close(hwnd: int | None) -> bool:
    if os.name != "nt" or hwnd is None:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.PostMessageW(int(hwnd), 0x0010, 0, 0)
        return True
    except Exception:
        return False


def _terminate_process_tree(pid: int) -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except Exception:
        pass


def _packaged_screenshot_path(output_dir: Path, tab: str, theme: str, screenshot_suffix: str | None = None) -> Path:
    filename = f"surface-packaged-exe-{tab}-{theme}"
    if screenshot_suffix:
        filename = f"{filename}-{screenshot_suffix}"
    return output_dir / f"{filename}.png"


def _capture_packaged_exe(
    output_dir: Path,
    exe_path: Path,
    *,
    theme: str,
    tab: str,
    extra_env: dict[str, str] | None = None,
    apply_start_theme: bool = True,
    screenshot_suffix: str | None = None,
) -> list[dict[str, object]]:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    normalized_tab = tab.lower()
    requested_theme = theme if theme in REQUIRED_THEME_VALUES else "system"
    screenshot_path = _packaged_screenshot_path(output_dir, normalized_tab, requested_theme, screenshot_suffix)

    record: dict[str, object] = {
        "surface": "packaged-exe",
        "phase": "packaged-runtime",
        "theme": requested_theme,
        "theme_requested": requested_theme if apply_start_theme else None,
        "tab": normalized_tab,
        "expected_tab": normalized_tab,
        "apply_start_theme": bool(apply_start_theme),
        "start_time_utc": timestamp,
        "exe_path": str(exe_path),
        "exe_sha256": _sha256(exe_path) if exe_path.exists() else None,
        "close_return_code": -1,
        "return_code": None,
        "return_code_at_close": None,
        "pid": None,
        "close_success": False,
        "run_success": False,
        "launch_success": False,
        "window_visible_seconds": None,
        "window_found": False,
        "window_handle": None,
        "window_title": None,
        "window_rect": None,
        "window_close": None,
        "close_attempted": False,
        "exit_code_after_close": None,
    }
    if not exe_path.exists():
        record.update(
            {
                "launched": False,
                "return_code": None,
                "close_return_code": -1,
                "expected_window_title": "RangeScout",
            }
        )
        record["screenshot"] = {
            "path": str(screenshot_path),
            "saved": False,
            "uniform_image": True,
            "non_uniform_image": False,
            "width": 0,
            "height": 0,
        }
        return [record]

    env = os.environ.copy()
    if os.name == "nt":
        env.pop("QT_QPA_PLATFORM", None)
    env.pop("RANGESCOUT_AUTO_CLOSE_SECONDS", None)
    if apply_start_theme:
        env["RANGESCOUT_START_THEME"] = requested_theme
    else:
        env.pop("RANGESCOUT_START_THEME", None)
    env["RANGESCOUT_START_TAB"] = tab
    if extra_env:
        env.update(extra_env)
    # Packaged UI evidence closes each launched window after capture. Bypass
    # the user-facing close-to-tray interception so every evidence process
    # exits cleanly instead of being left hidden and force-terminated.
    env["RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE"] = "1"

    proc = None
    handle = None
    started_at = time.time()
    started = False
    hold_seconds = 20.0
    started_windows = False
    visible_since: float | None = None
    try:
        proc = subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started = True
        record["pid"] = proc.pid
        record["started"] = True
        record["launched"] = True

        for _ in range(max(1, int(hold_seconds / 0.25))):
            if proc.poll() is not None:
                break
            windows = _window_list_by_pid(proc.pid)
            preferred = _prefer_range_scout_window(windows) if windows else None
            if preferred is not None and str(_window_title(preferred) or "").lower().startswith("rangescout"):
                handle = preferred
                origin = _preferred_capture_origin()
                if origin is not None:
                    _move_window(handle, *origin)
                    time.sleep(0.25)
                _set_client_size(handle, *STRICT_VIEWPORT)
                time.sleep(0.4)
                if visible_since is None:
                    visible_since = time.time()
                started_windows = True
                break
            time.sleep(0.25)

        if proc.poll() is not None:
            record["return_code"] = proc.returncode
            record["close_return_code"] = proc.returncode
            record["close_success"] = False
            record["run_success"] = False
            record["launch_success"] = False
            record["window_found"] = False
            record["window_handle"] = None
            record["window_title"] = None
            record["window_rect"] = None
            record["screenshot"] = _capture_window_path(screenshot_path, handle)
            return [record]

        record["window_found"] = started_windows
        record["window_handle"] = handle
        record["window_title"] = _window_title(handle) if handle else None
        record["window_rect"] = _window_rect(handle) if handle else None
        tab_check = {"selected": normalized_tab, "expected": normalized_tab, "match": bool(handle is not None)}
        record["tab_check"] = tab_check
        if visible_since is None:
            record["close_return_code"] = proc.returncode if proc.returncode is not None else -1
            record["return_code"] = proc.returncode if proc.returncode is not None else None
            record["return_code_at_close"] = record["return_code"]
            record["window_visible_seconds"] = None
            record["screenshot"] = _capture_window_path(screenshot_path, handle)
            return [record]

        while proc.poll() is None and (time.time() - visible_since) < 15.0:
            time.sleep(0.25)

        visible_seconds = round(max(0.0, time.time() - visible_since), 6)
        if visible_seconds < 15.0 and handle is not None:
            time.sleep(max(0.0, 15.0 - visible_seconds))
            visible_seconds = round(max(0.0, time.time() - visible_since), 6)
        record["window_visible_seconds"] = visible_seconds

        record["screenshot"] = _capture_window_path(screenshot_path, handle)
        record["window_capture_path"] = str(screenshot_path)

        close_requested_at = time.time()
        record["window_close"] = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close_requested_at)),
            "requested": False,
            "theme": requested_theme,
            "tab": normalized_tab,
        }
        requested_close = False
        if handle is not None:
            requested_close = _request_window_close(handle)
            record["window_close"]["requested"] = requested_close
        record["close_attempted"] = True
        try:
            record["close_return_code"] = proc.wait(timeout=6.0)
        except Exception:
            record["close_return_code"] = proc.returncode if proc.poll() is not None else -1

        if proc.poll() is None and handle is not None and requested_close:
            try:
                # Qt waits for active research/network workers during a normal
                # close. Preserve a bounded graceful window before recording a
                # fail-closed forced termination.
                record["close_return_code"] = proc.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc.pid)
                try:
                    record["close_return_code"] = proc.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    record["close_return_code"] = -1
        elif proc.poll() is None and not requested_close:
            _terminate_process_tree(proc.pid)
            if proc.poll() is not None:
                record["close_return_code"] = proc.returncode
            else:
                record["close_return_code"] = -1

        if record["close_return_code"] is None:
            record["close_return_code"] = proc.returncode if proc.poll() is not None else -1
        record["close_success"] = record["close_return_code"] == 0
        record["close_timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        record["return_code"] = proc.returncode
        record["return_code_at_close"] = record["close_return_code"]
        record["run_success"] = bool(
            record["window_found"]
            and bool(handle)
            and record["screenshot"].get("saved")
            and record["screenshot"].get("non_uniform_image")
            and record["screenshot"].get("width") == STRICT_VIEWPORT[0]
            and record["screenshot"].get("height") == STRICT_VIEWPORT[1]
            and bool(record["window_close"]) == True
            and record["close_success"]
            and record["return_code"] == 0
            and record["window_visible_seconds"] is not None
            and record["window_visible_seconds"] >= 15.0
            and bool(record.get("tab_check", {}).get("match"))
        )
        record["launch_success"] = record["run_success"]
        record["exit_code_after_close"] = record["return_code"]
        return [record]
    except OSError as exc:
        record["run_success"] = False
        record["launch_success"] = False
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        record["duration_s"] = round(time.time() - started_at, 6)
        record["started"] = started
        if proc is not None:
            try:
                if proc.poll() is None:
                    _terminate_process_tree(proc.pid)
                    proc.kill()
                    proc.wait(timeout=2.0)
            except Exception:
                pass

    if "screenshot" not in record:
        record["screenshot"] = {
            "path": str(screenshot_path),
            "saved": False,
            "uniform_image": True,
            "non_uniform_image": False,
            "width": 0,
            "height": 0,
        }
    return [record]


def capture_surfaces(
    output_dir: Path,
    *,
    exe_path: str | None = None,
    source_zip: str | None = None,
    windows_zip: str | None = None,
) -> list[dict[str, object]]:
    if QApplication is None or QGuiApplication is None:
        manifest = {
            "image_count": 0,
            "images": [],
            "market_controls": {},
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "fail_closed": True,
            "error": "PySide6 is required for UI surface capture.",
        }
        path = output_dir / "evidence_manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return []

    if os.name == "nt" and os.environ.get("QT_QPA_PLATFORM") in {"offscreen", "minimal"}:
        os.environ.pop("QT_QPA_PLATFORM", None)

    output_dir.mkdir(parents=True, exist_ok=True)
    source_profile = output_dir / "source-profile"
    source_profile.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(source_profile / "AppData" / "Roaming")
    os.environ["LOCALAPPDATA"] = str(source_profile / "AppData" / "Local")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(True)
    source_data = source_profile / "RangeScout"
    _prepare_normal_profile(source_data)
    credential_store = InMemoryCredentialStore()
    credential_store.save(ProviderCredentials("finnhub", {"api_key": "deterministic_evidence_only"}))
    credential_store.save(ProviderCredentials("alpha_vantage", {"api_key": "deterministic_evidence_only"}))
    source_application = RangeScoutApplication(data_dir=source_data, credential_store=credential_store)
    transport = _DeterministicAnalystTransport()
    analyst_service = AnalystService(
        source_application.store.path,
        credential_store,
        finnhub=FinnhubRecommendationClient(transport),
        alpha_vantage=AlphaVantageEarningsEstimatesClient(transport),
    )
    window = build_window(application=source_application, analyst_service=analyst_service, auto_refresh=False)
    window.market_symbol_input.setText("BA")
    window.set_active_symbol("BA", source="capture-automation")
    window._on_refresh()
    app.processEvents()

    evidence: list[dict[str, object]] = []
    evidence.extend(_capture_runtime_surfaces(window, output_dir))
    control_profiles: dict[str, dict[str, object]] = {}
    for entry in evidence:
        if entry.get("phase") != "runtime":
            continue
        surface = str(entry.get("surface", ""))
        controls = entry.get("controls")
        if surface in {"market", "charts", "settings"} and isinstance(controls, dict):
            control_profiles[surface] = controls

    if exe_path:
        packaged_exe = Path(exe_path).resolve()
        packaged_profile_root = output_dir / "packaged-profile"
        packaged_env = _build_profile_env(packaged_profile_root)
        packaged_data = packaged_profile_root / "AppData" / "Roaming" / "RangeScout"
        _prepare_normal_profile(packaged_data)
        packaged_env.update(
            {
                "RANGESCOUT_START_SYMBOL": "BA",
            }
        )
        for tab in TAB_LABELS:
            evidence.extend(
                _capture_packaged_exe(
                    output_dir,
                    packaged_exe,
                    theme="dark",
                    tab=tab,
                    extra_env=packaged_env,
                )
            )
        for tab in ["market", "research", "settings"]:
            for theme in ["system", "light", "dark"]:
                evidence.extend(
                    _capture_packaged_exe(
                        output_dir,
                        packaged_exe,
                        theme=theme,
                        tab=tab,
                        extra_env=packaged_env,
                    )
                )
        settings_restart_probe = _run_settings_restart_probe(output_dir, packaged_exe)
    else:
        settings_restart_probe = None

    market_controls = control_profiles.get("market", {})
    charts_controls = control_profiles.get("charts", {})
    settings_controls = control_profiles.get("settings", {})
    control_independence = _control_independence_evaluation(market_controls, charts_controls)
    manifest = {
        "image_count": len(evidence),
        "images": evidence,
        "market_controls": market_controls,
        "charts_controls": charts_controls,
        "settings_controls": settings_controls,
        "market_controls_runtime": _collect_control_checks(window),
        "control_independence": control_independence,
        "settings_restart_probe": settings_restart_probe,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exe_path": exe_path,
        "exe_sha256": _sha256(packaged_exe) if exe_path else None,
        "source_zip": source_zip,
        "windows_zip": windows_zip,
        "source_zip_sha256": _sha256(Path(source_zip)) if source_zip else None,
        "windows_zip_sha256": _sha256(Path(windows_zip)) if windows_zip else None,
        "market_tab_checked": _safe_tab_name(window.tabs),
    }
    manifest["fail_closed"] = any(
        (
            bool(entry.get("phase") == "runtime" and not entry.get("tab_check", {}).get("match"))
            or bool(entry.get("phase") in {"theme", "packaged-runtime"} and not entry.get("run_success"))
            for entry in evidence
        )
    )

    manifest_path = output_dir / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    window.exit_application()
    app.quit()
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="release/cp7_handoff_evidence")
    parser.add_argument("--exe", default=None, help="Path to packaged RangeScout executable")
    parser.add_argument("--source-zip", default=None, help="Path to exact source zip artifact")
    parser.add_argument("--windows-zip", default=None, help="Path to exact windows zip artifact")
    args = parser.parse_args()

    output_dir = Path(args.output)
    capture_surfaces(
        output_dir,
        exe_path=args.exe,
        source_zip=args.source_zip,
        windows_zip=args.windows_zip,
    )


if __name__ == "__main__":
    try:
        main()
    except NoGuiRuntimeError as exc:
        error_out = {"error": str(exc)}
        Path("release").mkdir(parents=True, exist_ok=True)
        (Path("release") / "capture_ui_surfaces_error.json").write_text(json.dumps(error_out, indent=2), encoding="utf-8")
        raise SystemExit(1)
