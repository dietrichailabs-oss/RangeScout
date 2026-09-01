#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import ssl
import struct
import subprocess
import sys
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - fallback for older runtimes
    import tomli as tomllib  # type: ignore[import-not-found]

from app import PRODUCT
from scripts.build_manifest import build_manifest_records
from scripts.stage_release import stage_release
from scripts.handoff.verify_staged_source_regression import verify_staged_source_contract

ZIP_FIXED_DATETIME = (1980, 1, 1, 0, 0, 0)
PACKAGING_SKIP_DIRS = {"__pycache__", ".pytest_cache", "work", "spec"}
PACKAGING_SKIP_FILE_SUFFIXES = {".pyc", ".pyc.bak", ".pyc~"}
PACKAGING_SKIP_FILENAMES = {".DS_Store"}
CHECKSUM_MANIFEST_NAME = "SHA256SUMS.txt"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERIFIER_NAME = "MANIFEST_VERIFICATION.json"
MANIFEST_DETAILS_NAME = "manifest_details.json"
FORBIDDEN_RUNTIME_COMPONENT_TOKENS = (
    "qt6virtualkeyboard.dll",
    "qtvirtualkeyboardplugin.dll",
)
MANIFEST_ENTRY_EXCLUSIONS = {
    CHECKSUM_MANIFEST_NAME,
    MANIFEST_NAME,
    MANIFEST_VERIFIER_NAME,
    MANIFEST_DETAILS_NAME,
}
RELEASE_PYTHON = (3, 14, 6)
RELEASE_DISTRIBUTIONS = {
    "PySide6": "6.11.1",
    "PySide6-Addons": "6.11.1",
    "PySide6-Essentials": "6.11.1",
    "shiboken6": "6.11.1",
    "PyInstaller": "6.21.0",
    "pyinstaller-hooks-contrib": "2026.6",
}
PROHIBITED_QT_SHADOW_LIBRARIES = ("icuuc.dll",)
PE_MACHINE_AMD64 = 0x8664


def _is_windows_pe_executable(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(2)
        return header == b"MZ"
    except OSError:
        return False


def _assert_pinned_release_environment() -> dict[str, object]:
    actual_python = tuple(sys.version_info[:3])
    if actual_python != RELEASE_PYTHON:
        raise RuntimeError(
            f"Windows release build requires CPython {'.'.join(map(str, RELEASE_PYTHON))} "
            f"x64, found {'.'.join(map(str, actual_python))}."
        )
    if struct.calcsize("P") != 8:
        raise RuntimeError("Windows release build requires a 64-bit Python interpreter.")
    versions: dict[str, str] = {}
    for distribution, expected in RELEASE_DISTRIBUTIONS.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"Missing pinned release dependency: {distribution}=={expected}") from exc
        if actual != expected:
            raise RuntimeError(
                f"Release dependency mismatch: {distribution}=={actual}; expected {expected}."
            )
        versions[distribution] = actual
    return {
        "python": ".".join(map(str, actual_python)),
        "architecture": "x64",
        "distributions": versions,
    }


def _pe_machine(path: Path) -> int:
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise RuntimeError(f"Not a Windows PE file: {path}")
        handle.seek(0x3C)
        pe_offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise RuntimeError(f"Invalid Windows PE signature: {path}")
        return struct.unpack("<H", handle.read(2))[0]


def _windows_file_version(path: Path) -> str:
    environment = os.environ.copy()
    environment["RANGESCOUT_AUDIT_FILE"] = str(path)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-Item -LiteralPath $env:RANGESCOUT_AUDIT_FILE).VersionInfo.FileVersion",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=environment,
    )
    return completed.stdout.strip()


