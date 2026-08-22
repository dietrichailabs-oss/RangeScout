from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import importlib.util

import pytest

from app import PRODUCT
from scripts.package_release import (
    _assert_clean_runtime_sources,
    _write_dependency_notice_files,
    _write_launcher_files,
    build_release_package,
)


def test_write_dependency_notice_files_requires_lgpl_artifacts(tmp_path: Path) -> None:
    target_root = tmp_path / "package"
    target_root.mkdir()
    with pytest.raises(RuntimeError, match="Missing required notice artifact"):
        _write_dependency_notice_files(
            target_dir=target_root,
            source_root=tmp_path,
            generated_utc="2026-01-01T00:00:00+00:00",
        )


def _forbidden_qt_present(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path, "r") as archive:
        lowered = [name.lower() for name in archive.namelist()]
    return [
        path
        for path in lowered
        if "qt6virtualkeyboard.dll" in path or "qtvirtualkeyboardplugin.dll" in path
    ]


class TestPackageRelease(unittest.TestCase):
    @pytest.mark.skipif(
        importlib.util.find_spec("PyInstaller") is None or importlib.util.find_spec("PySide6") is None,
        reason="PyInstaller and PySide6 are required to validate executable build path.",
    )
    def test_package_release_builds_artifacts(self) -> None:
        source_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as folder:
            dist_root = Path(folder) / "dist"
            zip_path, manifest_path = build_release_package(
                source_root=source_root,
                dist_dir=dist_root,
                version=PRODUCT.version,
                with_executable=True,
            )

            self.assertTrue(zip_path.exists())
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn("entries", manifest)
            manifest_files = {entry["path"] for entry in manifest["entries"]}
            self.assertIn("README.md", manifest_files)
            self.assertIn("run.bat", manifest_files)
            self.assertIn("notices/DEPENDENCY_LICENSES.md", manifest_files)
            self.assertIn("notices/MARKET_DATA_NOTICE.md", manifest_files)
            self.assertIn("notices/PRIVACY_AND_DATA_USE.md", manifest_files)
            self.assertIn("notices/QT_SOURCE_INSTRUCTIONS.md", manifest_files)
            self.assertIn("notices/QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json", manifest_files)
            self.assertNotIn("SHA256SUMS.txt", manifest_files)
            self.assertNotIn("manifest.json", manifest_files)
            self.assertNotIn("manifest_details.json", manifest_files)
            self.assertFalse(any(".pyc" in key or "__pycache__" in key for key in manifest_files))
            self.assertFalse(any(key.endswith(".py") and "/app/" in f"/{key}" for key in manifest_files))

            with zipfile.ZipFile(zip_path, "r") as archive:
                names = sorted(archive.namelist())
                self.assertIn("README.md", names)
                self.assertIn("run.bat", names)
                self.assertIn("notices/DEPENDENCY_LICENSES.md", names)
                self.assertIn("notices/MARKET_DATA_NOTICE.md", names)
                self.assertIn("notices/PRIVACY_AND_DATA_USE.md", names)
                self.assertIn("notices/QT_SOURCE_INSTRUCTIONS.md", names)
                self.assertIn("notices/QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json", names)
                self.assertIn("manifest.json", names)
                self.assertEqual(_forbidden_qt_present(zip_path), [])
                self.assertIn("_internal/PySide6/plugins/platforms/qwindows.dll", names)

                source_manifest = json.loads(
                    archive.read("notices/QT_CORRESPONDING_SOURCE_ASSET_MANIFEST.json")
                )
                sbom = json.loads(archive.read("notices/SBOM.json"))
                mapped = {
                    component
                    for asset in source_manifest["source_assets"]
                    for component in asset["runtime_sbom_mapping"]
                }
                self.assertTrue({"PySide6", "shiboken6", "QtCore", "QtPdf", "QtSvg"} <= mapped)
                inclusion = {
                    asset["filename"]: asset["master_handoff_inclusion"]
                    for asset in source_manifest["source_assets"]
                }
                self.assertTrue(inclusion["qtbase-everywhere-src-6.11.1.tar.xz"])
                self.assertFalse(inclusion["qtwebengine-everywhere-src-6.11.1.tar.xz"])
                instructions = archive.read("notices/QT_SOURCE_INSTRUCTIONS.md").decode("utf-8")
                self.assertIn("578,914,356-byte size", instructions)
                self.assertIn("stable project-controlled corresponding-source location", instructions)
                self.assertIn("not custom GitHub Release assets", instructions)
                self.assertIn("publication remains", instructions)
                self.assertIn("blocked if this retained asset", instructions)
                shipped = {item["name"] for item in sbom["packages"]}
                source_relevant = {
                    "PySide6", "shiboken6", "QtCore", "QtGui", "QtNetwork", "QtOpenGL",
                    "QtPdf", "QtQml", "QtQuick", "QtSvg", "QtWidgets",
                }
                self.assertEqual((shipped & source_relevant) - mapped, set())
                self.assertEqual(
                    archive.read("notices/MARKET_DATA_NOTICE.md"),
                    (source_root / "docs" / "MARKET_DATA_NOTICE.md").read_bytes(),
                )
                self.assertEqual(
                    archive.read("notices/PRIVACY_AND_DATA_USE.md"),
                    (source_root / "docs" / "PRIVACY_AND_DATA_USE.md").read_bytes(),
                )


