"""Bounded local-only recent Active Symbol history."""

from __future__ import annotations


class RecentSymbols:
    def __init__(self, symbols=(), *, limit: int = 12) -> None:
        self.limit = max(1, min(50, int(limit)))
        self._symbols: list[str] = []
        for symbol in reversed(tuple(symbols)):
            self.add(symbol)

    def add(self, symbol: str) -> tuple[str, ...]:
        normalized = str(symbol).strip().upper()
        if not normalized:
            return self.values
        self._symbols = [item for item in self._symbols if item != normalized]
        self._symbols.insert(0, normalized)
        del self._symbols[self.limit :]
        return self.values

    def clear(self) -> None:
        self._symbols.clear()

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(self._symbols)
