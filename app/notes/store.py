"""Minimal local note persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path


@dataclass(frozen=True)
class Note:
    id: str
    symbol: str
    text: str
    created_at: str


class NoteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._notes: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self._notes = payload.get("notes", [])

    def _save(self) -> None:
        self.path.write_text(json.dumps({"notes": self._notes}, indent=2), encoding="utf-8")

    def add(self, symbol: str, text: str) -> Note:
        note = Note(id=f"{symbol.upper()}-{datetime.now(timezone.utc).timestamp():.0f}", symbol=symbol.upper(), text=text, created_at=datetime.now(timezone.utc).isoformat())
        self._notes.append(note.__dict__)
        self._save()
        return note

    def list_for(self, symbol: str) -> list[Note]:
        symbol = symbol.upper()
        return [Note(**item) for item in self._notes if item["symbol"] == symbol]