def test_uninstall_bat_generation_matches_packaged_uninstall_bat(tmp_path: Path) -> None:
    launcher_root = tmp_path / "launcher"
    _write_launcher_files(launcher_root)
    generated_uninstall_bytes = (launcher_root / "uninstall.bat").read_bytes()
    generated_uninstall = generated_uninstall_bytes.decode("utf-8")

    assert 'if "%~1"=="" (' in generated_uninstall
    assert 'set "HAS_ARG=0"' in generated_uninstall
    assert 'set "TARGET=%~dp0."' in generated_uninstall
    assert 'set "HAS_ARG=1"' in generated_uninstall
    assert 'set "TARGET=%~f1"' in generated_uninstall
    assert (
        'if not defined TARGET if "%HAS_ARG%"=="1" if defined RANGE_SCOUT_INSTALL_BASE set "TARGET=%RANGE_SCOUT_INSTALL_BASE%\\RangeScout"'
        in generated_uninstall
    )
    assert 'set "RS_UNINSTALL_WORKDIR=%TEMP%\\RangeScoutUninstall_%RANDOM%_%RANDOM%"' in generated_uninstall
    assert 'if not exist "%RS_UNINSTALL_WORKDIR%" mkdir "%RS_UNINSTALL_WORKDIR%"' in generated_uninstall
    assert '(echo set "TRIES=0" >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert '(echo :UNINSTALL_ATTEMPT >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert '(echo if not exist "%%TARGET%%" exit /b 0 >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert '(echo rmdir /S /Q "%%TARGET%%" >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert '(echo if %%TRIES%% GEQ 6 exit /b 1 >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert '(echo timeout /T 1 /NOBREAK >nul >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert '(echo goto UNINSTALL_ATTEMPT >> "%RS_UNINSTALL_WORKER%")' in generated_uninstall
    assert 'if errorlevel 1 exit /b 1' in generated_uninstall
    assert generated_uninstall.rstrip().endswith('exit /b 0')

    if importlib.util.find_spec("PyInstaller") is None or importlib.util.find_spec("PySide6") is None:
        pytest.skip("PyInstaller and PySide6 are required to validate packaged uninstall BAT parity.")

    source_root = Path(__file__).resolve().parents[2]
    dist_root = tmp_path / "dist"
    release_zip, _manifest_path = build_release_package(
        source_root=source_root,
        dist_dir=dist_root,
        version=PRODUCT.version,
        with_executable=True,
    )

    with zipfile.ZipFile(release_zip, "r") as archive:
        packaged_uninstall = archive.read("uninstall.bat")

    assert packaged_uninstall == generated_uninstall_bytes


def test_runtime_source_cleanliness_gate_rejects_project_python(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    (clean / "_internal").mkdir(parents=True)
    (clean / "_internal" / "base_library.zip").write_bytes(b"compiled")
    _assert_clean_runtime_sources(clean)
    leaked = clean / "_internal" / "app" / "market_data" / "router.py"
    leaked.parent.mkdir(parents=True)
    leaked.write_text("source", encoding="utf-8")
    with pytest.raises(RuntimeError, match="project Python sources"):
        _assert_clean_runtime_sources(clean)
