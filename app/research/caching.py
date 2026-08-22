"""Bounded on-disk JSON cache for public SEC responses."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ResearchCache:
    def __init__(self, root: Path, *, max_entries: int = 128, max_bytes: int = 64 * 1024 * 1024, max_age_hours: int = 24) -> None:
        self.root = root
        self.max_entries = max(1, max_entries)
        self.max_bytes = max(1024, max_bytes)
        self.max_age = timedelta(hours=max(1, max_age_hours))

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored = datetime.fromisoformat(payload["stored_at"])
            if datetime.now(timezone.utc) - stored > self.max_age:
                path.unlink(missing_ok=True)
                return None
            return payload["value"]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        temporary = path.with_suffix(".tmp")
        payload = {"stored_at": datetime.now(timezone.utc).isoformat(), "value": value}
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
        self.prune()

    def prune(self) -> None:
        if not self.root.exists():
            return
        files = sorted((item for item in self.root.glob("*.json") if item.is_file()), key=lambda item: item.stat().st_mtime, reverse=True)
        total = 0
        for index, path in enumerate(files):
            try:
                size = path.stat().st_size
                total += size
                if index >= self.max_entries or total > self.max_bytes:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

