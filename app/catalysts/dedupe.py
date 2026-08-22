from __future__ import annotations

from collections import OrderedDict

from app.catalysts.entities import CatalystEvent


class EventDeduplicator:
    def __init__(self, capacity: int = 5000) -> None:
        self.capacity = max(100, capacity)
        self._seen: OrderedDict[str, None] = OrderedDict()

    def accept(self, event: CatalystEvent) -> bool:
        if event.event_id in self._seen:
            return False
        self._seen[event.event_id] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True
