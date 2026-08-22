"""Bounded exponential reconnect policy with deterministic jitter injection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    initial_seconds: float = 1.0
    maximum_seconds: float = 30.0
    multiplier: float = 2.0
    jitter_fraction: float = 0.15
    maximum_attempts: int | None = None

    def delay(self, attempt: int, random_value: Callable[[], float] = lambda: 0.5) -> float:
        if attempt < 1:
            raise ValueError("Reconnect attempts begin at one.")
        base = min(self.maximum_seconds, self.initial_seconds * self.multiplier ** (attempt - 1))
        jitter = base * self.jitter_fraction * ((random_value() * 2.0) - 1.0)
        return max(0.0, min(self.maximum_seconds, base + jitter))

    def permits(self, attempt: int) -> bool:
        return self.maximum_attempts is None or attempt <= self.maximum_attempts
