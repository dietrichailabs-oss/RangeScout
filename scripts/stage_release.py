#!/usr/bin/env python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app import PRODUCT
from app.platform import platform_adapter
from scripts.build_manifest import write_detailed_manifest

SOURCE_ALLOWLIST = {
    "app",
    "docs",
    "packaging",
    "resources",
    "scripts",
    "tests",
    "README.md",
    f"RELEASE_NOTES_v{PRODUCT.version}.md",
    "LICENSE",
    "pyproject.toml",
}

RELEASE_ALLOWLIST = {
    "app",
    "resources",
    "docs",
    "README.md",
    f"RELEASE_NOTES_v{PRODUCT.version}.md",
    "LICENSE",
    "pyproject.toml",
}

EXCLUDE_DIR_NAMES = {
    ".git",
    ".github",
    ".pytest_cache",
    ".mypy_cache",
    "__pycache__",
    "release",
    "tmp_cp4_verify8",
    "tmp_restore",
    ".venv",
    "build",
    "dist",
}


def _git_head(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root),
                text=True,
            ).strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def _git_tree_hash(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=str(repo_root),
                text=True,
            ).strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def _git_status(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
                text=True,
            ).strip()
            or "clean"
        )
    except Exception:
        return "unknown"



def _ignore_names(path: Path, names: list[str]) -> list[str]:
    ignored = []
    for name in names:
        if (
            name in EXCLUDE_DIR_NAMES
            or name == "__pycache__"
            or name.endswith(".pyc")
            or name == ".DS_Store"
        ):
            ignored.append(name)
    return ignored


def _safe_unlink(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass
    try:
        path.unlink()
    except OSError as exc:
        if path.is_dir():
            raise
        raise


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return

    def on_error(func: callable, path_str: str, exc_info) -> None:  # noqa: ARG001
        target = Path(path_str)
        try:
            if target.is_dir():
                target.chmod(0o700)
            else:
                target.chmod(0o600)
            func(path_str)
        except OSError:
            pass

    if path.is_symlink():
        path.unlink(missing_ok=True)
        return

    if path.is_dir():
        shutil.rmtree(path, onerror=on_error)
    else:
        _safe_unlink(path)


def _clear_dir(root: Path) -> None:
    if not root.exists():
        return
    for item in sorted(root.iterdir(), key=lambda x: str(x).lower(), reverse=True):
        if item.is_symlink() or item.is_file():
            try:
                _safe_unlink(item)
            except OSError:
                _safe_rmtree(item)
        else:
            _safe_rmtree(item)


def stage_release(
    src_root: Path,
    staging_root: Path,
    *,
    include_tests: bool = False,
) -> None:
    staging_root.mkdir(parents=True, exist_ok=True)
    if staging_root.exists():
        _clear_dir(staging_root)

    allowlist = set(SOURCE_ALLOWLIST if include_tests else RELEASE_ALLOWLIST)
    for rel in sorted(allowlist):
        source = src_root / rel
        if source.is_dir():
            shutil.copytree(source, staging_root / source.name, ignore=_ignore_names)
        elif source.exists():
            shutil.copy2(source, staging_root / source.name)

    readme = staging_root / "README_FIRST.md"
    readme.write_text(
        f"RangeScout {PRODUCT.version} {PRODUCT.build_identity} staging source.\n"
        f"build={PRODUCT.build_identity}\n"
        f"committed_git_head={_git_head(src_root)}\n",
        encoding="utf-8",
    )

    source_identity = staging_root / "SOURCE_IDENTITY.txt"
    source_identity.write_text(
        f"name={PRODUCT.name}\n"
        f"version={PRODUCT.version}\n"
        f"build={PRODUCT.build_identity}\n"
        f"git_tree={_git_tree_hash(src_root)}\n"
        f"git_status={_git_status(src_root)}\n"
        f"git_head={_git_head(src_root)}\n"
        f"platform={platform_adapter().app_name}\n",
        encoding="utf-8",
    )

    write_detailed_manifest(staging_root, staging_root / "manifest_details.json")
    write_detailed_manifest(staging_root, staging_root / "manifest.json")


if __name__ == "__main__":
    stage_release(Path(__file__).resolve().parents[1], Path("release/staging"), include_tests=True)
    print("Staging complete.")
