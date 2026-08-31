from __future__ import annotations

from pathlib import Path
import tempfile

from scripts.handoff import installer_evidence
from scripts.handoff.inno_installer_evidence import _accepted_display_name, _inno_default_target


def test_inno_current_user_display_name_preserves_exact_product_and_version() -> None:
    assert _accepted_display_name("RangeScout 1.6.3")
    assert _accepted_display_name("RangeScout 1.6.3 (Current user)")
    assert not _accepted_display_name("RangeScout 1.2.0 (Current user)")
    assert not _accepted_display_name("Other 1.6.3")


def test_inno_default_target_matches_per_user_install_location(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert _inno_default_target() == (tmp_path / "Local" / "Programs" / "RangeScout").resolve()


def test_uninstall_passed_requires_zero_return_code() -> None:
    """Non-zero uninstall return code must fail the pass gate."""
    scenario = {
        "target_exists_before_uninstall": True,
        "installed_copy_uninstaller_invoked": True,
        "uninstall_step": {"return_code": 1},
        "target_exists_after_uninstall": False,
        "remaining_files_after_uninstall": [],
        "cleanup_required": False,
    }
    assert not installer_evidence._uninstall_passed(scenario)


def test_uninstall_passed_accepts_clean_zero_return_code() -> None:
    """All strict uninstall gate conditions must pass when clean and zero-return."""
    scenario = {
        "target_exists_before_uninstall": True,
        "installed_copy_uninstaller_invoked": True,
        "uninstall_step": {"return_code": 0},
        "target_exists_after_uninstall": False,
        "remaining_files_after_uninstall": [],
        "cleanup_required": False,
    }
    assert installer_evidence._uninstall_passed(scenario)


def test_select_uninstaller_script_prefers_bat() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        ps1 = root / "uninstall.ps1"
        bat = root / "uninstall.bat"
        bat.write_text("echo uninstall-bat", encoding="utf-8")
        ps1.write_text("Write-Host uninstall-ps1", encoding="utf-8")

        selected, label = installer_evidence._select_uninstaller_script(root)
        assert selected == bat
        assert label == "uninstall.bat"


def test_uninstall_evidence_scenario_contains_bat_first_entrypoint() -> None:
    scenario = {
        "installed_copy_uninstaller_invoked": True,
        "installed_uninstall_path": "C:\\\\RangeScout\\\\uninstall.bat",
        "target_exists_before_uninstall": True,
        "target_exists_after_uninstall": False,
        "remaining_files_after_uninstall": [],
        "cleanup_required": False,
        "uninstall_entrypoint": "uninstall.bat",
        "uninstall_step": {"return_code": 0},
    }
    assert installer_evidence._uninstall_passed(scenario)
    assert scenario["installed_uninstall_path"].endswith("uninstall.bat")
