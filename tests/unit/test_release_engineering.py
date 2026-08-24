from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import scripts.release_engineering as release_engineering

from app import PRODUCT
from scripts.release_engineering import (
    INTERNAL_ARTIFACT_NAMES,
    _write_checksums,
    _write_portable_zip,
    configure_reproducible_build_environment,
    runtime_manifest,
    verify_internal_artifacts,
    write_runtime_manifest,
)
from scripts.package_release import _windows_version_text
from scripts.stage_release import SOURCE_ALLOWLIST
from scripts.stage_release import stage_release
from scripts.handoff.verify_staged_source_regression import verify_staged_source_contract


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_milestone_product_identity() -> None:
    assert PRODUCT.version == "1.6.2"
    assert PRODUCT.build_identity == "rs-v1.6.2-semantic-routing-r8"
    assert PRODUCT.company == "Dietrich AI Labs"
    assert PRODUCT.publisher == "Dietrich AI Labs"
    assert "packaging" in SOURCE_ALLOWLIST
    assert "RELEASE_NOTES_v1.6.2.md" in SOURCE_ALLOWLIST
    assert "RELEASE_NOTES_v1.1.0.md" not in SOURCE_ALLOWLIST


def test_staged_source_contract_is_self_contained(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    staged = tmp_path / "staged-source"
    stage_release(root, staged, include_tests=True)
    referenced = verify_staged_source_contract(root, staged)
    assert (staged / f"RELEASE_NOTES_v{PRODUCT.version}.md").is_file()
    assert "RELEASE_NOTES_v1.1.0.md" not in referenced


def test_staged_source_contract_rejects_test_reference_to_omitted_repository_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    staged = tmp_path / "staged"
    for root in (repository, staged):
        (root / "tests").mkdir(parents=True)
        for name in ("README.md", "LICENSE", "pyproject.toml", f"RELEASE_NOTES_v{PRODUCT.version}.md"):
            (root / name).write_text(name, encoding="utf-8")
    (repository / "OMITTED.md").write_text("historical", encoding="utf-8")
    test_source = 'root = Path(__file__).resolve().parents[1]\ntext = (root / "OMITTED.md").read_text()\n'
    (staged / "tests" / "test_omitted.py").write_text(test_source, encoding="utf-8")

    with pytest.raises(RuntimeError, match="OMITTED.md"):
        verify_staged_source_contract(repository, staged)


def test_windows_metadata_and_public_disclosure_use_dietrich_identity() -> None:
    root = Path(__file__).resolve().parents[2]
    version_text = _windows_version_text()
    assert "StringStruct('CompanyName', 'Dietrich AI Labs')" in version_text
    assert "StringStruct('ProductVersion', '1.6.2')" in version_text
    inno = (root / "packaging" / "windows" / "RangeScout.iss").read_text(encoding="utf-8")
    assert '#define AppPublisher "Dietrich AI Labs"' in inno
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "Publisher/company: Dietrich AI Labs" in readme
    assert "publicly trusted Authenticode certificate is required" in readme


def test_authenticode_inspection_transports_spaced_path_through_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "folder with spaces" / "RangeScout.exe"
    target.parent.mkdir()
    target.write_bytes(b"MZfixture")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return type("Completed", (), {
            "returncode": 0,
            "stdout": '{"Status":"NotTrusted","SignerThumbprint":"ABC"}',
            "stderr": "",
        })()

    monkeypatch.setattr(release_engineering.subprocess, "run", fake_run)
    details = release_engineering._authenticode_details(target)
    assert details["SignerThumbprint"] == "ABC"
    assert observed["command"][-1].find("RANGESCOUT_AUTHENTICODE_PATH") >= 0
    assert observed["environment"]["RANGESCOUT_AUTHENTICODE_PATH"] == str(target.resolve())


def test_reproducible_build_environment_is_bound_to_source_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    completed = type("Completed", (), {"returncode": 0, "stdout": "1700000000\n", "stderr": ""})()
    monkeypatch.setattr(release_engineering.subprocess, "run", lambda *_args, **_kwargs: completed)
    values = configure_reproducible_build_environment(tmp_path)
    assert values["SOURCE_DATE_EPOCH"] == "1700000000"
    assert release_engineering.os.environ["SOURCE_DATE_EPOCH"] == "1700000000"
    assert release_engineering.os.environ["RANGESCOUT_BUILD_UTC"] == values["build_utc"]


def test_inno_definition_has_required_install_contract() -> None:
    source = Path(__file__).resolve().parents[2] / "packaging" / "windows" / "RangeScout.iss"
    text = source.read_text(encoding="utf-8")
    required = (
        "DefaultDirName={autopf}\\RangeScout",
        "PrivilegesRequiredOverridesAllowed=dialog commandline",
        'Name: "desktopicon"',
        'Name: "{autoprograms}\\RangeScout"',
        'UninstallDisplayName=RangeScout {#AppVersion}',
        "Uninstallable=yes",
        'Source: "{#RuntimeRoot}\\*"',
        "notimestamp",
        'Type: filesandordirs; Name: "{app}\\_internal"',
    )
    for value in required:
        assert value in text
    assert "AppData" not in text
    assert "*.csv" not in text
    assert "[UninstallDelete]" not in text


def test_runtime_manifest_and_portable_zip_are_exact_payload_twins(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "_internal").mkdir(parents=True)
    (runtime / "RangeScout.exe").write_bytes(b"MZfixture")
    (runtime / "_internal" / "runtime.dll").write_bytes(b"runtime")
    (runtime / "README.md").write_text("read me", encoding="utf-8")
    manifest_path = write_runtime_manifest(runtime, tmp_path / "runtime_manifest.json")
    portable = _write_portable_zip(runtime, tmp_path / "RangeScout_1.6.0_Portable.zip")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["record_count"] == 3
    assert payload["entries"] == runtime_manifest(runtime)
    with zipfile.ZipFile(portable) as archive:
        assert archive.testzip() is None
        names = sorted(archive.namelist())
        assert names == ["README.md", "RangeScout.exe", "_internal/runtime.dll"]
        for row in payload["entries"]:
            assert hashlib.sha256(archive.read(row["path"])).hexdigest() == row["sha256"]


def test_public_artifact_verifier_requires_exact_names_and_hashes(tmp_path: Path) -> None:
    artifacts = []
    for name in INTERNAL_ARTIFACT_NAMES[:3]:
        path = tmp_path / name
        path.write_bytes(name.encode("utf-8"))
        artifacts.append(path)
    _write_checksums(artifacts, tmp_path / "SHA256SUMS.txt")
    result = verify_internal_artifacts(tmp_path)
    assert result["pass"] is True
    assert {row["path"] for row in result["artifacts"]} == set(INTERNAL_ARTIFACT_NAMES[:3])

    (tmp_path / "unexpected.exe").write_bytes(b"no")
    with pytest.raises(RuntimeError, match="internal artifact set mismatch"):
        verify_internal_artifacts(tmp_path)


def test_checksum_verifier_rejects_tampering(tmp_path: Path) -> None:
    artifacts = []
    for name in INTERNAL_ARTIFACT_NAMES[:3]:
        path = tmp_path / name
        path.write_bytes(name.encode("utf-8"))
        artifacts.append(path)
    _write_checksums(artifacts, tmp_path / "SHA256SUMS.txt")
    artifacts[0].write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="checksum verification failed"):
        verify_internal_artifacts(tmp_path)
