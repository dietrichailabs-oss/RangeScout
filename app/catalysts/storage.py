from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.catalysts.entities import CatalystEvent


class CatalystStore:
    def __init__(self, path: Path, maximum_events: int = 1000) -> None:
        self.path = path
        self.maximum_events = maximum_events

    def save(self, events: list[CatalystEvent]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = [event.without_restricted_content() for event in events[-self.maximum_events:]]
        payload = []
        for event in safe:
            row = asdict(event)
            row["published_at"] = event.published_at.isoformat()
            row["received_at"] = event.received_at.isoformat()
            row["relevance"] = event.relevance.value
            payload.append(row)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
