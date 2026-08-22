"""Single-owner Active Symbol state and stale-result protection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from threading import RLock
from typing import Callable


def normalize_symbol(value: object) -> str:
    symbol = str(value).strip().upper()
    if not symbol:
        raise ValueError("Active Symbol cannot be empty.")
    if len(symbol) > 20 or not all(character.isalnum() or character in {".", "-", "^"} for character in symbol):
        raise ValueError("Active Symbol contains unsupported characters.")
    return symbol


@dataclass(frozen=True, slots=True)
class ActiveSymbolState:
    symbol: str
    generation: int
    source: str
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class SymbolRequest:
    symbol: str
    generation: int
    request_id: int
    source: str
    requested_at: datetime


class ActiveSymbolController:
    """Owns the application's symbol and validates asynchronous results."""

    def __init__(self, initial_symbol: str = "AAPL") -> None:
        now = datetime.now(timezone.utc)
        self._state = ActiveSymbolState(normalize_symbol(initial_symbol), 0, "startup", now)
        self._request_ids = count(1)
        self._listeners: list[Callable[[ActiveSymbolState], None]] = []
        self._lock = RLock()

    @property
    def state(self) -> ActiveSymbolState:
        with self._lock:
            return self._state

    @property
    def symbol(self) -> str:
        return self.state.symbol

    def subscribe(self, listener: Callable[[ActiveSymbolState], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def set(self, symbol: object, *, source: str) -> ActiveSymbolState:
        normalized = normalize_symbol(symbol)
        source_text = str(source).strip() or "unknown"
        with self._lock:
            if normalized == self._state.symbol:
                return self._state
            self._state = ActiveSymbolState(
                symbol=normalized,
                generation=self._state.generation + 1,
                source=source_text,
                changed_at=datetime.now(timezone.utc),
            )
            state = self._state
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(state)
        return state

    def request(self, *, source: str) -> SymbolRequest:
        with self._lock:
            state = self._state
            return SymbolRequest(
                symbol=state.symbol,
                generation=state.generation,
                request_id=next(self._request_ids),
                source=str(source).strip() or "unknown",
                requested_at=datetime.now(timezone.utc),
            )

    def accepts(self, request: SymbolRequest) -> bool:
        state = self.state
        return request.symbol == state.symbol and request.generation == state.generation

