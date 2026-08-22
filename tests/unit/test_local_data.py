from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.application.local_data import delete_local_data, LocalDataDeletionReport


def test_delete_local_data_removes_allowlisted_files_and_records(tmp_path: Path) -> None:
    db = tmp_path / "history.sqlite"
    db.write_text("db", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "junk.txt").write_text("x", encoding="utf-8")

    report = delete_local_data(tmp_path)
    assert report.complete is True
    assert db.name.split("\\")[-1] in report.deleted_paths
    assert settings.name.split("\\")[-1] in report.deleted_paths
    assert "temp" in report.deleted_paths
    assert not db.exists()
    assert not settings.exists()
    assert not temp.exists()


def test_delete_local_data_refuses_symlink_child(tmp_path: Path) -> None:
    if not hasattr(os, "symlink") and not hasattr(Path, "symlink_to"):
        return

    child = tmp_path / "history.sqlite"
    target = tmp_path / "target-file"
    try:
        child.symlink_to(target)
    except OSError:
        return
    target.write_text("x", encoding="utf-8")

    report = delete_local_data(tmp_path, targets=["history.sqlite"])
    assert report.complete is False
    assert report.refused_unsafe_paths["history.sqlite"] == "target is symlink/reparse"


def test_delete_local_data_partial_failure_is_reported(tmp_path: Path) -> None:
    stubborn = tmp_path / "history.sqlite"
    stubborn.write_text("db", encoding="utf-8")
    original_unlink = Path.unlink

    def blocked_unlink(_self: Path, *args: object, **kwargs: object) -> None:
        if str(_self) == str(stubborn):
            raise PermissionError("forced failure")
        original_unlink(_self, *args, **kwargs)

    try:
        setattr(Path, "unlink", blocked_unlink)
        report = delete_local_data(tmp_path, targets=["history.sqlite"])
    finally:
        setattr(Path, "unlink", original_unlink)

    assert report.complete is False
    assert "history.sqlite" in report.failed_paths


def test_delete_local_data_preserves_exported_csv_outside_root(tmp_path: Path) -> None:
    data_root = tmp_path / "app-data"
    data_root.mkdir()
    (data_root / "settings.json").write_text("{}", encoding="utf-8")
    exported_csv = tmp_path / "exports" / "portfolio.csv"
    exported_csv.parent.mkdir()
    expected = b"symbol,value\r\nAAPL,123\r\n"
    exported_csv.write_bytes(expected)

    report = delete_local_data(data_root)

    assert report.complete is True
    assert exported_csv.read_bytes() == expected


def _create_windows_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction regression")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"Unable to create Windows junction: {completed.stderr or completed.stdout}")


def test_delete_local_data_refuses_nested_windows_junction(tmp_path: Path) -> None:
    data_root = tmp_path / "app-data"
    temp = data_root / "temp"
    temp.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    expected = b"outside-app-data"
    sentinel.write_bytes(expected)
    junction = temp / "junction-child"
    _create_windows_junction(junction, external)

    report = delete_local_data(data_root, targets=["temp"])

    assert report.complete is False
    assert report.refused_unsafe_paths["temp/junction-child"] == "target is symlink/reparse"
    assert report.failed_paths["temp"] == "unsafe descendant remains"
    assert junction.exists()
    assert sentinel.read_bytes() == expected
