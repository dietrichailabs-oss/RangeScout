"""Thread-safe bounded TTL cache for normalized fabric results."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from app.market_data.contracts import FabricRequest, FabricResult


@dataclass(frozen=True)
class CacheEntry:
    value: FabricResult
    expires_at: datetime


class ResultCache:
    def __init__(self, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._values: OrderedDict[tuple[object, ...], CacheEntry] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def key(request: FabricRequest) -> tuple[object, ...]:
        return (
            request.canonical_instrument_id,
            request.canonical_symbol.upper(),
            request.asset_class.value,
            request.capability.value,
            request.venue,
            request.start,
            request.end,
            request.interval,
            request.adjustment,
        )

    def get(self, request: FabricRequest, now: datetime | None = None) -> FabricResult | None:
        current = now or datetime.now(timezone.utc)
        key = self.key(request)
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            if entry.expires_at <= current:
                del self._values[key]
                return None
            self._values.move_to_end(key)
            return entry.value

    def put(self, request: FabricRequest, result: FabricResult, now: datetime | None = None) -> None:
        ttl = max(0, result.cache_ttl_seconds)
        if ttl == 0:
            return
        current = now or datetime.now(timezone.utc)
        key = self.key(request)
        with self._lock:
            self._values[key] = CacheEntry(result, current + timedelta(seconds=ttl))
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)
