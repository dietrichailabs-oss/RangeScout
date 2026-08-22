from __future__ import annotations

import hashlib
import os
import importlib.util
from pathlib import Path
import shutil
import pytest

from app import PRODUCT
from scripts.package_release import build_release_package


def _sha(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def test_cp5_release_and_source_hashes_match_between_independent_builds(tmp_path: Path) -> None:
    if importlib.util.find_spec("PyInstaller") is None:
        pytest.skip("PyInstaller required for deterministic build comparison.")
    root = Path(__file__).resolve().parents[2]
    os.environ["SOURCE_DATE_EPOCH"] = "1700000000"
    dist = tmp_path / "deterministic"
    dist.mkdir()

    def _build_once() -> tuple[Path, str, str]:
        release_build, _ = build_release_package(
            source_root=root,
            dist_dir=dist,
            version=PRODUCT.version,
            with_executable=True,
        )
        source_build = dist / f"RangeScout-{PRODUCT.version}-source.zip"
        return release_build, _sha(release_build), _sha(source_build)

    release_build_a, release_sha_a, source_sha_a = _build_once()

    for path in (
        dist / f"RangeScout-{PRODUCT.version}-windows",
        dist / "staging-runtime",
        dist / "staging-source",
        dist / "work",
        dist / "spec",
    ):
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    release_build_b, release_sha_b, source_sha_b = _build_once()

    assert release_build_a.exists()
    assert release_build_b.exists()
    assert release_sha_a == release_sha_b
    assert source_sha_a == source_sha_b
