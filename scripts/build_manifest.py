#!/usr/bin/env python
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dataclasses import dataclass


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_manifest(staging_root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for entry in sorted(staging_root.rglob("*")):
        if entry.is_file():
            rel = str(entry.relative_to(staging_root)).replace("\\", "/")
            manifest[rel] = file_sha256(entry)
    return manifest


@dataclass(frozen=True)
class ManifestRecord:
    path: str
    size: int
    sha256: str


def build_manifest_records(staging_root: Path) -> list[ManifestRecord]:
    records: list[ManifestRecord] = []
    for entry in sorted(staging_root.rglob("*")):
        if entry.is_file():
            rel = str(entry.relative_to(staging_root)).replace("\\", "/")
            stat = entry.stat()
            records.append(ManifestRecord(path=rel, size=stat.st_size, sha256=file_sha256(entry)))
    return records


def write_detailed_manifest(staging_root: Path, out_file: Path) -> list[ManifestRecord]:
    records = build_manifest_records(staging_root)
    payload = {
        "files": [
            {
                "path": item.path,
                "size": item.size,
                "sha256": item.sha256,
            }
            for item in records
        ]
    }
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return records


def write_manifest(staging_root: Path, out_file: Path) -> None:
    manifest = build_manifest(staging_root)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    root = Path("release/staging")
    write_manifest(root, Path("release/manifest.json"))
    print(f"Wrote manifest for {len(list(root.rglob('*')))} paths")