def _remove_prohibited_qt_shadow_libraries(runtime_root: Path) -> dict[str, object]:
    removed: list[dict[str, object]] = []
    for name in PROHIBITED_QT_SHADOW_LIBRARIES:
        for path in sorted(runtime_root.rglob(name)):
            if not path.is_file():
                continue
            removed.append(
                {
                    "path": path.relative_to(runtime_root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _file_sha256(path),
                    "file_version": _windows_file_version(path),
                    "reason": "shadows the Windows ICU compatibility DLL required by Qt6Core",
                }
            )
            path.unlink()
    remaining = sorted(
        path.relative_to(runtime_root).as_posix()
        for name in PROHIBITED_QT_SHADOW_LIBRARIES
        for path in runtime_root.rglob(name)
        if path.is_file()
    )
    if remaining:
        raise RuntimeError(f"Prohibited Qt shadow libraries remain: {remaining}")
    if not removed:
        raise RuntimeError(
            "Expected PyInstaller ICU shadow library was not observed; dependency collection changed."
        )
    return {"removed": removed, "remaining": remaining}

def _critical_qt_runtime_files(runtime_root: Path) -> list[Path]:
    internal = runtime_root / "_internal"
    candidates = [
        runtime_root / "RangeScout.exe",
        internal / "PySide6" / "Qt6Core.dll",
        internal / "PySide6" / "Qt6Gui.dll",
        internal / "PySide6" / "Qt6Widgets.dll",
        internal / "PySide6" / "Qt6Network.dll",
        internal / "PySide6" / "Qt6WebSockets.dll",
        internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
    ]
    candidates.extend(sorted((internal / "PySide6").glob("QtCore*.pyd")))
    candidates.extend(sorted((internal / "shiboken6").glob("Shiboken*.pyd")))
    candidates.extend(sorted(internal.glob("python*.dll")))
    missing = [str(path.relative_to(runtime_root)) for path in candidates if not path.is_file()]
    if missing:
        raise RuntimeError("Critical packaged Qt/Python runtime file missing: " + ", ".join(missing))
    return candidates


def _run_packaged_qt_smoke(runtime_root: Path) -> dict[str, object]:
    profile = runtime_root.parent / "_qt_runtime_smoke_profile"
    _safe_remove(profile)
    (profile / "AppData" / "Roaming").mkdir(parents=True)
    (profile / "AppData" / "Local").mkdir(parents=True)
    environment = os.environ.copy()
    environment.update(
        {
            "APPDATA": str(profile / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(profile / "AppData" / "Local"),
            "QT_QPA_PLATFORM": "offscreen",
            "RANGESCOUT_AUTO_CLOSE_SECONDS": "2",
            "RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE": "1",
        }
    )
    completed = subprocess.run(
        [str(runtime_root / "RangeScout.exe")],
        cwd=str(runtime_root),
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )
    result = {
        "exit_code": completed.returncode,
        "qtcore": "imported during packaged startup",
        "qtwidgets": "imported during packaged startup",
        "qtwebsockets": "imported through app.streaming.qt_transport",
        "pass": completed.returncode == 0,
    }
    _safe_remove(profile)
    if not result["pass"]:
        raise RuntimeError(
            f"Packaged Qt import/launch smoke failed with exit code {completed.returncode}: "
            f"{completed.stderr[-1000:]}"
        )
    return result


def _write_qt_runtime_audit(
    runtime_root: Path,
    *,
    environment: dict[str, object],
    shadow_libraries: dict[str, object],
    smoke: dict[str, object],
) -> Path:
    critical_rows: list[dict[str, object]] = []
    for path in _critical_qt_runtime_files(runtime_root):
        machine = _pe_machine(path)
        if machine != PE_MACHINE_AMD64:
            raise RuntimeError(
                f"Non-x64 packaged runtime component: {path} machine=0x{machine:04x}"
            )
        critical_rows.append(
            {
                "path": path.relative_to(runtime_root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _file_sha256(path),
                "file_version": _windows_file_version(path),
                "pe_machine": "AMD64",
            }
        )
    qt6core = sorted(path.relative_to(runtime_root).as_posix() for path in runtime_root.rglob("Qt6Core.dll"))
    qtcore_pyd = sorted(path.relative_to(runtime_root).as_posix() for path in runtime_root.rglob("QtCore*.pyd"))
    if qt6core != ["_internal/PySide6/Qt6Core.dll"]:
        raise RuntimeError(f"Duplicate/conflicting Qt6Core runtime: {qt6core}")
    if len(qtcore_pyd) != 1 or not qtcore_pyd[0].startswith("_internal/PySide6/"):
        raise RuntimeError(f"Duplicate/conflicting QtCore extension: {qtcore_pyd}")
    payload = {
        "schema": "rangescout.qt-runtime-audit.v1",
        "status": "PASS",
        "version": PRODUCT.version,
        "build_identity": PRODUCT.build_identity,
        "build_environment": environment,
        "shadow_library_removal": shadow_libraries,
        "critical_runtime": critical_rows,
        "qt6core_locations": qt6core,
        "qtcore_extension_locations": qtcore_pyd,
        "packaged_import_smoke": smoke,
    }
    output = runtime_root / "notices" / "QT_RUNTIME_AUDIT.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output


def _build_timestamp(source_root: Path | None = None) -> str:
    if source_root is None:
        source_root = Path(__file__).resolve().parents[1]
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
        except (OSError, ValueError, TypeError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _relative_manifest_target(target_dir: Path) -> str:
    return target_dir.name


def _runtime_forbidden_components_present(payload_root: Path) -> list[Path]:
    def _matches_forbidden(candidate: Path) -> bool:
        lowered = str(candidate).replace("\\", "/").lower()
        return any(
            token in lowered or lowered.endswith(f"/{token}")
            for token in FORBIDDEN_RUNTIME_COMPONENT_TOKENS
        )

    matches: list[Path] = []
    if not payload_root.exists():
        return matches
    for candidate in payload_root.rglob("*"):
        if not candidate.is_file():
            continue
        if _matches_forbidden(candidate):
            matches.append(candidate)
    return matches


def _remove_forbidden_runtime_components(payload_root: Path) -> list[Path]:
    removed: list[Path] = []
    if not payload_root.exists():
        return removed
    forbidden = _runtime_forbidden_components_present(payload_root)
    for artifact in forbidden:
        try:
            artifact.unlink()
            removed.append(artifact)
        except OSError:
            try:
                if artifact.is_dir():
                    shutil.rmtree(artifact)
                else:
                    raise
            except Exception:
                raise RuntimeError(f"Failed to remove forbidden runtime component: {artifact}") from None
    return removed


def _ensure_forbidden_runtime_components_absent(payload_root: Path) -> None:
    remaining = _runtime_forbidden_components_present(payload_root)
    if remaining:
        raise RuntimeError(
            "Forbidden Qt Virtual Keyboard runtime components remain: "
            + ", ".join(str(path.relative_to(payload_root)) for path in remaining)
        )


def _merge_component_entries(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_priority = {
        "runtime": 80,
        "runtime-package": 60,
        "bundled-payload": 40,
        "executable-payload": 20,
        "unknown": 0,
    }

    def _normalize_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name:
            return ""
        lowered = name.lower()
        if lowered in {"python", "python3", "python-3"}:
            return "Python"
        if lowered == "openssl":
            return "OpenSSL"
        return name

    def _normalize_version(value: Any) -> str:
        return str(value).strip() if str(value or "").strip() else "unknown"

    def _normalize_license(value: Any) -> str:
        return str(value).strip() if str(value or "").strip() else "unknown"

    def _normalize_source(value: Any) -> str:
        return str(value or "").strip() or "unknown"

    def _normalize_path(value: Any) -> str | None:
        value = str(value or "").strip()
        return value or None

    def _normalize_sha256(value: Any) -> str | None:
        value = str(value or "").strip()
        if value and value.lower() == "unknown":
            return None
        return value or None

    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        normalized = {
            "name": _normalize_name(item.get("name")),
            "version": _normalize_version(item.get("version")),
            "license": _normalize_license(item.get("license")),
            "source": _normalize_source(item.get("source")),
            "path": _normalize_path(item.get("path")),
            "sha256": _normalize_sha256(item.get("sha256")),
            "notice_file": str(item.get("notice_file", "notices/DEPENDENCY_LICENSES.md")),
        }
        if not normalized["name"]:
            continue

        key = (normalized["name"].lower(), normalized.get("path") or "")
        prior = seen.get(key)
        if prior is None:
            seen[key] = normalized
            continue

        if prior.get("version") == "unknown" and normalized["version"] != "unknown":
            prior["version"] = normalized["version"]
        if prior.get("license") == "unknown" and normalized["license"] != "unknown":
            prior["license"] = normalized["license"]

        if normalized.get("path") and not prior.get("path"):
            prior["path"] = normalized["path"]
        if normalized.get("sha256") and not prior.get("sha256"):
            prior["sha256"] = normalized["sha256"]
        if normalized.get("notice_file") and not prior.get("notice_file"):
            prior["notice_file"] = normalized["notice_file"]

        prior_source = str(prior.get("source", "unknown")).lower()
        normalized_source = normalized["source"].lower()
        if source_priority.get(normalized_source, 0) > source_priority.get(prior_source, 0):
            prior["source"] = normalized["source"]
    return sorted(
        seen.values(),
        key=lambda row: (
            str(row.get("name", "")).lower(),
            str(row.get("path") or ""),
            str(row.get("version", "")),
        ),
    )


def _safe_append_components(
    records: list[dict[str, Any]],
    name: str,
    source: str,
    version: str | None,
    license_value: str | None,
    *,
    path: str | None = None,
    sha256: str | None = None,
) -> None:
    if not name:
        return
    item = {
        "name": name,
        "version": version,
        "license": license_value,
        "source": source,
        "path": path,
        "sha256": sha256,
        "notice_file": "notices/DEPENDENCY_LICENSES.md",
    }
    if item not in records:
        records.append(item)


def _qt_runtime_license() -> str:
    return "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"


def _public_readme_text() -> str:
    return f"""# RangeScout {PRODUCT.version}

Thank you for installing RangeScout.

This package is a self-contained Windows application.
Run RangeScout from `run.bat` (double-click) or `run.ps1`.

RangeScout stores user data in:
- `%AppData%\\RangeScout` (or equivalent per-user app-data location):
  - `settings.json`
  - `watchlists.json`
  - `notes.json`
  - `history.sqlite`
- `%AppData%\\RangeScout\\temp` for temporary files
- no persistent log file is written by default.

Default install location is `%ProgramFiles%\\RangeScout`.

Yahoo Finance is the default live provider. It requires network access and may return delayed data.
Finnhub streaming is optional and uses a credential supplied by the current user.
Official SEC research follows the global Active Symbol and records source, filing date, units, and selection rationale.

## Files

- `RangeScout.exe` - main application
- `README.md` - quick user documentation
- `README.txt` - basic release identity
- `run.bat` / `run.ps1` - launch helpers
- `install.bat` / `install.ps1` - install to a target folder
- `uninstall.bat` - remove the installed package
- `notices/` - dependency licenses and SBOM

## Notes

- The app reads/writes user data from the `%AppData%\\RangeScout` family.
- Uninstall removes files in the install target only; per-user data in `%AppData%\\RangeScout` is retained.
- For a fresh install in the default path, use `install.bat` with no arguments.
- To remove the install directory only, run `uninstall.bat "C:\\Path\\To\\Install"` from a terminal.
"""


def _public_readme_text_txt() -> str:
    return f"""RangeScout {PRODUCT.version}
Run via run.bat or run.ps1.
Yahoo Finance is the default live provider; Finnhub streaming is optional with your own credential.
Use install.bat [target] for explicit install path.
Use uninstall.bat [target] to remove the install directory.
User data remains under %AppData%\\RangeScout by default.
Default install target is %ProgramFiles%\\RangeScout.
Uninstall removes installation files only; per-user data in %AppData%\\RangeScout is retained.
No persistent log file is written by default.
"""


def _safe_module_version(module_name: str) -> str | None:
    try:
        return importlib.metadata.version(module_name)
    except Exception:
        pass

    try:
        module = __import__(module_name)
        return getattr(module, "__version__", None)
    except Exception:
        return None


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _zip_dir(
    source_dir: Path,
    zip_path: Path,
    include_root_name: bool = False,
    *,
    skip_dirs: set[str] | None = None,
    skip_file_suffixes: set[str] | None = None,
    skip_filenames: set[str] | None = None,
) -> None:
    if not source_dir.exists():
        raise FileNotFoundError(f"source directory not found: {source_dir}")

    skip_dirs = skip_dirs or set()
    skip_file_suffixes = skip_file_suffixes or set()
    skip_filenames = skip_filenames or set()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in sorted(source_dir.rglob("*")):
            if not entry.is_file():
                continue
            rel = entry.relative_to(source_dir)
            if any(part in skip_dirs for part in rel.parts):
                continue
            if any(entry.name.endswith(suffix) for suffix in skip_file_suffixes):
                continue
            if entry.name in skip_filenames:
                continue

            arc_name = rel
            if include_root_name:
                arc_name = Path(source_dir.name) / rel
            info = zipfile.ZipInfo(str(arc_name).replace("\\", "/"), date_time=ZIP_FIXED_DATETIME)
            with entry.open("rb") as source:
                archive.writestr(info, source.read())


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path)


def _safe_remove(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _safe_manifest_rows(target_dir: Path, *, exclusions: set[str] | None = None) -> list[dict[str, Any]]:
    exclusions = exclusions or set()
    rows: list[dict[str, Any]] = []
    for row in build_manifest_records(target_dir):
        rel_path = row.path.replace("\\", "/")
        if any(part in PACKAGING_SKIP_DIRS for part in rel_path.split("/")):
            continue
        if rel_path in exclusions:
            continue
        if any(row.path.endswith(suffix) for suffix in PACKAGING_SKIP_FILE_SUFFIXES):
            continue
        if rel_path.startswith("__pycache__/"):
            continue
        rows.append({"path": rel_path, "size": row.size, "sha256": row.sha256})
    return sorted(rows, key=lambda row: row["path"])


def _verify_manifest(
    target_dir: Path,
    records: list[dict[str, Any]],
    *,
    checksum_file: str,
    extra_excluded_paths: set[str] | None = None,
) -> dict[str, Any]:
    excluded = {checksum_file, MANIFEST_NAME, MANIFEST_VERIFIER_NAME, MANIFEST_DETAILS_NAME}
    if extra_excluded_paths:
        excluded.update(extra_excluded_paths)

    expected_paths = sorted(
        [
            str(path.relative_to(target_dir)).replace("\\", "/")
            for path in sorted(target_dir.rglob("*"))
            if path.is_file()
            and str(path.relative_to(target_dir)).replace("\\", "/") not in excluded
            and not any(part in PACKAGING_SKIP_DIRS for part in str(path.relative_to(target_dir)).replace("\\", "/").split("/"))
            and not path.name.startswith(".")
        ]
    )
    manifest_paths = sorted(row["path"] for row in records)
    missing = [path for path in expected_paths if path not in manifest_paths]
    extra = [path for path in manifest_paths if path not in expected_paths]
    duplicate = sorted(
        path
        for path, count in {path: manifest_paths.count(path) for path in manifest_paths}.items()
        if count > 1
    )

    return {
        "generated_utc": _build_timestamp(target_dir),
        "target": _relative_manifest_target(target_dir),
        "checksum_file": checksum_file,
        "manifested_file_count": len(manifest_paths),
        "expected_file_count": len(expected_paths),
        "covered_exactly_once": not missing and not extra and not duplicate,
        "missing": missing,
        "extra": extra,
        "duplicate": duplicate,
    }


def _write_checksum_manifest(target_dir: Path, records: list[dict[str, Any]]) -> None:
    checksum_path = target_dir / CHECKSUM_MANIFEST_NAME
    lines = [f"{row['sha256']} {row['path']}" for row in sorted(records, key=lambda item: item['path'])]
    checksum_path.write_text("\n".join(lines), encoding="utf-8")


def _write_manifest(target_dir: Path, build_timestamp: str, records: list[dict[str, Any]]) -> Path:
    manifest_path = target_dir / MANIFEST_NAME
    payload = {
        "generated_utc": build_timestamp,
        "build_identity": PRODUCT.build_identity,
        "version": PRODUCT.version,
        "record_count": len(records),
        "entries": records,
        "checksum_file": CHECKSUM_MANIFEST_NAME,
        "exclusions": sorted(MANIFEST_ENTRY_EXCLUSIONS),
    }
    manifest_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return manifest_path


def _write_detailed_manifest(target_dir: Path) -> None:
    manifest_path = target_dir / "manifest_details.json"
    records = [
        {
            "path": row.path,
            "size": row.size,
            "sha256": row.sha256,
        }
        for row in build_manifest_records(target_dir)
        if not any(part in PACKAGING_SKIP_DIRS for part in row.path.split("/"))
        and not any(row.path.endswith(suffix) for suffix in PACKAGING_SKIP_FILE_SUFFIXES)
    ]
    manifest_payload = {"files": records}
    manifest_path.write_text(json.dumps(manifest_payload, sort_keys=True, indent=2), encoding="utf-8")


def _load_runtime_requirements(source_root: Path) -> dict[str, str | None]:
    requirements: dict[str, str | None] = {}
    pyproject = source_root / "pyproject.toml"
    if not pyproject.exists():
        return requirements

    with pyproject.open("rb") as handle:
        data = tomllib.loads(handle.read().decode("utf-8"))
    dependencies = data.get("project", {}).get("dependencies", []) or []
    for item in dependencies:
        name = str(item).split(">=")[0].split("<")[0].split("=")[0].strip()
        name = name.strip()
        if name and name.lower() != "python":
            requirements[name] = None
    requirements.update({
        "PySide6": None,
        "shiboken6": None,
        "PyInstaller": None,
    })
    return requirements


def _lookup_package_metadata(name: str) -> tuple[str | None, str | None]:
    try:
        normalized = name.replace("-", "_")
        dist = importlib.metadata.distribution(normalized)
        version = dist.version
        metadata_text = dist.read_text("METADATA")
        license_value = None
        if metadata_text:
            for line in metadata_text.splitlines():
                if line.startswith("License:"):
                    license_value = line.split(":", 1)[1].strip() or None
                    break
        return version, license_value
    except Exception:
        return None, None


def _collect_dependency_inventory() -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    inventory.append({
        "name": "Python",
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "license": "Python Software Foundation",
        "source": "runtime",
    })
    openssl_version = None
    try:
        openssl_version = ssl.OPENSSL_VERSION.split()[1]
    except Exception:
        openssl_version = None
    if openssl_version:
        inventory.append({
            "name": "OpenSSL",
            "version": openssl_version,
            "license": "Apache 2.0 / dual",
            "source": "runtime",
        })

    try:
        from PySide6.QtCore import __version__ as qt_version

        inventory.append(
            {
                "name": "QtCore",
                "version": qt_version,
                "license": "LGPL-3.0 / GPL-3.0 alternatives",
                "source": "runtime",
            }
        )
    except Exception:
        pass
    return inventory


def _payload_path_records(payload_root: Path) -> list[dict[str, Any]]:
    def _normalize_path(path: Path) -> str:
        return str(path.relative_to(payload_root).as_posix())

    def _license_for(path: str, package: str | None) -> str:
        if "python" in path or "python" in (package or "").lower():
            return "Python Software Foundation"
        if "openssl" in (path or "") or package in {"libssl", "libcrypto", "openssl"}:
            return "Apache 2.0"
        if "sqlite" in (path or ""):
            return "Public Domain"
        if "zstd" in path:
            return "BSD License"
        if "zlib" in path or "libz" in path:
            return "zlib License"
        if "msvc" in path or "vcruntime" in path:
            return "Microsoft Visual C++ Redistributable"
        if "qt" in path or "pyside" in path or "shiboken" in path:
            return _qt_runtime_license()
        return "Various"

    def _component_name(path: str, package: str | None) -> str:
        normalized = path.replace("\\", "/").lower()
        if package:
            if package == "python3.dll":
                return "Python Runtime"
            if package.startswith("python"):
                return f"Python {package}"
            if package == "libssl-3.dll":
                return "OpenSSL"
            if package == "libcrypto-3.dll":
                return "OpenSSL"
            if "vcruntime" in package or "msvc" in package:
                return "MSVC Runtime"
            if "sql" in package and "sqlite" in package:
                return "SQLite"
            if package == "libffi-8.dll":
                return "libffi Runtime"
            if package == "libzstd.dll" or package == "libzstd-static.dll":
                return "Zstandard"
            if "PySide6" in package:
                return f"PySide6 {package.split('.')[-2] if '.' in package else package}"
        if normalized.endswith(".pyd") or normalized.endswith(".dll") or ".dll/" in normalized:
            if "qt6" in normalized and package:
                return package.replace("_", " ")
            if "pyside6" in normalized:
                return package.replace("_", " ")
            if "shiboken" in normalized:
                return "Shiboken"
        if "translations/qt" in normalized:
            return "Qt Translations"
        return package or "Bundled Runtime"

    def _version_from_filename(name: str) -> str:
        if "python3" in name:
            return "3"
        parts = name.split("-")
        for part in parts:
            if "." in part and part[0].isdigit():
                return part
        for part in parts:
            if all(ch.isdigit() or ch == "." for ch in part) and part.count(".") >= 1:
                return part
        return "unknown"

    records: list[dict[str, Any]] = []
    if not payload_root.exists():
        return records

    for target in sorted(payload_root.rglob("*")):
        if target.is_dir():
            continue
        if any(
            token in str(target).replace("\\", "/").lower()
            for token in FORBIDDEN_RUNTIME_COMPONENT_TOKENS
        ):
            continue
        name = target.name
        lower = name.lower()
        if lower.endswith(".tmp") or lower == "MANIFEST":
            continue
        if lower.endswith(".txt") and not any(marker in lower for marker in (".py",)):
            continue
        if lower in {".ds_store", "thumbs.db", "manifest.json", "manifest_details.json", "MANIFEST_VERIFICATION.json"}:
            continue
        if not (
            lower.endswith(".dll")
            or lower.endswith(".pyd")
            or lower.endswith(".zip")
            or lower.endswith(".exe")
            or lower.startswith(("python", "lib"))
            or "openssl" in lower
            or "sqlite" in lower
            or "shiboken" in lower
            or "pyside6" in lower
            or "qt6" in lower
            or lower in {"base_library.zip"}
            or "_zstd" in lower
            or "zlib" in lower
            or "libffi" in lower
            or "vcruntime" in lower
            or "msvc" in lower
        ):
            continue

        rel = _normalize_path(target)
        size = target.stat().st_size
        if size == 0:
            continue

        family = _component_name(rel, name)
        version = "unknown"
        if "PySide6" in name or "shiboken6" in name:
            version = _lookup_package_metadata("PySide6")[0] or _lookup_package_metadata("shiboken6")[0] or "unknown"
        elif lower.startswith("python") and lower.endswith(".dll"):
            version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        elif lower == "libffi-8.dll":
            version = "8"
        elif "libssl" in lower or "libcrypto" in lower:
            version = openssl_version = None
            try:
                openssl_version = ssl.OPENSSL_VERSION.split()[1]
            except Exception:
                openssl_version = None
            if openssl_version:
                version = openssl_version
        elif lower.startswith("sqlite"):
            version = "3"
        else:
            parsed = _version_from_filename(name)
            if parsed != "unknown":
                version = parsed

        if name.lower().endswith((".py", ".pyc", ".pyi")):
            continue

        records.append({
            "name": family,
            "version": version,
            "license": _license_for(rel.lower(), name.lower()),
            "source": "bundled-payload",
            "path": rel,
            "sha256": _file_sha256(target),
        })

    return records


def _payload_has_qt_component(payload_components: list[dict[str, Any]], module_name: str) -> bool:
    needle = "".join(ch for ch in module_name.lower() if ch.isalnum())
    for component in payload_components:
        path_value = str(component.get("path", "")).lower()
        name_value = str(component.get("name", "")).lower()
        path_token = "".join(ch for ch in path_value if ch.isalnum())
        name_token = "".join(ch for ch in name_value if ch.isalnum())
        if needle in path_token or needle in name_token:
            return True
    return False


def _collect_dependency_inventory_from_executable(exe_path: Path, generated_utc: str) -> list[dict[str, Any]]:
    del generated_utc
    inventory = _collect_dependency_inventory()
    payload_components = _payload_path_records(exe_path.parent)

    pyinstaller_version, _ = _lookup_package_metadata("PyInstaller")
    if pyinstaller_version:
        _safe_append_components(inventory, "PyInstaller", "runtime-package", pyinstaller_version, _lookup_package_metadata("PyInstaller")[1])

    qt_version = None
    try:
        from PySide6.QtCore import __version__ as qt_module_version

        qt_version = qt_module_version
    except Exception:
        qt_version = _safe_module_version("PySide6")

    for name in [
        "QtCore",
        "QtGui",
        "QtNetwork",
        "QtOpenGL",
        "QtPdf",
        "QtQml",
        "QtQuick",
        "QtSvg",
        "QtWidgets",
    ]:
        if _payload_has_qt_component(payload_components, name):
            _safe_append_components(inventory, name, "runtime", qt_version, _qt_runtime_license())

    for component in payload_components:
        _safe_append_components(
            inventory,
            component.get("name", "Bundled Runtime"),
            component.get("source", "bundled-payload"),
            component.get("version"),
            component.get("license"),
            path=component.get("path"),
            sha256=component.get("sha256"),
        )

    return _merge_component_entries(inventory)


def _write_dependency_notice_files(target_dir: Path, source_root: Path, generated_utc: str) -> None:
    notices_dir = target_dir / "notices"
    notices_dir.mkdir(exist_ok=True)

    requirements = _load_runtime_requirements(source_root)
    exe_path = target_dir / "RangeScout.exe"
    if exe_path.exists():
        sbom_items = _collect_dependency_inventory_from_executable(exe_path, generated_utc)
    else:
        sbom_items = _collect_dependency_inventory()

    for name in sorted(requirements):
        version, license_name = _lookup_package_metadata(name)
        _safe_append_components(
            sbom_items,
            name,
            "runtime-package",
            version,
            license_name,
        )

    sbom_items = _merge_component_entries(sbom_items)

    source_manifest_path = source_root / "docs" / "QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json"
    if not source_manifest_path.exists():
        raise RuntimeError(f"Missing required notice artifact: {source_manifest_path}")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    mapped_components = {
        str(component)
        for asset in source_manifest.get("source_assets", [])
        for component in asset.get("runtime_sbom_mapping", [])
    }
    source_mapped_runtime_names = {
        "PySide6",
        "shiboken6",
        "QtCore",
        "QtGui",
        "QtNetwork",
        "QtOpenGL",
        "QtPdf",
        "QtQml",
        "QtQuick",
        "QtSvg",
        "QtWidgets",
    }
    shipped_source_components = {
        str(item.get("name"))
        for item in sbom_items
        if str(item.get("name")) in source_mapped_runtime_names
    }
    missing_source_mappings = sorted(shipped_source_components - mapped_components)
    if missing_source_mappings:
        raise RuntimeError(
            "Qt corresponding-source manifest does not map shipped SBOM components: "
            + ", ".join(missing_source_mappings)
        )

    lines = ["# Dependency licenses", ""]
    for item in sorted(sbom_items, key=lambda row: row["name"].lower()):
        lines.append(f"- {item['name']}: version={item.get('version', 'unknown')} | license={item.get('license', 'unknown')}")

    (notices_dir / "DEPENDENCY_LICENSES.md").write_text("\n".join(lines), encoding="utf-8")
    _write_qt_compliance_notices(notices_dir, source_root)
    (notices_dir / "SBOM.json").write_text(
        json.dumps(
            {
                "generated_utc": generated_utc,
                "product": PRODUCT.name,
                "version": PRODUCT.version,
                "build": PRODUCT.build_identity,
                "packages": sorted(
                    sbom_items,
                    key=lambda row: (str(row.get("name", "")).lower()),
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_qt_compliance_notices(notices_dir: Path, source_root: Path) -> None:
    shipped_qt_version = "unknown"
    try:
        from PySide6.QtCore import __version__ as qt_version

        shipped_qt_version = qt_version
    except Exception:
        shipped_qt_version = _safe_module_version("PySide6") or "unknown"
    qt_commercial_notice = (
        "RangeScout includes Qt/PySide6 runtime components.\n"
        f"Shipped Qt version: {shipped_qt_version}\n"
        "This package provides the required prominent notice for LGPLv3 components.\n"
        "Recipients may inspect, modify, rebuild, replace, and relink the LGPL libraries.\n"
        "This package excludes Qt Virtual Keyboard modules.\n"
        "Qt is distributed under the Qt Company licensing terms.\n"
        "For full Qt commercial license terms, see https://www.qt.io/terms-conditions.\n"
    )
    (notices_dir / "QT_PYSIDE6_NOTICE.md").write_text(qt_commercial_notice, encoding="utf-8")

    license_source_paths = [
        source_root / "docs" / "LGPL-3.0.txt",
        source_root / "docs" / "QT_SOURCE_INSTRUCTIONS.md",
        source_root / "docs" / "QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json",
        source_root / "docs" / "MARKET_DATA_NOTICE.md",
        source_root / "docs" / "PRIVACY_AND_DATA_USE.md",
    ]
    for source in license_source_paths:
        destination = notices_dir / source.name
        if source.exists():
            shutil.copy2(source, destination)
            continue
        raise RuntimeError(f"Missing required notice artifact: {source}")


def _write_public_readme_files(target_dir: Path) -> None:
    (target_dir / "README.md").write_text(_public_readme_text(), encoding="utf-8")
    (target_dir / "README.txt").write_text(_public_readme_text_txt(), encoding="utf-8")


def _windows_version_text() -> str:
    version_parts = tuple(int(part) for part in PRODUCT.version.split("."))
    if len(version_parts) != 3:
        raise RuntimeError(f"Windows metadata requires a three-part version: {PRODUCT.version}")
    version = (*version_parts, 0)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version!r},
    prodvers={version!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {PRODUCT.company!r}),
         StringStruct('FileDescription', 'RangeScout'),
         StringStruct('FileVersion', {PRODUCT.version!r}),
         StringStruct('InternalName', 'RangeScout'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 {PRODUCT.company}'),
         StringStruct('OriginalFilename', 'RangeScout.exe'),
         StringStruct('ProductName', 'RangeScout'),
         StringStruct('ProductVersion', {PRODUCT.version!r})]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def _build_executable(staging_root: Path, output_root: Path) -> Path:
    release_environment = _assert_pinned_release_environment()
    exe = output_root / "RangeScout.exe"
    staging_root = staging_root.resolve()
    output_root = output_root.resolve()
    _safe_remove(output_root)

    build_dist = output_root.parent / "_pyi_dist"
    build_work = output_root.parent / "_pyi_work"
    build_spec = output_root.parent / "_pyi_spec"
    _safe_remove(build_dist)
    _safe_remove(build_work)
    _safe_remove(build_spec)
    version_file = output_root.parent / "_windows_version_info.txt"
    version_file.write_text(_windows_version_text(), encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name",
        "RangeScout",
        "--distpath",
        str(build_dist),
        "--workpath",
        str(build_work),
        "--specpath",
        str(build_spec),
        "--version-file",
        str(version_file),
    ]
    resources = staging_root / "resources"
    app_icon = resources / "rangescout.ico"
    if not app_icon.is_file():
        raise RuntimeError(f"Missing required RangeScout application icon: {app_icon}")
    cmd.extend(["--icon", str(app_icon)])
    cmd.extend(["--add-data", f"{resources};resources"])
    cmd.append(str(staging_root / "app" / "ui" / "runner.py"))
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONHASHSEED", "0")
        subprocess.run(
            cmd,
            check=True,
            cwd=str(staging_root),
            env=env,
            text=True,
            capture_output=True,
        )
    except Exception as exc:  # pragma: no cover - subprocess passthrough
        if output := traceback.format_exc():
            print(output)
        raise RuntimeError(f"PyInstaller failed: {exc}") from exc
    staged_root = build_dist / "RangeScout"
    if not staged_root.exists():
        raise RuntimeError(f"PyInstaller did not generate dist payload at: {staged_root}")

    try:
        shutil.move(str(staged_root), str(output_root))
    except Exception as exc:
        _safe_remove(output_root)
        raise RuntimeError(f"Could not stage PyInstaller output: {exc}") from exc

    _safe_remove(build_dist)
    _safe_remove(build_work)
    _safe_remove(build_spec)
    version_file.unlink(missing_ok=True)
    if not exe.exists():
        raise RuntimeError("PyInstaller did not generate RangeScout.exe")
    if not _is_windows_pe_executable(exe):
        raise RuntimeError("RangeScout.exe is not a valid Windows PE executable")
    _assert_clean_runtime_sources(output_root)
    shadow_libraries = _remove_prohibited_qt_shadow_libraries(output_root)
    smoke = _run_packaged_qt_smoke(output_root)
    _write_qt_runtime_audit(
        output_root,
        environment=release_environment,
        shadow_libraries=shadow_libraries,
        smoke=smoke,
    )
    return exe


def _assert_clean_runtime_sources(output_root: Path) -> None:
    leaked_sources = sorted(
        path.relative_to(output_root).as_posix()
        for path in output_root.rglob("*.py")
        if "app" in path.relative_to(output_root).parts
    )
    if leaked_sources:
        raise RuntimeError(
            "Public runtime contains project Python sources: " + ", ".join(leaked_sources[:20])
        )


def _write_launcher_files(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)

    (target_dir / "run.bat").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "\"%~dp0RangeScout.exe\"\r\n"
        "if %ERRORLEVEL% neq 0 (\r\n"
        "  echo Launch failed. Verify installation completed successfully.\r\n"
        "  exit /b %ERRORLEVEL%\r\n"
        ")\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    (target_dir / "run.ps1").write_text(
        "param([string]$Exe)\r\n"
        "$exePath = if ($Exe) { $Exe } else { Join-Path $PSScriptRoot 'RangeScout.exe' }\r\n"
        "if (-not (Test-Path $exePath)) { Write-Host 'RangeScout.exe not found.'; exit 1 }\r\n"
        "& $exePath\r\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\r\n"
        "exit 0\r\n",
        encoding="utf-8",
    )
    launcher_payload = "".join(
        [
            "@echo off\r\n",
            "setlocal\r\n",
            "set \"TARGET=%~1\"\r\n",
            "if not defined TARGET if defined RANGE_SCOUT_INSTALL_BASE set \"TARGET=%RANGE_SCOUT_INSTALL_BASE%\\RangeScout\"\r\n",
            "if not defined TARGET if defined ProgramFiles set \"TARGET=%ProgramFiles%\\RangeScout\"\r\n",
            "if not defined TARGET if defined PROGRAMFILES set \"TARGET=%PROGRAMFILES%\\RangeScout\"\r\n",
            "if \"%TARGET:~-1%\"==\"\\\\\" set \"TARGET=%TARGET:~0,-1%\"\r\n",
            "if not defined TARGET exit /b 1\r\n",
            "for %%I in (\"%TARGET%\") do set \"TARGET=%%~fI\"\r\n",
            "if not exist \"%~dp0install.ps1\" (\r\n",
            "  echo Missing install.ps1 in package root.\r\n",
            "  exit /b 1\r\n",
            ")\r\n",
            "powershell -NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -File \"%~dp0install.ps1\" -Target \"%TARGET%\"\r\n",
            "if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%\r\n",
            "if not exist \"%TARGET%\\RangeScout.exe\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\_internal\\PySide6\\plugins\\platforms\\qwindows.dll\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\_internal\\PySide6\\Qt6Core.dll\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\_internal\\PySide6\\Qt6Gui.dll\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\_internal\\PySide6\\Qt6Widgets.dll\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\README.md\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\README.txt\" exit /b 1\r\n",
            "if not exist \"%TARGET%\\LICENSE\" exit /b 1\r\n",
            "echo Installed to %TARGET%\r\n",
            "exit /b 0\r\n",
        ]
    )
    (target_dir / "install.bat").write_text(launcher_payload, encoding="utf-8")

    (target_dir / "install.ps1").write_text(
        "param(\r\n"
        "  [string]$Target = \"$env:ProgramFiles\\RangeScout\"\r\n"
        ")\r\n"
        "Set-StrictMode -Version Latest\r\n"
        "$ErrorActionPreference = \"Stop\"\r\n"
        "if (-not $Target) {\r\n"
        "  if ($env:RANGE_SCOUT_INSTALL_BASE) { $Target = \"$env:RANGE_SCOUT_INSTALL_BASE\\RangeScout\" }\r\n"
        "  if (-not $Target) { $Target = \"$env:ProgramFiles\\RangeScout\" }\r\n"
        "}\r\n"
        "New-Item -ItemType Directory -Force -Path $Target | Out-Null\r\n"
        "$sourceRoot = $PSScriptRoot\r\n"
        "$requiredEntries = @(Get-ChildItem -LiteralPath $sourceRoot -Force)\r\n"
        "$sourceFiles = @()\r\n"
        "foreach ($entry in $requiredEntries) {\r\n"
        "  if ($entry.PSIsContainer) {\r\n"
        "    Get-ChildItem -LiteralPath $entry.FullName -Recurse -File | ForEach-Object { $sourceFiles += $_ }\r\n"
        "  } else {\r\n"
        "    $sourceFiles += $entry\r\n"
        "  }\r\n"
        "}\r\n"
        "foreach ($sourceFile in $sourceFiles) {\r\n"
        "  if (-not (Test-Path -LiteralPath $sourceFile.FullName)) {\r\n"
        "    Write-Host \"Missing source file $($sourceFile.FullName).\"\r\n"
        "    exit 1\r\n"
        "  }\r\n"
        "  $relative = $sourceFile.FullName.Substring($sourceRoot.Length).TrimStart('\\\\')\r\n"
        "  $destination = Join-Path $Target $relative\r\n"
        "  $destinationDir = Split-Path -Parent $destination\r\n"
        "  if ($destinationDir -and -not (Test-Path -LiteralPath $destinationDir)) {\r\n"
        "    New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null\r\n"
        "  }\r\n"
        "  Copy-Item -LiteralPath $sourceFile.FullName -Destination $destination -Force | Out-Null\r\n"
        "}\r\n"
        "$sourceExe = Join-Path $PSScriptRoot 'RangeScout.exe'\r\n"
        "if (-not (Test-Path $sourceExe)) { Write-Host 'RangeScout.exe missing in package.'; exit 1 }\r\n"
        "foreach ($sourceFile in $sourceFiles) {\r\n"
        "  $relative = $sourceFile.FullName.Substring($sourceRoot.Length).TrimStart('\\\\')\r\n"
        "  $destination = Join-Path $Target $relative\r\n"
        "  if (-not (Test-Path -LiteralPath $destination)) {\r\n"
        "    Write-Host \"Missing installed file $relative.\"\r\n"
        "    exit 1\r\n"
        "  }\r\n"
        "  $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()\r\n"
        "  $targetHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()\r\n"
        "  if ($sourceHash -ne $targetHash) {\r\n"
        "    Write-Host \"SHA mismatch for $relative.\"\r\n"
        "    exit 1\r\n"
        "  }\r\n"
        "}\r\n"
        "$qwindows = Join-Path $Target '_internal\\PySide6\\plugins\\platforms\\qwindows.dll'\r\n"
        "if (-not (Test-Path $qwindows)) { Write-Host 'Missing required DLL: qwindows.dll'; exit 1 }\r\n"
        "exit 0\r\n",
        encoding="utf-8",
    )
    (target_dir / "uninstall.bat").write_text(
        "@echo off\r\n"
        "setlocal EnableExtensions\r\n"
        "if \"%~1\"==\"\" (\r\n"
        "  set \"HAS_ARG=0\"\r\n"
        "  set \"TARGET=%~dp0.\"\r\n"
        ") else (\r\n"
        "  set \"HAS_ARG=1\"\r\n"
        "  set \"TARGET=%~f1\"\r\n"
        ")\r\n"
        "if not defined TARGET if \"%HAS_ARG%\"==\"1\" if defined RANGE_SCOUT_INSTALL_BASE set \"TARGET=%RANGE_SCOUT_INSTALL_BASE%\\RangeScout\"\r\n"
        "if \"%TARGET:~-1%\"==\"\\\\\" set \"TARGET=%TARGET:~0,-1%\"\r\n"
        "for %%I in (\"%TARGET%\") do set \"TARGET=%%~fI\"\r\n"
        "if not exist \"%TARGET%\" exit /b 0\r\n"
        "taskkill /F /IM RangeScout.exe /T >nul 2>&1\r\n"
        "if not defined TEMP if defined SystemRoot set \"TEMP=%SystemRoot%\\Temp\"\r\n"
        "if not defined TEMP if defined WINDIR set \"TEMP=%WINDIR%\\Temp\"\r\n"
        "set \"RS_UNINSTALL_WORKDIR=%TEMP%\\RangeScoutUninstall_%RANDOM%_%RANDOM%\"\r\n"
        "if not exist \"%RS_UNINSTALL_WORKDIR%\" mkdir \"%RS_UNINSTALL_WORKDIR%\"\r\n"
        "set \"RS_UNINSTALL_WORKER=%RS_UNINSTALL_WORKDIR%\\RangeScout_uninstall_%RANDOM%.bat\"\r\n"
        "(echo @echo off > \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo setlocal EnableExtensions >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo set \"TARGET=%TARGET%\" >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo if not exist \"%%TARGET%%\" exit /b 0 >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo set \"TRIES=0\" >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo :UNINSTALL_ATTEMPT >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo rmdir /S /Q \"%%TARGET%%\" >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo if not exist \"%%TARGET%%\" exit /b 0 >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo set /A TRIES+=1 >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo if %%TRIES%% GEQ 6 exit /b 1 >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo timeout /T 1 /NOBREAK >nul >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo goto UNINSTALL_ATTEMPT >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "(echo exit /b 0 >> \"%RS_UNINSTALL_WORKER%\")\r\n"
        "if not exist \"%RS_UNINSTALL_WORKER%\" exit /b 1\r\n"
        "start \"\" /b cmd /c \"\"%RS_UNINSTALL_WORKER%\"\"\r\n"
        "if errorlevel 1 exit /b 1\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )
    (target_dir / "uninstall.ps1").write_text(
        "param(\r\n"
        "  [string]$Target = \"$env:ProgramFiles\\RangeScout\"\r\n"
        ")\r\n"
        "$Target = $Target.Trim()\r\n"
        "if (-not $Target) {\r\n"
        "  $Target = \"$env:ProgramFiles\\RangeScout\"\r\n"
        "}\r\n"
        "$target = [System.IO.Path]::GetFullPath($Target)\r\n"
        "if (-not (Test-Path -LiteralPath $target -PathType Container)) {\r\n"
        "  exit 0\r\n"
        "}\r\n"
        "$targetExe = Join-Path $target 'RangeScout.exe'\r\n"
        "$deadline = (Get-Date).AddSeconds(20)\r\n"
        "while ((Get-Date) -lt $deadline) {\r\n"
        "  $remaining = Get-Process -Name RangeScout -ErrorAction SilentlyContinue |\r\n"
        "    Where-Object {\r\n"
        "      if (-not $_.Path) { $false }\r\n"
        "      try {\r\n"
        "        [System.IO.Path]::GetFullPath($_.Path) -ieq $targetExe\r\n"
        "      } catch {\r\n"
        "        $false\r\n"
        "      }\r\n"
        "    }\r\n"
        "  if (-not $remaining) {\r\n"
        "    break\r\n"
        "  }\r\n"
        "  foreach ($proc in @($remaining)) {\r\n"
        "    try {\r\n"
        "      Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue\r\n"
        "    } catch {\r\n"
        "      # ignore\r\n"
        "    }\r\n"
        "  }\r\n"
        "  Start-Sleep -Milliseconds 500\r\n"
        "}\r\n"
        "function Remove-TargetDirectory {\r\n"
        "  param([string]$Path)\r\n"
        "  $attempts = 0\r\n"
        "  while ((Test-Path -LiteralPath $Path) -and $attempts -lt 8) {\r\n"
        "    try {\r\n"
        "      Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop\r\n"
        "      if (-not (Test-Path -LiteralPath $Path)) {\r\n"
        "        return $true\r\n"
        "      }\r\n"
        "    } catch {\r\n"
        "      Start-Sleep -Milliseconds 500\r\n"
        "      $attempts++\r\n"
        "    }\r\n"
        "  }\r\n"
        "  return -not (Test-Path -LiteralPath $Path)\r\n"
        "}\r\n"
        "if (Remove-TargetDirectory -Path $target) {\r\n"
        "  exit 0\r\n"
        "}\r\n"
        "exit 1\r\n",
        encoding="utf-8",
    )
    (target_dir / "installer.bat").write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "call \"%~dp0install.bat\" \"%~1\"\r\n"
        "if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%\r\n"
        "echo Installer completed.\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
    )


def build_release_package(
    source_root: Path,
    dist_dir: Path,
    version: str,
    *,
    with_executable: bool = True,
) -> tuple[Path, Path]:
    if not with_executable and os.environ.get("RS_ALLOW_PLACEHOLDER_EXE", "0") != "1":
        raise RuntimeError(
            "Refusing to build a release without a real RangeScout.exe. "
            "Set RS_ALLOW_PLACEHOLDER_EXE=1 only for isolated fixture tests."
        )

    build_timestamp = _build_timestamp(source_root)
    declared_build_utc = os.environ.get("RANGESCOUT_BUILD_UTC", build_timestamp)
    actual_build_utc = os.environ.get(
        "RANGESCOUT_ACTUAL_BUILD_UTC",
        declared_build_utc,
    )

    dist_dir = dist_dir.resolve()
    dist_dir.mkdir(parents=True, exist_ok=True)

    package_root = dist_dir / f"RangeScout-{version}-windows"
    _safe_remove(package_root)

    staging_root = dist_dir / "staging-runtime"
    stage_release(source_root, staging_root, include_tests=False)

    source_staging_root = dist_dir / "staging-source"
    stage_release(source_root, source_staging_root, include_tests=True)
    verify_staged_source_contract(source_root, source_staging_root)
    source_zip_path = dist_dir / f"RangeScout-{version}-source.zip"
    _zip_dir(
        source_staging_root,
        source_zip_path,
        skip_dirs=PACKAGING_SKIP_DIRS,
        skip_file_suffixes=PACKAGING_SKIP_FILE_SUFFIXES,
        skip_filenames=PACKAGING_SKIP_FILENAMES,
    )

    if with_executable:
        _build_executable(staging_root, package_root)
    else:
        (package_root / "RangeScout.exe").write_text("EXE_NOT_BUILT_IN_TESTS\n", encoding="utf-8")

    removed_components = _remove_forbidden_runtime_components(package_root)
    if removed_components:
        pass
    _ensure_forbidden_runtime_components_absent(package_root)

    (package_root / "README.txt").write_text(
        "RangeScout {name} v{version}\r\n"
        "Build: {build}\r\n"
        "Source package: {source_zip}\r\n"
        "Source-Date-Epoch: {sde}\r\n"
        "Build UTC (SOURCE_DATE_EPOCH): {build_utc_sde}\r\n".format(
            name=PRODUCT.name,
            version=PRODUCT.version,
            build=PRODUCT.build_identity,
            source_zip=source_zip_path.name,
            sde=os.environ.get("SOURCE_DATE_EPOCH", "not-set"),
            build_utc_sde=build_timestamp,
        ),
        encoding="utf-8",
    )
    exe_path = package_root / "RangeScout.exe"
    (package_root / "RELEASE_IDENTITY.txt").write_text(
        f"name={PRODUCT.name}\r\n"
        f"version={PRODUCT.version}\r\n"
        f"build={PRODUCT.build_identity}\r\n"
        f"source_date_epoch={os.environ.get('SOURCE_DATE_EPOCH', '')}\r\n"
        f"generated_utc={build_timestamp}\r\n"
        f"build_utc={declared_build_utc}\r\n"
        f"actual_build_utc={actual_build_utc}\r\n"
        f"source_exe_sha256={_file_sha256(exe_path) if exe_path.exists() else ''}\r\n"
        f"source_zip={source_zip_path.name}\r\n",
        encoding="utf-8",
    )
    _write_public_readme_files(package_root)
    if (source_root / "LICENSE").exists():
        shutil.copy2(source_root / "LICENSE", package_root / "LICENSE")

    _write_launcher_files(package_root)
    _write_dependency_notice_files(package_root, source_root, build_timestamp)

    manifest_path = package_root / MANIFEST_NAME
    exclusions = set(MANIFEST_ENTRY_EXCLUSIONS)
    records = _safe_manifest_rows(package_root, exclusions=exclusions)
    _write_manifest(package_root, build_timestamp, records)
    manifest_verification_path = package_root / MANIFEST_VERIFIER_NAME
    manifest_verification = _verify_manifest(
        package_root,
        records,
        checksum_file=CHECKSUM_MANIFEST_NAME,
        extra_excluded_paths=exclusions,
    )
    manifest_verification_path.write_text(
        json.dumps(manifest_verification, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    _write_checksum_manifest(package_root, records)
    manifest_verification = _verify_manifest(
        package_root,
        records,
        checksum_file=CHECKSUM_MANIFEST_NAME,
        extra_excluded_paths=exclusions,
    )
    if not manifest_verification["covered_exactly_once"]:
        raise RuntimeError(
            "Manifest coverage failed: "
            f"missing={manifest_verification['missing']} extra={manifest_verification['extra']} "
            f"duplicate={manifest_verification['duplicate']}"
        )

    # include manifest_details for consumer-friendly inventory
    _write_detailed_manifest(package_root)

    release_zip = dist_dir / f"RangeScout-{version}-windows.zip"
    _safe_remove(release_zip)
    _zip_dir(
        package_root,
        release_zip,
        skip_dirs=PACKAGING_SKIP_DIRS,
        skip_file_suffixes=PACKAGING_SKIP_FILE_SUFFIXES,
        skip_filenames=PACKAGING_SKIP_FILENAMES,
    )
    return release_zip, manifest_path


def build_from_defaults() -> tuple[Path, Path]:
    return build_release_package(
        source_root=Path(__file__).resolve().parents[1],
        dist_dir=Path("release/dist"),
        version=PRODUCT.version,
        with_executable=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="release/staging")
    parser.add_argument("--dist", default="release/dist")
    parser.add_argument("--version", default=PRODUCT.version)
    exe_group = parser.add_mutually_exclusive_group()
    exe_group.add_argument("--build-exe", action="store_true")
    exe_group.add_argument("--no-build-exe", action="store_true")
    parser.set_defaults(build_exe=True)
    args = parser.parse_args()

    source = Path(args.source)
    release_zip, manifest_path = build_release_package(
        source_root=source,
        dist_dir=Path(args.dist),
        version=args.version,
        with_executable=bool(args.build_exe and not args.no_build_exe),
    )
    print(f"release zip: {release_zip}")
    print(f"manifest: {manifest_path}")


if __name__ == "__main__":
    main()
