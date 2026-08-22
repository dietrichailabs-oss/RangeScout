#!/usr/bin/env python
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

WM_CLOSE = 0x0010
WM_GETICON = 0x007F
ICON_SMALL = 0
ICON_BIG = 1
ICON_SMALL2 = 2
GCLP_HICON = -14
GCLP_HICONSM = -34


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _windows_for_pid(pid: int) -> list[dict[str, Any]]:
    if sys.platform != "win32":
        raise RuntimeError("Tray runtime evidence requires Windows.")
    user32 = ctypes.windll.user32
    records: list[dict[str, Any]] = []

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(handle: int, _parameter: int) -> bool:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        if int(process_id.value) != pid:
            return True
        length = int(user32.GetWindowTextLengthW(handle))
        buffer = ctypes.create_unicode_buffer(max(length + 1, 2))
        user32.GetWindowTextW(handle, buffer, len(buffer))
        records.append(
            {
                "handle": int(handle),
                "title": buffer.value,
                "visible": bool(user32.IsWindowVisible(handle)),
                "enabled": bool(user32.IsWindowEnabled(handle)),
            }
        )
        return True

    if not user32.EnumWindows(callback, 0):
        raise OSError("EnumWindows failed")
    return records



def _window_icon_handles(handle: int) -> dict[str, int]:
    user32 = ctypes.windll.user32
    values = {
        "wm_geticon_small": int(user32.SendMessageW(handle, WM_GETICON, ICON_SMALL, 0) or 0),
        "wm_geticon_big": int(user32.SendMessageW(handle, WM_GETICON, ICON_BIG, 0) or 0),
        "wm_geticon_small2": int(user32.SendMessageW(handle, WM_GETICON, ICON_SMALL2, 0) or 0),
    }
    get_class_long = getattr(user32, "GetClassLongPtrW", None) or getattr(user32, "GetClassLongW", None)
    if callable(get_class_long):
        values["class_icon_big"] = int(get_class_long(handle, GCLP_HICON) or 0)
        values["class_icon_small"] = int(get_class_long(handle, GCLP_HICONSM) or 0)
    return values

def _wait_for_main_window(pid: int, timeout_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = _windows_for_pid(pid)
        visible = [row for row in last if row["visible"] and "RangeScout" in row["title"]]
        if visible:
            return visible[0]
        time.sleep(0.2)
    raise RuntimeError(f"RangeScout main window did not become visible. windows={last}")


def collect(exe: Path, output: Path, *, launch_timeout_seconds: float = 30.0) -> dict[str, Any]:
    if sys.platform != "win32":
        raise RuntimeError("Tray runtime evidence requires Windows.")
    exe = exe.resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    profile = Path(tempfile.mkdtemp(prefix="RangeScoutTrayProbe-"))
    environment = os.environ.copy()
    environment.pop("RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE", None)
    environment.pop("RANGESCOUT_AUTO_CLOSE_SECONDS", None)
    environment["USERPROFILE"] = str(profile)
    environment["APPDATA"] = str(profile / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(profile / "AppData" / "Local")
    Path(environment["APPDATA"]).mkdir(parents=True, exist_ok=True)
    Path(environment["LOCALAPPDATA"]).mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=environment)
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    close_posted = False
    alive_after_close = False
    hidden_after_close = False
    window_icon_handles: dict[str, int] = {}
    try:
        main_window = _wait_for_main_window(process.pid, launch_timeout_seconds)
        window_icon_handles = _window_icon_handles(int(main_window["handle"]))
        before = _windows_for_pid(process.pid)
        close_posted = bool(ctypes.windll.user32.PostMessageW(main_window["handle"], WM_CLOSE, 0, 0))
        if not close_posted:
            raise OSError("PostMessageW(WM_CLOSE) failed")
        time.sleep(2.5)
        alive_after_close = process.poll() is None
        after = _windows_for_pid(process.pid) if alive_after_close else []
        hidden_after_close = alive_after_close and not any(
            row["visible"] and "RangeScout" in row["title"] for row in after
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=8)

    payload = {
        "schema": "rangescout.tray-runtime-evidence.v1",
        "executable": {
            "path": str(exe),
            "size": exe.stat().st_size,
            "sha256": _sha256(exe),
        },
        "process_id": process.pid,
        "windows_before_close": before,
        "window_icon_handles": window_icon_handles,
        "window_icon_present": any(window_icon_handles.values()),
        "wm_close_posted": close_posted,
        "process_alive_after_titlebar_close": alive_after_close,
        "main_window_hidden_after_close": hidden_after_close,
        "windows_after_close": after,
        "cleanup_exit_code": process.returncode,
        "overall_pass": close_posted and alive_after_close and hidden_after_close and any(window_icon_handles.values()),
        "scope_note": (
            "This probe proves that a real packaged RangeScout process survives WM_CLOSE and hides its "
            "visible main window. Deterministic Qt tests separately verify tray Open/Exit actions and icon identity."
        ),
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Prove packaged RangeScout close-to-tray behavior on Windows.")
    parser.add_argument("exe", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--launch-timeout", type=float, default=30.0)
    args = parser.parse_args()
    payload = collect(args.exe, args.output, launch_timeout_seconds=args.launch_timeout)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
