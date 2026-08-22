#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path


def _run_command(command: list[str] | str, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, object]:
    def _format_command(cmd_value: list[str]) -> str:
        return " ".join(
            part if not isinstance(part, str) else (f"\"{part}\"" if " " in part else part)
            for part in cmd_value
        )

    use_shell = False
    if isinstance(command, list):
        command_display = _format_command(command)
        cmd = list(command)
    else:
        command_display = command
        cmd = command
        use_shell = True

    started_at = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        shell=use_shell,
        env=env,
        check=False,
    )
    return {
        "command": command_display,
        "cwd": str(cwd) if cwd else None,
        "return_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_s": round(time.perf_counter() - started_at, 6),
    }


def _wait_for_process_exit(
    proc: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> int | None:
    if proc.poll() is not None:
        return proc.returncode
    end_time = time.perf_counter() + timeout_seconds
    while time.perf_counter() < end_time:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.05)
    return proc.poll() if proc.poll() is not None else None


def _safe_remove_directory(target_root: Path) -> dict[str, object]:
    result = {"attempted": False, "removed": False, "errors": []}
    if not target_root.exists():
        return result
    result["attempted"] = True
    try:
        shutil.rmtree(target_root)
        result["removed"] = not target_root.exists()
    except Exception as exc:
        result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        result["removed"] = not target_root.exists()
    return result


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _uninstall_passed(scenario: dict[str, object]) -> bool:
    target_existed = bool(scenario.get("target_exists_before_uninstall"))
    installed_uninstall_invoked = bool(scenario.get("installed_copy_uninstaller_invoked"))
    uninstall_step = scenario.get("uninstall_step")
    uninstall_step = uninstall_step if isinstance(uninstall_step, dict) else {}
    return_code = uninstall_step.get("return_code", 1)
    target_exists_after = bool(scenario.get("target_exists_after_uninstall"))
    remaining_files = scenario.get("remaining_files_after_uninstall")
    cleanup_required = bool(scenario.get("cleanup_required"))
    if isinstance(remaining_files, list):
        remaining_ok = not remaining_files
    else:
        remaining_ok = False
    try:
        return (
            target_existed
            and installed_uninstall_invoked
            and int(return_code) == 0
            and not target_exists_after
            and remaining_ok
            and not cleanup_required
        )
    except (TypeError, ValueError):
        return False


def _inventory_by_file_type(target_root: Path) -> dict[str, object]:
    if not target_root.exists():
        return {"exists": False, "files": [], "dirs": [], "file_count": 0, "dir_count": 0}

    files: list[str] = []
    dirs: list[str] = []
    for item in sorted(target_root.rglob("*")):
        rel = str(item.relative_to(target_root)).replace("\\", "/")
        if item.is_dir():
            dirs.append(rel)
        else:
            files.append(rel)
    return {
        "exists": True,
        "files": files,
        "dirs": dirs,
        "file_count": len(files),
        "dir_count": len(dirs),
    }


def _payload_inventory(target_root: Path) -> dict[str, dict[str, int | str]]:
    if not target_root.exists():
        return {}

    manifest: dict[str, dict[str, int | str]] = {}
    for item in sorted(target_root.rglob("*")):
        if not item.is_file():
            continue
        rel_path = str(item.relative_to(target_root)).replace("\\", "/")
        try:
            size = item.stat().st_size
        except OSError:
            continue
        manifest[rel_path] = {
            "size": size,
            "sha256": _sha256(item),
            "path": rel_path,
        }
    return manifest


