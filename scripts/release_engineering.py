#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import PRODUCT
from scripts.package_release import (
    PACKAGING_SKIP_DIRS,
    PACKAGING_SKIP_FILENAMES,
    PACKAGING_SKIP_FILE_SUFFIXES,
    ZIP_FIXED_DATETIME,
    build_release_package,
)


INTERNAL_ARTIFACT_NAMES = (
    f"RangeScout_{PRODUCT.version}_Setup.exe",
    f"RangeScout_{PRODUCT.version}_Portable.zip",
    f"RangeScout_{PRODUCT.version}_Source.zip",
    "SHA256SUMS.txt",
)
SETUP_SCRIPT = Path("packaging/windows/RangeScout.iss")


@dataclass(frozen=True)
class ReleaseArtifacts:
    setup: Path
    portable: Path
    source: Path
    checksums: Path
    runtime_root: Path
    runtime_manifest: Path
    authenticode_evidence: Path | None = None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def configure_reproducible_build_environment(source_root: Path) -> dict[str, str]:
    """Bind build timestamps to the immutable source commit."""
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%ct", "HEAD"],
        cwd=str(source_root),
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip().isdigit():
        raise RuntimeError("Unable to derive SOURCE_DATE_EPOCH from the source commit.")
    epoch = completed.stdout.strip()
    timestamp = datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    os.environ["SOURCE_DATE_EPOCH"] = epoch
    os.environ["RANGESCOUT_BUILD_UTC"] = timestamp
    os.environ["RANGESCOUT_ACTUAL_BUILD_UTC"] = timestamp
    os.environ.setdefault("PYTHONHASHSEED", "0")
    return {"SOURCE_DATE_EPOCH": epoch, "build_utc": timestamp}


def _record(path: Path, root: Path) -> dict[str, int | str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }


def runtime_manifest(runtime_root: Path) -> list[dict[str, int | str]]:
    if not runtime_root.is_dir():
        raise FileNotFoundError(f"runtime root not found: {runtime_root}")
    return [_record(path, runtime_root) for path in sorted(runtime_root.rglob("*")) if path.is_file()]


