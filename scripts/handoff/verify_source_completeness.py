#!/usr/bin/env python
"""Fail closed when a frozen handoff cannot reconstruct declared inputs/changes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath


def verify_changed_files(changed_files: Path, staged_source: Path) -> list[str]:
    declared = [line.strip().replace("\\", "/") for line in changed_files.read_text(encoding="utf-8").splitlines() if line.strip()]
    unsafe = [path for path in declared if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts]
    if unsafe:
        raise RuntimeError("CHANGED_FILES contains unsafe paths: " + ", ".join(unsafe))
    missing = [path for path in declared if not (staged_source / Path(*PurePosixPath(path).parts)).is_file()]
    if missing:
        raise RuntimeError("Changed tracked files absent from staged source: " + ", ".join(missing))
    return declared


def verify_input_manifest(manifest_path: Path, input_root: Path) -> list[str]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for row in payload.get("entries", []):
        path = str(row.get("path", "")).replace("\\", "/")
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts:
            raise RuntimeError(f"Input manifest contains an unsafe path: {path!r}")
        if not (input_root / Path(*pure.parts)).is_file():
            missing.append(path)
    if missing:
        raise RuntimeError("Input manifest files absent from evidence: " + ", ".join(missing))
    return [str(row["path"]) for row in payload.get("entries", [])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    args = parser.parse_args()
    changed = verify_changed_files(args.changed_files, args.source)
    inputs = verify_input_manifest(args.input_manifest, args.input_root)
    print(f"SOURCE_RECONSTRUCTABILITY=PASS changed={len(changed)} inputs={len(inputs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
