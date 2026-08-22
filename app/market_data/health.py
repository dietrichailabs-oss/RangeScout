"""Bounded contextual provider health and circuit-breaker state."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from statistics import median
from threading import RLock


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class HealthWindow:
    max_samples: int = 200
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    outcomes: deque[str] = field(default_factory=deque)
    latencies_ms: deque[float] = field(default_factory=deque)
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    opened_at: datetime | None = None
    rate_limited_until: datetime | None = None
    state: CircuitState = CircuitState.CLOSED

    def _trim(self) -> None:
        while len(self.outcomes) > self.max_samples:
            self.outcomes.popleft()
        while len(self.latencies_ms) > self.max_samples:
            self.latencies_ms.popleft()

    def allow(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if self.rate_limited_until and current < self.rate_limited_until:
            return False
        if self.state == CircuitState.OPEN:
            if self.opened_at and current - self.opened_at >= timedelta(seconds=self.cooldown_seconds):
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True

    def success(self, latency_ms: float, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        self.outcomes.append("success")
        self.latencies_ms.append(max(0.0, latency_ms))
        self.consecutive_failures = 0
        self.last_success = current
        self.state = CircuitState.CLOSED
        self.opened_at = None
        self._trim()

    def failure(self, kind: str, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        self.outcomes.append(kind)
        self.consecutive_failures += 1
        self.last_failure = current
        if self.consecutive_failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = current
        self._trim()

    def rate_limited(self, retry_after_seconds: float | None, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        delay = max(1.0, retry_after_seconds or self.cooldown_seconds)
        self.rate_limited_until = current + timedelta(seconds=delay)
        self.failure("rate_limited", current)

    def metrics(self) -> dict[str, object]:
        total = len(self.outcomes)
        successes = sum(item == "success" for item in self.outcomes)
        sorted_latencies = sorted(self.latencies_ms)
        p95_index = max(0, int(len(sorted_latencies) * 0.95) - 1)
        return {
            "request_count": total,
            "success_rate": successes / total if total else 0.0,
            "timeout_rate": sum(item == "timeout" for item in self.outcomes) / total if total else 0.0,
            "parse_failure_rate": sum(item == "parse" for item in self.outcomes) / total if total else 0.0,
            "stale_rate": sum(item == "stale" for item in self.outcomes) / total if total else 0.0,
            "validation_failure_rate": sum(item == "validation" for item in self.outcomes) / total if total else 0.0,
            "p50_latency_ms": median(sorted_latencies) if sorted_latencies else None,
            "p95_latency_ms": sorted_latencies[p95_index] if sorted_latencies else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "consecutive_failures": self.consecutive_failures,
            "rate_limited_until": self.rate_limited_until.isoformat() if self.rate_limited_until else None,
            "circuit_state": self.state.value,
        }


class ProviderHealth:
    def __init__(self, max_samples: int = 200, failure_threshold: int = 3, cooldown_seconds: float = 30.0):
        self._windows: dict[tuple[str, str, str], HealthWindow] = {}
        self._lock = RLock()
        self._settings = (max_samples, failure_threshold, cooldown_seconds)

    def window(self, provider_id: str, asset_class: str, capability: str) -> HealthWindow:
        key = (provider_id, asset_class, capability)
        with self._lock:
            if key not in self._windows:
                self._windows[key] = HealthWindow(*self._settings)
            return self._windows[key]

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {"|".join(key): value.metrics() for key, value in self._windows.items()}
