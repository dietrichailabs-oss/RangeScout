from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
import zipfile

from app import PRODUCT


def _release_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    env_root = os.environ.get("RANGESCOUT_RELEASE_ROOT")
    if env_root:
        candidate = Path(env_root)
        if candidate.exists():
            return candidate

    candidates = [
        root / "release" / "cp5" / f"RangeScout-{PRODUCT.version}-windows.zip",
        root / "release" / f"RangeScout-{PRODUCT.version}-windows.zip",
        root / "release" / "dist" / f"RangeScout-{PRODUCT.version}-windows.zip",
        root / "release" / f"RangeScout-{PRODUCT.version}-windows",
        root / "release" / "dist" / f"RangeScout-{PRODUCT.version}-windows",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return root / "release" / "dist" / f"RangeScout-{PRODUCT.version}-windows"


def _extract_if_needed(package_root: Path) -> Path:
    if package_root.is_file() and package_root.suffix.lower() == ".zip":
        temp = Path(tempfile.mkdtemp(prefix="rs-release-"))
        with zipfile.ZipFile(package_root, "r") as archive:
            archive.extractall(temp)
        extracted = temp / f"RangeScout-{PRODUCT.version}-windows"
        if extracted.exists():
            return extracted
        for candidate in (temp / "run.bat", temp / "run.ps1"):
            if candidate.exists():
                return temp
        entries = [entry for entry in temp.rglob("run.bat") if entry.parent.joinpath("run.ps1").exists()]
        if entries:
            return entries[0].parent
        return temp
    return package_root


def test_installer_batch_files_do_not_launch_python() -> None:
    package_root = _extract_if_needed(_release_path())
    try:
        assert package_root.exists()
        run_bat = package_root / "run.bat"
        run_ps1 = package_root / "run.ps1"
        assert run_bat.exists()
        assert run_ps1.exists()

        run_bat_text = run_bat.read_text(encoding="utf-8")
        run_ps1_text = run_ps1.read_text(encoding="utf-8")
        assert "python" not in run_bat_text.lower()
        assert "python" not in run_ps1_text.lower()
        assert "RangeScout.exe" in run_bat_text
        assert "RangeScout.exe" in run_ps1_text
    finally:
        parent = package_root.parent
        if parent.name.startswith("rs-release-") and parent.exists():
            shutil.rmtree(parent)
