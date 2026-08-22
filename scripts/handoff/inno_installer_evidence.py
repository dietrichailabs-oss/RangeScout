#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import winreg
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.handoff.installer_evidence import _run_launch_attempt
from app import PRODUCT


INNO_UNINSTALLER_NAMES = {"unins000.exe", "unins000.dat", "unins000.msg"}
UNINSTALL_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{4A97E81B-630D-4A27-B60B-9004B61D69F3}_is1"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path, *, exclude_inno_uninstaller: bool = False) -> dict[str, dict[str, int | str]]:
    rows: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if exclude_inno_uninstaller and Path(relative).name.lower() in INNO_UNINSTALLER_NAMES:
            continue
        rows[relative] = {"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
    return rows


def _verify(expected: dict[str, dict[str, int | str]], actual: dict[str, dict[str, int | str]]) -> dict[str, object]:
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    mismatched = sorted(
        path for path in set(expected) & set(actual)
        if expected[path]["size"] != actual[path]["size"]
        or expected[path]["sha256"] != actual[path]["sha256"]
    )
    return {
        "pass": not missing and not unexpected and not mismatched,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": missing,
        "unexpected": unexpected,
        "mismatched": mismatched,
    }


def _run(command: list[str]) -> dict[str, object]:
    started = time.perf_counter()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    return {
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "duration_s": round(time.perf_counter() - started, 3),
    }


def _wait_absent(path: Path, timeout_seconds: float = 30.0) -> bool:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        if not path.exists():
            return True
        time.sleep(0.25)
    return not path.exists()


def _wait_for_allowed_files(
    path: Path,
    allowed: set[str],
    timeout_seconds: float = 30.0,
) -> list[str]:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        if not path.exists():
            return []
        remaining = sorted(
            item.relative_to(path).as_posix()
            for item in path.rglob("*")
            if item.is_file()
        )
        if set(remaining) <= allowed:
            return remaining
        time.sleep(0.25)
    if not path.exists():
        return []
    return sorted(
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file()
    )


def _registry_value(root: int, subkey: str, name: str) -> str | None:
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            continue
    return None


def _accepted_display_name(value: str | None) -> bool:
    """Accept Inno's explicit per-user suffix without relaxing product identity."""
    return value in {
        f"RangeScout {PRODUCT.version}",
        f"RangeScout {PRODUCT.version} (Current user)",
    }


def _shell_registration(target: Path) -> dict[str, object]:
    shortcut = (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "RangeScout.lnk"
    )
    display_name = _registry_value(winreg.HKEY_CURRENT_USER, UNINSTALL_SUBKEY, "DisplayName")
    display_version = _registry_value(winreg.HKEY_CURRENT_USER, UNINSTALL_SUBKEY, "DisplayVersion")
    install_location = _registry_value(winreg.HKEY_CURRENT_USER, UNINSTALL_SUBKEY, "InstallLocation")
    build_identity = _registry_value(
        winreg.HKEY_CURRENT_USER,
        r"Software\RangeScout",
        "BuildIdentity",
    )
    return {
        "start_menu_shortcut": str(shortcut),
        "start_menu_shortcut_exists": shortcut.is_file(),
        "uninstall_registry_key": rf"HKCU\{UNINSTALL_SUBKEY}",
        "display_name": display_name,
        "display_version": display_version,
        "install_location": install_location,
        "build_identity": build_identity,
        "pass": (
            shortcut.is_file()
            and _accepted_display_name(display_name)
            and display_version == PRODUCT.version
            and install_location is not None
            and Path(install_location).resolve() == target.resolve()
            and build_identity == PRODUCT.build_identity
        ),
    }


def collect_evidence(
    *,
    setup: Path,
    portable: Path,
    output: Path,
    default_target: Path,
    spaced_target: Path,
    upgrade_target: Path,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    extract_root = output / "portable_extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)
    with zipfile.ZipFile(portable) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("portable ZIP CRC failure")
        archive.extractall(extract_root)
    expected = _inventory(extract_root)
    isolated_home = output / "isolated_user"
    isolated_roaming = isolated_home / "AppData" / "Roaming"
    isolated_local = isolated_home / "AppData" / "Local"
    isolated_roaming.mkdir(parents=True, exist_ok=True)
    isolated_local.mkdir(parents=True, exist_ok=True)
    appdata_sentinel = isolated_roaming / "RangeScout" / "preserve-me.txt"
    appdata_sentinel.parent.mkdir(parents=True, exist_ok=True)
    appdata_sentinel.write_text("preserve RangeScout user data", encoding="utf-8")
    launch_environment = {
        "USERPROFILE": str(isolated_home),
        "APPDATA": str(isolated_roaming),
        "LOCALAPPDATA": str(isolated_local),
    }
    portable_launch = _run_launch_attempt(
        extract_root / "RangeScout.exe",
        hold_seconds=20.0,
        env_overrides=launch_environment,
    )

    scenarios: list[dict[str, object]] = []
    plans = (
        ("default", default_target, False, False),
        ("spaced", spaced_target, True, False),
        ("upgrade", upgrade_target, True, True),
    )
    for label, target, explicit_dir, simulate_upgrade in plans:
        if target.exists():
            raise RuntimeError(f"refusing to overwrite existing install target: {target}")
        user_export = target / "user-export.csv"
        stale_runtime = target / "_internal" / "legacy-runtime.dll"
        if simulate_upgrade:
            stale_runtime.parent.mkdir(parents=True, exist_ok=True)
            stale_runtime.write_bytes(b"legacy runtime must be removed during upgrade")
            (target / "RangeScout.exe").write_bytes(b"MZlegacy")
            user_export.write_text("symbol,price\nUSER,1.00\n", encoding="utf-8")
        command = [
            str(setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/CURRENTUSER",
        ]
        if explicit_dir:
            command.append(f"/DIR={target}")
        install_log = output / f"{label}_install.log"
        command.append(f"/LOG={install_log}")
        install = _run(command)
        actual_target = target
        installed = _inventory(actual_target, exclude_inno_uninstaller=True) if actual_target.exists() else {}
        if simulate_upgrade:
            installed.pop("user-export.csv", None)
        parity = _verify(expected, installed)
        shell_registration = _shell_registration(actual_target)
        upgrade_preservation = {
            "simulated": simulate_upgrade,
            "stale_runtime_removed": not stale_runtime.exists() if simulate_upgrade else None,
            "user_export_preserved_after_install": (
                user_export.is_file()
                and user_export.read_text(encoding="utf-8") == "symbol,price\nUSER,1.00\n"
            ) if simulate_upgrade else None,
        }
        launch = _run_launch_attempt(
            actual_target / "RangeScout.exe",
            hold_seconds=20.0,
            env_overrides=launch_environment,
        ) if actual_target.exists() else {"run_success": False}
        uninstaller = actual_target / "unins000.exe"
        uninstall = _run([
            str(uninstaller),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
        ]) if uninstaller.is_file() else {"return_code": -1, "stderr": "uninstaller missing"}
        shell_registration_removed = not _shell_registration(actual_target)["start_menu_shortcut_exists"] and (
            _registry_value(winreg.HKEY_CURRENT_USER, UNINSTALL_SUBKEY, "DisplayName") is None
        )
        if simulate_upgrade:
            remaining = _wait_for_allowed_files(actual_target, {"user-export.csv"})
            absent = not actual_target.exists()
            export_preserved_after_uninstall = (
                user_export.is_file()
                and user_export.read_text(encoding="utf-8") == "symbol,price\nUSER,1.00\n"
            )
            uninstall_state_pass = export_preserved_after_uninstall and remaining == ["user-export.csv"]
        else:
            absent = _wait_absent(actual_target)
            remaining = [
                path.relative_to(actual_target).as_posix()
                for path in actual_target.rglob("*")
                if path.is_file()
            ] if actual_target.exists() else []
            export_preserved_after_uninstall = None
            uninstall_state_pass = absent and not remaining
        scenario_pass = (
            install.get("return_code") == 0
            and parity["pass"] is True
            and shell_registration["pass"] is True
            and launch.get("run_success") is True
            and uninstall.get("return_code") == 0
            and uninstall_state_pass
            and shell_registration_removed
            and (not simulate_upgrade or upgrade_preservation["stale_runtime_removed"] is True)
            and (not simulate_upgrade or upgrade_preservation["user_export_preserved_after_install"] is True)
        )
        scenarios.append({
            "label": label,
            "target": str(actual_target),
            "install": install,
            "payload_parity": parity,
            "shell_registration": shell_registration,
            "upgrade_preservation": upgrade_preservation,
            "launch": launch,
            "installed_uninstaller": str(uninstaller),
            "uninstall": uninstall,
            "shell_registration_removed_after_uninstall": shell_registration_removed,
            "target_absent_after_uninstall": absent,
            "user_export_preserved_after_uninstall": export_preserved_after_uninstall,
            "remaining_files_after_uninstall": remaining,
            "pass": scenario_pass,
        })

    payload = {
        "schema": "rangescout.inno-installer-evidence.v1",
        "setup": {"path": str(setup), "size": setup.stat().st_size, "sha256": _sha256(setup)},
        "portable": {"path": str(portable), "size": portable.stat().st_size, "sha256": _sha256(portable)},
        "portable_crc_pass": True,
        "portable_runtime_file_count": len(expected),
        "isolated_user_profile": str(isolated_home),
        "appdata_sentinel": str(appdata_sentinel),
        "appdata_preserved_after_uninstall": (
            appdata_sentinel.is_file()
            and appdata_sentinel.read_text(encoding="utf-8") == "preserve RangeScout user data"
        ),
        "portable_launch": portable_launch,
        "scenarios": scenarios,
        "overall_pass": (
            portable_launch.get("run_success") is True
            and all(row["pass"] for row in scenarios)
            and appdata_sentinel.is_file()
            and appdata_sentinel.read_text(encoding="utf-8") == "preserve RangeScout user data"
        ),
    }
    (output / "inno_installer_evidence.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("setup")
    parser.add_argument("portable")
    parser.add_argument("--output", required=True)
    parser.add_argument("--default-target", required=True)
    parser.add_argument("--spaced-target", required=True)
    parser.add_argument("--upgrade-target", required=True)
    args = parser.parse_args()
    payload = collect_evidence(
        setup=Path(args.setup).resolve(),
        portable=Path(args.portable).resolve(),
        output=Path(args.output).resolve(),
        default_target=Path(args.default_target).resolve(),
        spaced_target=Path(args.spaced_target).resolve(),
        upgrade_target=Path(args.upgrade_target).resolve(),
    )
    print(json.dumps(payload, indent=2))
    raise SystemExit(0 if payload["overall_pass"] else 1)


if __name__ == "__main__":
    main()
