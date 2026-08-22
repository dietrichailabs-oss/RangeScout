"""Deterministic subscription bookkeeping and provider limit enforcement."""

from __future__ import annotations

from dataclasses import dataclass


def normalize_stream_symbol(value: str) -> str:
    symbol = str(value).strip().upper()
    if not symbol or len(symbol) > 16:
        raise ValueError("A valid symbol is required.")
    if not all(character.isalnum() or character in ".-" for character in symbol):
        raise ValueError("Symbol format is invalid.")
    return symbol


@dataclass(frozen=True, slots=True)
class SubscriptionChange:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


class SubscriptionBook:
    def __init__(self, limit: int | None = None) -> None:
        if limit is not None and limit < 1:
            raise ValueError("Subscription limit must be positive.")
        self.limit = limit
        self._symbols: set[str] = set()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._symbols))

    def subscribe(self, symbols: list[str] | tuple[str, ...] | set[str]) -> SubscriptionChange:
        normalized = {normalize_stream_symbol(symbol) for symbol in symbols}
        added = normalized - self._symbols
        if self.limit is not None and len(self._symbols | added) > self.limit:
            raise ValueError(f"The active provider supports at most {self.limit} streaming symbols.")
        self._symbols.update(added)
        return SubscriptionChange(added=tuple(sorted(added)))

    def unsubscribe(self, symbols: list[str] | tuple[str, ...] | set[str]) -> SubscriptionChange:
        normalized = {normalize_stream_symbol(symbol) for symbol in symbols}
        removed = normalized & self._symbols
        self._symbols.difference_update(removed)
        return SubscriptionChange(removed=tuple(sorted(removed)))

    def clear(self) -> SubscriptionChange:
        removed = self.symbols
        self._symbols.clear()
        return SubscriptionChange(removed=removed)