def write_runtime_manifest(runtime_root: Path, output: Path) -> Path:
    rows = runtime_manifest(runtime_root)
    payload = {
        "schema": "rangescout.runtime-payload.v1",
        "version": PRODUCT.version,
        "build_identity": PRODUCT.build_identity,
        "record_count": len(rows),
        "entries": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output


def _write_portable_zip(runtime_root: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(runtime_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(runtime_root)
            if any(part in PACKAGING_SKIP_DIRS for part in relative.parts):
                continue
            if path.name in PACKAGING_SKIP_FILENAMES:
                continue
            if any(path.name.endswith(suffix) for suffix in PACKAGING_SKIP_FILE_SUFFIXES):
                continue
            info = zipfile.ZipInfo(relative.as_posix(), ZIP_FIXED_DATETIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output


def _resolve_iscc(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env_path = os.environ.get("INNO_ISCC")
    if env_path:
        candidates.append(Path(env_path))
    found = shutil.which("ISCC.exe") or shutil.which("iscc.exe")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
            Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "Inno Setup 6 compiler not found. Install Inno Setup and/or set INNO_ISCC."
    )


def _resolve_signtool(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env_path = os.environ.get("WINDOWS_SIGNTOOL")
    if env_path:
        candidates.append(Path(env_path))
    found = shutil.which("signtool.exe")
    if found:
        candidates.append(Path(found))
    kits = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    if kits.is_dir():
        candidates.extend(sorted(kits.glob("*/x64/signtool.exe"), reverse=True))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("Windows SignTool not found. Set WINDOWS_SIGNTOOL.")


def _authenticode_details(path: Path) -> dict[str, object]:
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:RANGESCOUT_AUTHENTICODE_PATH;"
        "[pscustomobject]@{Status=[string]$s.Status;StatusMessage=$s.StatusMessage;"
        "SignatureType=[string]$s.SignatureType;"
        "SignerSubject=if($s.SignerCertificate){$s.SignerCertificate.Subject}else{''};"
        "SignerThumbprint=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{''};"
        "TimeStamperSubject=if($s.TimeStamperCertificate){$s.TimeStamperCertificate.Subject}else{''}}"
        "|ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment["RANGESCOUT_AUTHENTICODE_PATH"] = str(path.resolve())
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Authenticode inspection failed: {completed.stderr}")
    return json.loads(completed.stdout)


def sign_self_signed(
    path: Path,
    *,
    thumbprint: str,
    timestamp_url: str,
    signtool: Path | None = None,
) -> dict[str, object]:
    normalized_thumbprint = "".join(thumbprint.split()).upper()
    if len(normalized_thumbprint) != 40:
        raise ValueError("signing thumbprint must be a SHA-1 certificate thumbprint")
    tool = _resolve_signtool(signtool)
    command = [
        str(tool), "sign", "/sha1", normalized_thumbprint, "/s", "My",
        "/fd", "SHA256", "/tr", timestamp_url, "/td", "SHA256",
        "/d", f"RangeScout {PRODUCT.version}", str(path.resolve()),
    ]
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"Authenticode signing failed for {path.name}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    details = _authenticode_details(path)
    if str(details.get("SignerThumbprint", "")).upper() != normalized_thumbprint:
        raise RuntimeError(f"unexpected Authenticode signer for {path.name}: {details}")
    if not details.get("TimeStamperSubject"):
        raise RuntimeError(f"timestamp missing from Authenticode signature: {path.name}")
    return {
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": sha256(path),
        "signing_mode": "self-signed",
        "publicly_trusted": False,
        "timestamp_url": timestamp_url,
        **details,
    }


def compile_setup(
    *,
    source_root: Path,
    runtime_root: Path,
    output_dir: Path,
    iscc: Path | None = None,
) -> Path:
    compiler = _resolve_iscc(iscc)
    script = (source_root / SETUP_SCRIPT).resolve()
    if not script.is_file():
        raise FileNotFoundError(f"Inno Setup script not found: {script}")
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = output_dir / f"RangeScout_{PRODUCT.version}_Setup.exe"
    if expected.exists():
        expected.unlink()
    command = [
        str(compiler),
        "/Qp",
        f"/DRuntimeRoot={runtime_root.resolve()}",
        f"/DOutputDir={output_dir.resolve()}",
        f"/DAppVersion={PRODUCT.version}",
        f"/DAppPublisher={PRODUCT.publisher}",
        f"/DBuildIdentity={PRODUCT.build_identity}",
        f"/DSetupBaseFilename=RangeScout_{PRODUCT.version}_Setup",
        f"/DAppIcon={(source_root / 'resources' / 'rangescout.ico').resolve()}",
        str(script),
    ]
    completed = subprocess.run(
        command,
        cwd=str(source_root),
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Inno Setup compilation failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    if not expected.is_file():
        raise RuntimeError(f"Inno Setup did not create: {expected}")
    return expected


def _write_checksums(paths: Iterable[Path], output: Path) -> Path:
    lines = [f"{sha256(path)}  {path.name}" for path in paths]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def verify_internal_artifacts(output_dir: Path) -> dict[str, object]:
    actual = sorted(path.name for path in output_dir.iterdir() if path.is_file())
    expected = sorted(INTERNAL_ARTIFACT_NAMES)
    if actual != expected:
        raise RuntimeError(f"internal artifact set mismatch: expected={expected} actual={actual}")
    checksums = output_dir / "SHA256SUMS.txt"
    records: list[dict[str, object]] = []
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(None, 1)
        path = output_dir / filename.strip()
        if not path.is_file() or sha256(path) != digest.lower():
            raise RuntimeError(f"checksum verification failed: {filename}")
        records.append({"path": path.name, "size": path.stat().st_size, "sha256": digest.lower()})
    if sorted(row["path"] for row in records) != sorted(INTERNAL_ARTIFACT_NAMES[:3]):
        raise RuntimeError("SHA256SUMS does not cover exactly the three release artifacts")
    return {"pass": True, "artifacts": records, "checksum_file": checksums.name}


def build_release_set(
    *,
    source_root: Path,
    output_dir: Path,
    work_dir: Path,
    iscc: Path | None = None,
    signing_thumbprint: str | None = None,
    timestamp_url: str = "http://timestamp.digicert.com",
    signtool: Path | None = None,
) -> ReleaseArtifacts:
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    work_dir = work_dir.resolve()
    if PRODUCT.version != "1.6.1" or not PRODUCT.build_identity.startswith("rs-v1.6.1-"):
        raise RuntimeError("RangeScout 1.6.1 release engineering requires an immutable 1.6.1 build identity")
    configure_reproducible_build_environment(source_root)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    legacy_dir = work_dir / "runtime_build"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    _legacy_zip, _manifest_path = build_release_package(
        source_root=source_root,
        dist_dir=legacy_dir,
        version=PRODUCT.version,
        with_executable=True,
    )
    runtime_root = legacy_dir / f"RangeScout-{PRODUCT.version}-windows"
    legacy_source = legacy_dir / f"RangeScout-{PRODUCT.version}-source.zip"
    signing_records: list[dict[str, object]] = []
    if signing_thumbprint:
        signing_records.append(sign_self_signed(
            runtime_root / "RangeScout.exe",
            thumbprint=signing_thumbprint,
            timestamp_url=timestamp_url,
            signtool=signtool,
        ))
    runtime_manifest_path = write_runtime_manifest(
        runtime_root,
        work_dir / f"RangeScout_{PRODUCT.version}_RuntimeManifest.json",
    )

    portable = _write_portable_zip(runtime_root, output_dir / f"RangeScout_{PRODUCT.version}_Portable.zip")
    source = output_dir / f"RangeScout_{PRODUCT.version}_Source.zip"
    shutil.copy2(legacy_source, source)
    setup = compile_setup(
        source_root=source_root,
        runtime_root=runtime_root,
        output_dir=output_dir,
        iscc=iscc,
    )
    if signing_thumbprint:
        signing_records.append(sign_self_signed(
            setup,
            thumbprint=signing_thumbprint,
            timestamp_url=timestamp_url,
            signtool=signtool,
        ))
    checksums = _write_checksums((setup, portable, source), output_dir / "SHA256SUMS.txt")
    verify_internal_artifacts(output_dir)
    authenticode_evidence = None
    if signing_records:
        authenticode_evidence = work_dir / f"RangeScout_{PRODUCT.version}_Authenticode.json"
        authenticode_evidence.write_text(json.dumps({
            "schema": "rangescout.authenticode.v1",
            "publisher": PRODUCT.publisher,
            "build_identity": PRODUCT.build_identity,
            "signing_mode": "self-signed",
            "publicly_trusted": False,
            "artifacts": signing_records,
        }, indent=2, sort_keys=True), encoding="utf-8")
    return ReleaseArtifacts(
        setup=setup,
        portable=portable,
        source=source,
        checksums=checksums,
        runtime_root=runtime_root,
        runtime_manifest=runtime_manifest_path,
        authenticode_evidence=authenticode_evidence,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the RangeScout 1.6.1 internal QA artifact set.")
    parser.add_argument("--source", default=str(REPO_ROOT))
    parser.add_argument("--output", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--iscc")
    parser.add_argument("--signing-thumbprint")
    parser.add_argument("--timestamp-url", default="http://timestamp.digicert.com")
    parser.add_argument("--signtool")
    args = parser.parse_args()
    artifacts = build_release_set(
        source_root=Path(args.source),
        output_dir=Path(args.output),
        work_dir=Path(args.work),
        iscc=Path(args.iscc) if args.iscc else None,
        signing_thumbprint=args.signing_thumbprint,
        timestamp_url=args.timestamp_url,
        signtool=Path(args.signtool) if args.signtool else None,
    )
    print(json.dumps({
        "setup": str(artifacts.setup),
        "portable": str(artifacts.portable),
        "source": str(artifacts.source),
        "checksums": str(artifacts.checksums),
        "runtime_root": str(artifacts.runtime_root),
        "runtime_manifest": str(artifacts.runtime_manifest),
        "authenticode_evidence": str(artifacts.authenticode_evidence) if artifacts.authenticode_evidence else None,
        "verification": verify_internal_artifacts(artifacts.setup.parent),
    }, indent=2))


if __name__ == "__main__":
    main()