def _verify_payload(
    expected: dict[str, dict[str, int | str]],
    installed: dict[str, dict[str, int | str]],
) -> dict[str, object]:
    expected_keys = set(expected)
    installed_keys = set(installed)
    missing = sorted(expected_keys - installed_keys)
    unexpected = sorted(installed_keys - expected_keys)
    size_mismatches: list[dict[str, object]] = []
    sha_mismatches: list[dict[str, object]] = []

    for path in sorted(expected_keys & installed_keys):
        expected_meta = expected[path]
        installed_meta = installed[path]
        if expected_meta["size"] != installed_meta["size"]:
            size_mismatches.append(
                {
                    "path": path,
                    "expected_size": expected_meta["size"],
                    "installed_size": installed_meta["size"],
                }
            )
        if expected_meta["sha256"] != installed_meta["sha256"]:
            sha_mismatches.append(
                {
                    "path": path,
                    "expected_sha256": expected_meta["sha256"],
                    "installed_sha256": installed_meta["sha256"],
                }
            )

    return {
        "expected_file_count": len(expected_keys),
        "actual_file_count": len(installed_keys),
        "missing_files": missing,
        "unexpected_files": unexpected,
        "size_mismatches": size_mismatches,
        "sha256_mismatches": sha_mismatches,
        "verified": not missing and not unexpected and not size_mismatches and not sha_mismatches,
    }


def _kill_range_scout_processes() -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "RangeScout.exe", "/T"],
            check=False,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NoLogo",
                "-NonInteractive",
                "-Command",
                "Get-Process -Name RangeScout -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
    except Exception:
        pass


def _kill_range_scout_for_target(target_root: Path) -> None:
    exe_path = target_root / "RangeScout.exe"
    if not exe_path.exists():
        return
    quoted = str(exe_path).replace("\\", "\\\\")
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NoLogo",
                "-NonInteractive",
                "-Command",
                f"Get-Process -Name RangeScout -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -ieq '{quoted}' }} | Stop-Process -Force -ErrorAction SilentlyContinue",
            ],
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except Exception:
        pass


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


def _prefer_range_scout_window(handles: list[int]) -> int | None:
    if not handles:
        return None
    for handle in handles:
        title = _window_title(handle) or ""
        if title.lower().startswith("rangescout"):
            return handle
    return handles[0]


def _window_title(hwnd: int) -> str | None:
    if os.name != "nt":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return None


def _is_bad_window_title(title: str | None) -> bool:
    if not title:
        return False
    lowered = title.lower()
    return (
        "unhandled exception in script" in lowered
        or "qwindows.dll" in lowered and "missing" in lowered
        or "qt platform plugin" in lowered
        or ("unhandled" in lowered and "error" in lowered)
    )


def _is_process_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NoLogo",
                "-NonInteractive",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id",
            ],
            text=True,
            capture_output=True,
            timeout=2.0,
        )
        return bool(result.stdout.strip())
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


def _kill_process_by_id(pid: int, *, path_hint: str | None = None) -> bool:
    del path_hint
    for command in (
        [
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
        ],
        [
            "powershell",
            "-NoProfile",
            "-NoLogo",
            "-Command",
            "Stop-Process",
            "-Id",
            str(pid),
            "-Force",
            "-ErrorAction",
            "SilentlyContinue",
        ],
    ):
        try:
            subprocess.run(command, check=False, capture_output=True, timeout=2.0)
        except Exception:
            pass
    return True


def _kill_all_installer_range_scout_processes() -> None:
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NoLogo",
                "-NonInteractive",
                "-Command",
                "Get-Process -Name RangeScout -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue",
            ],
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except Exception:
        pass


