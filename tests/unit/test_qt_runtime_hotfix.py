from __future__ import annotations

import struct
from pathlib import Path

import pytest

from scripts import package_release


def _write_amd64_pe(path: Path) -> None:
    payload = bytearray(512)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, package_release.PE_MACHINE_AMD64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def test_release_dependency_lock_is_exact() -> None:
    assert package_release.RELEASE_PYTHON == (3, 14, 6)
    assert package_release.RELEASE_DISTRIBUTIONS == {
        "PySide6": "6.11.1",
        "PySide6-Addons": "6.11.1",
        "PySide6-Essentials": "6.11.1",
        "shiboken6": "6.11.1",
        "PyInstaller": "6.21.0",
        "pyinstaller-hooks-contrib": "2026.6",
    }


def test_pe_machine_gate_accepts_amd64_and_rejects_invalid(tmp_path: Path) -> None:
    amd64 = tmp_path / "amd64.dll"
    _write_amd64_pe(amd64)
    assert package_release._pe_machine(amd64) == package_release.PE_MACHINE_AMD64
    invalid = tmp_path / "invalid.dll"
    invalid.write_bytes(b"not-pe")
    with pytest.raises(RuntimeError, match="Windows PE"):
        package_release._pe_machine(invalid)


def test_qt_shadow_library_is_removed_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    shadow = runtime / "_internal" / "icuuc.dll"
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(b"incompatible ICU payload")
    monkeypatch.setattr(package_release, "_windows_file_version", lambda _path: "78.3.0.0")

    result = package_release._remove_prohibited_qt_shadow_libraries(runtime)

    assert result["remaining"] == []
    assert result["removed"] == [
        {
            "path": "_internal/icuuc.dll",
            "size": len(b"incompatible ICU payload"),
            "sha256": __import__("hashlib").sha256(b"incompatible ICU payload").hexdigest(),
            "file_version": "78.3.0.0",
            "reason": "shadows the Windows ICU compatibility DLL required by Qt6Core",
        }
    ]
    assert not shadow.exists()

def test_runtime_audit_rejects_duplicate_qt6core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    internal = runtime / "_internal"
    critical = [
        runtime / "RangeScout.exe",
        internal / "PySide6" / "Qt6Core.dll",
        internal / "PySide6" / "Qt6Gui.dll",
        internal / "PySide6" / "Qt6Widgets.dll",
        internal / "PySide6" / "Qt6Network.dll",
        internal / "PySide6" / "Qt6WebSockets.dll",
        internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
        internal / "PySide6" / "QtCore.pyd",
        internal / "shiboken6" / "Shiboken.pyd",
        internal / "python314.dll",
    ]
    for path in critical:
        _write_amd64_pe(path)
    _write_amd64_pe(internal / "stale" / "Qt6Core.dll")
    monkeypatch.setattr(package_release, "_windows_file_version", lambda _path: "test")

    with pytest.raises(RuntimeError, match="Duplicate/conflicting Qt6Core"):
        package_release._write_qt_runtime_audit(
            runtime,
            environment={"pass": True},
            shadow_libraries={"removed": [], "remaining": []},
            smoke={"pass": True},
        )
