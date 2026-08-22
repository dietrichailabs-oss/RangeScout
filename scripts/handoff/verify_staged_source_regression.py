#!/usr/bin/env python
"""Fail closed when staged tests require repository files omitted from source."""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from app import PRODUCT


_REVIEWED_FILE_SUFFIXES = {
    ".bat",
    ".exe",
    ".ico",
    ".iss",
    ".json",
    ".md",
    ".png",
    ".ps1",
    ".py",
    ".sql",
    ".svg",
    ".toml",
    ".txt",
    ".zip",
}


def _literal_path_tail(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = _literal_path_tail(node.right)
        if not right:
            return []
        return _literal_path_tail(node.left) + right
    return []


def _safe_relative_path(parts: list[str]) -> PurePosixPath | None:
    if not parts or any(not part or "\n" in part or "\r" in part for part in parts):
        return None
    joined = "/".join(part.replace("\\", "/").strip("/") for part in parts)
    if any(token in joined for token in ("*", "?", "{", "}")):
        return None
    pure = PurePosixPath(joined)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    if pure.name != "LICENSE" and pure.suffix.lower() not in _REVIEWED_FILE_SUFFIXES:
        return None
    return pure


def referenced_repository_files(repository_root: Path, staged_source: Path) -> list[str]:
    """Return repository files read by shipped tests and required in staging."""

    repository_root = repository_root.resolve()
    staged_source = staged_source.resolve()
    tests_root = staged_source / "tests"
    if not tests_root.is_dir():
        raise RuntimeError(f"staged tests are missing: {tests_root}")

    referenced: set[str] = set()
    for test_path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(test_path.read_text(encoding="utf-8-sig"), filename=str(test_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            relative = _safe_relative_path(_literal_path_tail(node))
            if relative is None:
                continue
            repository_path = repository_root.joinpath(*relative.parts)
            if repository_path.is_file():
                referenced.add(relative.as_posix())
    return sorted(referenced)


def verify_staged_source_contract(repository_root: Path, staged_source: Path) -> list[str]:
    repository_root = repository_root.resolve()
    staged_source = staged_source.resolve()
    current_documents = (
        "README.md",
        f"RELEASE_NOTES_v{PRODUCT.version}.md",
        "LICENSE",
        "pyproject.toml",
    )
    missing_current = [name for name in current_documents if not (staged_source / name).is_file()]
    if missing_current:
        raise RuntimeError("Current source documents absent from staging: " + ", ".join(missing_current))

    referenced = referenced_repository_files(repository_root, staged_source)
    missing_references = [name for name in referenced if not (staged_source / Path(name)).is_file()]
    if missing_references:
        raise RuntimeError(
            "Shipped tests reference repository files omitted from source staging: "
            + ", ".join(missing_references)
        )
    return referenced


def run_staged_source_regression(staged_source: Path, release_root: Path, pytest_args: list[str]) -> int:
    staged_source = staged_source.resolve()
    release_root = release_root.resolve()
    if not release_root.exists():
        raise RuntimeError(f"release fixture does not exist: {release_root}")
    environment = os.environ.copy()
    environment["RANGESCOUT_RELEASE_ROOT"] = str(release_root)
    command = [sys.executable, "-m", "pytest", *(pytest_args or ["-q"])]
    completed = subprocess.run(command, cwd=staged_source, env=environment, check=False)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--pytest-arg", action="append", default=[])
    args = parser.parse_args()

    referenced = verify_staged_source_contract(args.repository, args.source)
    print(
        "STAGED_SOURCE_CONTRACT=PASS "
        f"current_release_notes=RELEASE_NOTES_v{PRODUCT.version}.md "
        f"test_referenced_files={len(referenced)}"
    )
    if not args.run_tests:
        return 0
    if args.release_root is None:
        raise RuntimeError("--release-root is required with --run-tests")
    return_code = run_staged_source_regression(args.source, args.release_root, args.pytest_arg)
    if return_code:
        print(f"STAGED_SOURCE_REGRESSION=FAIL return_code={return_code}")
        return return_code
    print("STAGED_SOURCE_REGRESSION=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
