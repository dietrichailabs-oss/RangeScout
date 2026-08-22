"""Watchlist persistence and manipulation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from app.domain.errors import ValidationError


@dataclass
class WatchlistRecord:
    id: str
    title: str
    symbols: list[str] = field(default_factory=list)


@dataclass
class WatchlistStore:
    file_path: Path
    watchlists: dict[str, WatchlistRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    @classmethod
    def from_path(cls, path: str | Path) -> "WatchlistStore":
        return cls(file_path=Path(path))

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        for item in raw.get("watchlists", []):
            record = WatchlistRecord(
                id=item["id"],
                title=item["title"],
                symbols=list(item.get("symbols", [])),
            )
            self.watchlists[record.id] = record

    def _save(self) -> None:
        payload = {
            "watchlists": [
                {"id": r.id, "title": r.title, "symbols": r.symbols}
                for r in self.watchlists.values()
            ]
        }
        self.file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def list(self) -> list[WatchlistRecord]:
        return list(self.watchlists.values())

    def create(self, watchlist_id: str, title: str) -> WatchlistRecord:
        if watchlist_id in self.watchlists:
            raise ValidationError(f"Watchlist '{watchlist_id}' already exists.")
        record = WatchlistRecord(id=watchlist_id, title=title)
        self.watchlists[watchlist_id] = record
        self._save()
        return record

    def delete(self, watchlist_id: str) -> None:
        if watchlist_id not in self.watchlists:
            raise ValidationError(f"Watchlist '{watchlist_id}' not found.")
        del self.watchlists[watchlist_id]
        self._save()

    def add_symbol(self, watchlist_id: str, symbol: str) -> WatchlistRecord:
        symbol = symbol.upper()
        if watchlist_id not in self.watchlists:
            raise ValidationError(f"Watchlist '{watchlist_id}' not found.")
        record = self.watchlists[watchlist_id]
        if symbol not in record.symbols:
            record.symbols.append(symbol)
            self._save()
        return record

    def remove_symbol(self, watchlist_id: str, symbol: str) -> WatchlistRecord:
        symbol = symbol.upper()
        if watchlist_id not in self.watchlists:
            raise ValidationError(f"Watchlist '{watchlist_id}' not found.")
        record = self.watchlists[watchlist_id]
        if symbol in record.symbols:
            record.symbols = [s for s in record.symbols if s != symbol]
            self._save()
        return record
