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
    if len(symbol) > 32 or not all(character.isalnum() or character in {".", "-", "^", "/", "$"} for character in symbol):
        raise ValueError("Active Symbol contains unsupported characters.")
    return symbol


@dataclass(frozen=True, slots=True)
class ActiveSymbolState:
    symbol: str
    generation: int
    source: str
    changed_at: datetime
    instrument_id: int | None = None
    name: str = ""
    venue: str = ""
    asset_class: str = "unknown"
    provider_symbols: tuple[tuple[str, str], ...] = ()
    subtype: str = ""


@dataclass(frozen=True, slots=True)
class SymbolRequest:
    symbol: str
    generation: int
    request_id: int
    source: str
    requested_at: datetime

    instrument_id: int | None = None
    venue: str = ""
    asset_class: str = "unknown"
    provider_symbols: tuple[tuple[str, str], ...] = ()
    subtype: str = ""

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

    def set(
        self, symbol: object, *, source: str, instrument_id: int | None = None,
        name: str = "", venue: str = "", asset_class: str = "unknown",
        provider_symbols: tuple[tuple[str, str], ...] = (), subtype: str = "",
    ) -> ActiveSymbolState:
        normalized = normalize_symbol(symbol)
        source_text = str(source).strip() or "unknown"
        with self._lock:
            same_identity = instrument_id is None or instrument_id == self._state.instrument_id
            if normalized == self._state.symbol and same_identity:
                return self._state
            self._state = ActiveSymbolState(
                symbol=normalized,
                generation=self._state.generation + 1,
                source=source_text,
                changed_at=datetime.now(timezone.utc),
                instrument_id=instrument_id,
                name=str(name),
                venue=str(venue),
                asset_class=str(asset_class or "unknown"),
                provider_symbols=tuple(provider_symbols),
                subtype=str(subtype),
            )
            state = self._state
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(state)
        return state

    def request(self, *, source: str = "active-symbol-request") -> SymbolRequest:
        with self._lock:
            state = self._state
            return SymbolRequest(
                symbol=state.symbol,
                generation=state.generation,
                request_id=next(self._request_ids),
                source=str(source).strip() or "unknown",
                requested_at=datetime.now(timezone.utc),
                instrument_id=state.instrument_id,
                venue=state.venue,
                asset_class=state.asset_class,
                provider_symbols=state.provider_symbols,
                subtype=state.subtype,
            )

    def accepts(self, request: SymbolRequest) -> bool:
        state = self.state
        return request.symbol == state.symbol and request.generation == state.generation


    def is_current(self, request: SymbolRequest) -> bool:
        return self.accepts(request)