def _run_launch_attempt(
    exe_path: Path,
    *,
    hold_seconds: float = 20.0,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    if not exe_path.exists():
        return {
            "exe_path": str(exe_path),
            "exists": False,
            "started": False,
            "launch_success": False,
            "initially_running": False,
            "window_found": False,
            "launched": False,
            "duration_s": 0.0,
            "pid": None,
            "return_code": None,
            "close_return_code": None,
            "return_code_at_close": None,
            "window_handle": None,
            "window_title": None,
            "close_success": False,
            "run_success": False,
        }

    env = os.environ.copy()
    if os.name == "nt":
        env.pop("QT_QPA_PLATFORM", None)

    started_at = time.time()
    proc: subprocess.Popen[str] | None = None
    result: dict[str, object] = {
        "exe_path": str(exe_path),
        "exists": True,
        "started": False,
        "initially_running": False,
        "window_found": False,
        "launched": False,
        "window_seen": False,
        "duration_s": 0.0,
        "pid": None,
        "return_code": None,
        "close_return_code": None,
        "return_code_at_close": None,
        "window_handle": None,
        "window_title": None,
        "close_success": False,
        "launch_success": False,
        "run_success": False,
        "visibility_seconds": None,
        "close_attempted": False,
        "window_close": None,
    }

    try:
        env = os.environ.copy()
        if os.name == "nt":
            env.pop("QT_QPA_PLATFORM", None)
            env.pop("RANGESCOUT_AUTO_CLOSE_SECONDS", None)
            env["RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE"] = "1"
            env.setdefault("RANGESCOUT_START_THEME", "system")
            env.setdefault("RANGESCOUT_START_TAB", "market")
        if env_overrides:
            env.update(env_overrides)
        result["start_timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        proc = subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        result["started"] = True
        result["launched"] = True
        result["pid"] = proc.pid

        result["start_time_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at))
        handle = None
        visible_since: float | None = None
        for _ in range(max(1, int(hold_seconds / 0.25))):
            if proc.poll() is not None:
                break
            windows = _window_list_by_pid(proc.pid)
            preferred = _prefer_range_scout_window(windows) if windows else None
            if preferred is not None and str(_window_title(preferred) or "").lower().startswith("rangescout"):
                handle = preferred
                if visible_since is None:
                    visible_since = time.time()
                result["window_seen"] = True
                break
            if preferred is not None:
                handle = preferred
            time.sleep(0.25)

        result["window_title"] = _window_title(handle) if handle is not None else None
        if result["window_seen"] and result["visibility_seconds"] is None and handle is not None:
            result["visibility_seconds"] = round(max(0.0, time.time() - started_at), 6)

        result["window_found"] = handle is not None
        result["window_handle"] = handle
        result["window_title"] = _window_title(handle) if handle is not None else None
        result["initially_running"] = proc.poll() is None

        if proc.poll() is not None:
            result["return_code"] = proc.returncode
            result["return_code_at_close"] = proc.returncode
            result["run_success"] = False
            result["launch_success"] = False
            if visible_since is None:
                result["visibility_seconds"] = None
            else:
                result["visibility_seconds"] = round(max(0.0, time.time() - visible_since), 6)
            return result

        if visible_since is not None:
            while proc.poll() is None and (time.time() - visible_since) < 15.0:
                time.sleep(0.25)

        result["visibility_seconds"] = round(max(0.0, time.time() - visible_since), 6) if visible_since is not None else None
        result["close_attempted"] = True
        close_time = time.time()
        result["window_close"] = {
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(close_time)),
            "requested": False,
            "handle": handle,
        }
        if handle is not None:
            result["window_close"]["requested"] = _request_window_close(handle)
        result["close_return_code"] = _wait_for_process_exit(proc, timeout_seconds=8.0)
        if result["close_return_code"] is None and _is_process_alive(proc.pid):
            _kill_process_by_id(proc.pid)
            result["close_return_code"] = _wait_for_process_exit(proc, timeout_seconds=2.0)
        if result["close_return_code"] is None:
            result["close_return_code"] = proc.returncode
        result["close_success"] = result["close_return_code"] == 0
        result["close_timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        result["return_code"] = proc.returncode
        result["return_code_at_close"] = result["close_return_code"]
        if result["visibility_seconds"] is None:
            result["visibility_seconds"] = 0.0
        result["window_visible_for_seconds"] = result["visibility_seconds"]
        result["run_success"] = (
            bool(result["window_seen"])
            and bool(result["initially_running"])
            and bool(result["window_found"])
            and (str(result["window_title"] or "").lower().startswith("rangescout"))
            and not _is_bad_window_title(result["window_title"])
            and result["close_success"]
            and result["return_code_at_close"] == 0
            and result["visibility_seconds"] is not None
            and result["visibility_seconds"] >= 15.0
        )
        result["launch_success"] = result["run_success"]
        return result
    except OSError as exc:
        return {
            **result,
            "started": False,
            "return_code": None,
            "close_return_code": None,
            "return_code_at_close": None,
            "run_success": False,
            "launch_success": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
    finally:
        result["duration_s"] = round(time.time() - started_at, 6)
        if proc is not None and proc.poll() is None:
            try:
                if _is_process_alive(proc.pid):
                    _kill_process_by_id(proc.pid)
                proc.kill()
            except Exception:
                pass


def _find_package_root(extract_root: Path) -> Path:
    candidate_root = extract_root / "RangeScout-1.0.0-windows"
    if candidate_root.exists() and candidate_root.is_dir():
        return candidate_root

    for installer_name in ("install.bat", "installer.bat"):
        if (extract_root / installer_name).exists():
            return extract_root

    candidates = sorted((entry for entry in extract_root.iterdir() if entry.is_dir()), key=lambda item: item.name)
    if not candidates:
        return extract_root
    return extract_root / candidates[0]


def _select_installer_script(package_root: Path) -> tuple[Path, str]:
    for label in ("install.bat", "installer.bat"):
        script = package_root / label
        if script.exists():
            return script, label
    raise FileNotFoundError("No installer script exists in package")


def _select_uninstaller_script(package_root: Path) -> tuple[Path | None, str | None]:
    for label in ("uninstall.bat", "uninstall.ps1"):
        script = package_root / label
        if script.exists():
            return script, label
    return None, None


def _scenario_payload(
    package_root: Path,
    target_root: Path,
    install_script: Path,
    uninstall_script: Path | None,
    *,
    use_default_install: bool = False,
    install_parent: Path | None = None,
    no_arg_uninstall: bool = False,
    uninstall_environment: dict[str, str] | None = None,
    expected_payload_inventory: dict[str, dict[str, int | str]] | None = None,
) -> dict[str, object]:
    package_root = package_root.resolve()
    target_root = target_root.resolve()
    install_script = install_script.resolve()
    if uninstall_script is not None:
        uninstall_script = uninstall_script.resolve()
    if install_parent is not None:
        install_parent = install_parent.resolve()
    scenario: dict[str, object] = {
        "label": "default" if bool(use_default_install) else "spaced",
        "install_target": str(target_root),
        "default_install": bool(use_default_install),
        "no_arg_uninstall": bool(no_arg_uninstall),
        "scripts": {
            "install_script": str(install_script),
            "uninstall_script": str(uninstall_script) if uninstall_script else None,
        },
    }
    scenario["pre_install_inventory"] = _inventory_by_file_type(target_root)
    scenario["pre_cleanup"] = _safe_remove_directory(target_root)

    _kill_range_scout_processes()
    if target_root.exists():
        scenario["pre_target_pruned"] = _safe_remove_directory(target_root)

    if use_default_install:
        install_environment = os.environ.copy()
        base_target = install_parent if install_parent is not None else target_root.parent
        if "ProgramFiles" in install_environment:
            install_environment["ProgramFiles"] = str(base_target)
        else:
            install_environment["ProgramFiles"] = str(base_target)
        install_command = f"set \"ProgramFiles={str(base_target)}\" && \"{str(install_script)}\""
        scenario["install_step"] = _run_command(
            install_command,
            cwd=package_root,
            env=install_environment,
        )
        scenario["install_command"] = install_command
    else:
        scenario["install_step"] = _run_command([str(install_script), str(target_root)], cwd=package_root)
        scenario["install_command"] = [str(install_script), str(target_root)]
    scenario["post_install_inventory"] = _inventory_by_file_type(target_root)
    scenario["target_lengths"] = {
        "target_path": str(target_root),
        "target_length": len(str(target_root)),
        "target_parent_length": len(str(target_root.parent)),
    }

    expected_payload = expected_payload_inventory or _payload_inventory(package_root)
    scenario["expected_install_payload"] = expected_payload
    scenario["expected_payload_count"] = len(expected_payload)

    exe_candidates = [target_root / "RangeScout.exe", target_root / "RangeScout-1.0.0-windows" / "RangeScout.exe"]
    exe_path = next((item for item in exe_candidates if item.exists()), None)
    if exe_path is None:
        exe_path = next((item for item in target_root.rglob("RangeScout.exe")), None)

    if exe_path is None:
        scenario["installed_exe"] = {"path": str(target_root / "RangeScout.exe"), "exists": False, "sha256": None}
    else:
        scenario["installed_exe"] = {"path": str(exe_path), "exists": True, "sha256": _sha256(exe_path)}

    installed_payload = _payload_inventory(target_root)
    scenario["installed_payload_inventory"] = installed_payload
    scenario["installed_payload_verification"] = _verify_payload(expected_payload, installed_payload)
    scenario["installed_payload_verification_passed"] = bool(
        scenario["installed_payload_verification"]["verified"]
    )
    required_runtime_files = [
        "_internal/PySide6/Qt6Core.dll",
        "_internal/PySide6/Qt6Gui.dll",
        "_internal/PySide6/Qt6Widgets.dll",
        "_internal/PySide6/plugins/platforms/qwindows.dll",
    ]
    qwindows_present = installed_payload.get("_internal/PySide6/plugins/platforms/qwindows.dll") is not None
    scenario["required_runtime_presence"] = {
        path: path in installed_payload for path in required_runtime_files
    }
    scenario["critical_runtime_files_present"] = bool(qwindows_present)

    scenario["run_attempts"] = []
    first_attempt = None
    if exe_path is not None and scenario["installed_payload_verification_passed"] and bool(qwindows_present):
        first_attempt = _run_launch_attempt(exe_path, hold_seconds=20.0)
        scenario["run_attempts"].append(first_attempt)
        second_attempt = _run_launch_attempt(exe_path, hold_seconds=18.0)
        scenario["run_attempts"].append(second_attempt)
    scenario["run_restart_attempts"] = scenario["run_attempts"][1:] if len(scenario["run_attempts"]) > 1 else []
    scenario["run_success"] = all(attempt.get("run_success") for attempt in scenario["run_attempts"]) if scenario["run_attempts"] else False
    scenario["launch_success"] = bool(first_attempt and first_attempt.get("launch_success"))
    if uninstall_environment is not None:
        scenario["uninstall_environment"] = {
            key: uninstall_environment[key]
            for key in sorted(uninstall_environment.keys())
        }

    scenario["pre_uninstall_inventory"] = _inventory_by_file_type(target_root)
    scenario["uninstall_command"] = None
    scenario["target_exists_before_uninstall"] = target_root.exists()
    selected_uninstall = target_root / "uninstall.bat"
    if not selected_uninstall.exists():
        selected_uninstall = target_root / "uninstall.ps1"
        if not selected_uninstall.exists():
            selected_uninstall = None
    uninstall_script_is_copy = False
    if selected_uninstall is not None:
        uninstall_script_is_copy = selected_uninstall.parent.resolve() == target_root.resolve()
    uninstall_is_ps1 = False
    if selected_uninstall is not None:
        uninstall_is_ps1 = selected_uninstall.suffix.lower() == ".ps1"

    scenario["installed_uninstall_path"] = str(selected_uninstall) if selected_uninstall else None
    scenario["uninstall_entrypoint"] = selected_uninstall.name if selected_uninstall else None
    uninstall_cwd = (target_root.parent or package_root).resolve()
    if not uninstall_cwd.exists():
        uninstall_cwd = package_root

    if selected_uninstall is not None:
        scenario["scripts"]["uninstall_script"] = str(selected_uninstall)
        _kill_range_scout_for_target(target_root)
        if uninstall_is_ps1:
            uninstall_command = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-NoLogo",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(selected_uninstall),
                "-Target",
                str(target_root),
            ]
            scenario["uninstall_command"] = (
                " ".join(
                    part
                    if not isinstance(part, str)
                    else f"\"{part}\"" if " " in part else part
                    for part in uninstall_command
                )
                if isinstance(uninstall_command, list)
                else str(uninstall_command)
            )
            scenario["uninstall_step"] = _run_command(
                uninstall_command,
                cwd=uninstall_cwd,
                env=uninstall_environment,
            )
        elif no_arg_uninstall:
            uninstall_command = [str(selected_uninstall)]
            scenario["uninstall_command"] = (
                " ".join(
                    part
                    if not isinstance(part, str)
                    else f"\"{part}\"" if " " in part else part
                    for part in uninstall_command
                )
                if isinstance(uninstall_command, list)
                else str(uninstall_command)
            )
            scenario["uninstall_step"] = _run_command(
                uninstall_command,
                cwd=uninstall_cwd,
                env=uninstall_environment,
            )
        else:
            uninstall_command = [str(selected_uninstall), str(target_root)]
            scenario["uninstall_command"] = (
                " ".join(
                    part
                    if not isinstance(part, str)
                    else f"\"{part}\"" if " " in part else part
                    for part in uninstall_command
                )
                if isinstance(uninstall_command, list)
                else str(uninstall_command)
            )
            scenario["uninstall_step"] = _run_command(uninstall_command, cwd=uninstall_cwd)
    else:
        scenario["uninstall_step"] = {
            "command": None,
            "return_code": 127,
            "stdout": "",
            "stderr": "No uninstall script available.",
            "duration_s": 0.0,
            "cwd": str(package_root),
        }

    if scenario["target_exists_before_uninstall"]:
        end_time = time.time() + 5.0
        while target_root.exists() and time.time() < end_time:
            time.sleep(0.25)
    scenario["post_uninstall_inventory"] = _inventory_by_file_type(target_root)
    scenario["remaining_files_after_uninstall"] = scenario["post_uninstall_inventory"].get("files", [])
    scenario["target_exists_after_uninstall"] = target_root.exists()
    scenario["target_absent_after_wait"] = not target_root.exists()
    scenario["installed_copy_uninstaller_invoked"] = bool(
        selected_uninstall is not None and uninstall_script_is_copy
    )
    cleanup_result = _safe_remove_directory(target_root)
    scenario["cleanup"] = cleanup_result
    scenario["cleanup_inventory"] = _inventory_by_file_type(target_root)
    cleanup_attempted = bool(cleanup_result.get("attempted"))
    target_removed = bool(cleanup_result.get("removed"))
    scenario["cleanup_required"] = bool(cleanup_attempted and not target_removed)
    scenario["uninstall_return_code"] = scenario.get("uninstall_step", {}).get("return_code")
    scenario["uninstall_passed"] = _uninstall_passed(scenario)

    _kill_range_scout_processes()
    return scenario


def collect_installer_evidence(
    release_zip: Path,
    output_dir: Path,
    *,
    install_target: str | None = None,
    source_zip: str | None = None,
    harness_path: str | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)

    source_zip_path = Path(source_zip).resolve() if source_zip else None
    release_zip_path = release_zip.resolve()
    harness_path_obj = Path(harness_path).resolve() if harness_path else Path(__file__).resolve()
    source_sha256 = _sha256(source_zip_path) if source_zip_path and source_zip_path.exists() else None
    uninstall_bat_sha256: str | None = None
    exe_sha256: str | None = None

    tmp_root = (output_dir / "_tmp_package_extract").resolve()
    if tmp_root.exists():
        _safe_remove_directory(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(release_zip_path, "r") as archive:
        archive.extractall(tmp_root / "package")

    extract_root = (tmp_root / "package").resolve()
    package_root = _find_package_root(extract_root)
    install_script, _ = _select_installer_script(package_root)
    uninstall_script, _ = _select_uninstaller_script(package_root)
    expected_payload_inventory = _payload_inventory(package_root)

    install_root = Path(install_target).resolve() if install_target else Path(r"C:\RSQA\CP19")
    install_root.mkdir(parents=True, exist_ok=True)
    default_root = install_root / "Default"
    default_target = (default_root / "RangeScout").resolve()
    scenario_targets = [
        {
            "label": "default",
            "target": default_target,
            "use_default_install": True,
            "install_parent": default_root,
            "no_arg_uninstall": True,
        },
        {
            "label": "spaced",
            "target": (install_root / "Space Path" / "RangeScout").resolve(),
            "use_default_install": False,
            "install_parent": None,
            "no_arg_uninstall": False,
        },
    ]

    scenarios: list[dict[str, object]] = []
    for scenario_plan in scenario_targets:
        target_root = scenario_plan["target"]
        uninstall_environment = None
        if scenario_plan.get("no_arg_uninstall"):
            uninstall_environment = os.environ.copy()
            uninstall_parent = scenario_plan.get("install_parent")
            if uninstall_parent is not None:
                uninstall_environment["ProgramFiles"] = str(uninstall_parent)
                uninstall_environment["PROGRAMFILES"] = str(uninstall_parent)
            else:
                uninstall_environment["ProgramFiles"] = os.environ.get("ProgramFiles", "")
                uninstall_environment["PROGRAMFILES"] = os.environ.get("PROGRAMFILES", os.environ.get("ProgramFiles", ""))
        scenarios.append(
            _scenario_payload(
                package_root,
                target_root.resolve(),
                install_script,
                uninstall_script,
                use_default_install=bool(scenario_plan["use_default_install"]),
                install_parent=scenario_plan.get("install_parent"),
                no_arg_uninstall=bool(scenario_plan["no_arg_uninstall"]),
                uninstall_environment=uninstall_environment,
                expected_payload_inventory=expected_payload_inventory,
            )
        )
    package_uninstall_bat = package_root / "uninstall.bat"
    if package_uninstall_bat.exists():
        uninstall_bat_sha256 = _sha256(package_uninstall_bat)
    package_exe = package_root / "RangeScout.exe"
    if package_exe.exists():
        exe_sha256 = _sha256(package_exe)

    record = {
        "package": str(release_zip_path),
        "package_root": str(package_root),
        "package_extract_root": str(extract_root),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_zip": str(source_zip_path) if source_zip_path else None,
        "source_zip_sha256": source_sha256,
        "windows_zip": str(release_zip_path),
        "windows_zip_sha256": _sha256(release_zip_path),
        "installer_evidence_harness": str(harness_path_obj),
        "installer_evidence_harness_sha256": _sha256(harness_path_obj),
        "uninstall_bat_sha256": uninstall_bat_sha256,
        "exe_sha256": exe_sha256,
        "package_extract_root_length": len(str(extract_root)),
        "installer_root": str(install_root),
        "installer_root_length": len(str(install_root)),
        "environment": {
            "cwd": str(Path.cwd()),
            "python": os.environ.get("PYTHON", "python"),
            "platform": os.name,
            "path": os.environ.get("PATH"),
        },
        "installer_scenarios": scenarios,
    }
    record["scenario_count"] = len(scenarios)
    record["all_targets_uninstalled"] = all(
        bool(scenario.get("uninstall_passed"))
        for scenario in scenarios
    )
    record["all_launch_runs_closed_cleanly"] = all(
        bool(scenario.get("run_success")) for scenario in scenarios
    )
    record["overall_success"] = bool(record["all_targets_uninstalled"] and record["all_launch_runs_closed_cleanly"])

    evidence_file = output_dir / "installer_evidence.json"
    evidence_file.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    _safe_remove_directory(tmp_root / "package")
    _safe_remove_directory(tmp_root)
    return {"evidence_file": str(evidence_file), "payload": record}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_zip")
    parser.add_argument("--output", default="release/cp7_handoff_evidence")
    parser.add_argument("--install-target", default=None)
    parser.add_argument("--source-zip", default=None)
    parser.add_argument("--harness", default=None)
    args = parser.parse_args()

    result = collect_installer_evidence(
        Path(args.release_zip),
        Path(args.output),
        install_target=args.install_target,
        source_zip=args.source_zip,
        harness_path=args.harness,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
