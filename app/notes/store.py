"""Local note persistence with backward-compatible create/edit/category semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    symbol: str
    text: str
    created_at: str
    category: str = "Research Notes"
    modified_at: str | None = None


class NoteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._notes: list[dict] = []
        self._load()

    def _load(self) -> None:
        self._notes = []
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in payload.get("notes", []):
            if not isinstance(item, dict):
                continue
            created = str(item.get("created_at") or datetime.now(timezone.utc).isoformat())
            self._notes.append({
                "id": str(item.get("id") or uuid4()),
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "text": str(item.get("text") or ""),
                "created_at": created,
                "category": str(item.get("category") or "Research Notes"),
                "modified_at": str(item.get("modified_at") or created),
            })

    def _save(self) -> None:
        self.path.write_text(json.dumps({"notes": self._notes}, indent=2), encoding="utf-8")

    def reload(self) -> None:
        self._load()

    def add(self, symbol: str, text: str, category: str = "Research Notes") -> Note:
        stamp = datetime.now(timezone.utc).isoformat()
        note = Note(id=str(uuid4()), symbol=symbol.upper(), text=text, created_at=stamp, category=category, modified_at=stamp)
        self._notes.append(self._as_dict(note))
        self._save()
        return note

    def update(self, note_id: str, *, symbol: str, text: str, category: str) -> Note:
        for index, item in enumerate(self._notes):
            if item["id"] != note_id:
                continue
            note = Note(
                id=note_id,
                symbol=symbol.upper(),
                text=text,
                created_at=item["created_at"],
                category=category,
                modified_at=datetime.now(timezone.utc).isoformat(),
            )
            self._notes[index] = self._as_dict(note)
            self._save()
            return note
        raise KeyError(note_id)

    def delete(self, note_id: str) -> bool:
        before = len(self._notes)
        self._notes = [item for item in self._notes if item["id"] != note_id]
        changed = len(self._notes) != before
        if changed:
            self._save()
        return changed

    def get(self, note_id: str) -> Note | None:
        item = next((item for item in self._notes if item["id"] == note_id), None)
        return Note(**item) if item else None

    def list_for(self, symbol: str | None = None, category: str | None = None) -> list[Note]:
        normalized = str(symbol or "").upper()
        values = [
            Note(**item) for item in self._notes
            if (not normalized or item["symbol"] == normalized)
            and (not category or item["category"] == category)
        ]
        return sorted(values, key=lambda note: note.modified_at or note.created_at, reverse=True)

    @staticmethod
    def _as_dict(note: Note) -> dict[str, str | None]:
        return {field: getattr(note, field) for field in ("id", "symbol", "text", "created_at", "category", "modified_at")}
