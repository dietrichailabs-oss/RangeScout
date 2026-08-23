"""Ticker-ribbon subscription planning without per-symbol HTTP polling."""

from __future__ import annotations

from dataclasses import dataclass

from app.streaming.subscriptions import normalize_stream_symbol


@dataclass(frozen=True, slots=True)
class TickerSubscriptionPlan:
    subscribed: tuple[str, ...]
    overflow: tuple[str, ...]
    limit: int | None


def plan_ticker_subscriptions(symbols: list[str] | tuple[str, ...], limit: int | None) -> TickerSubscriptionPlan:
    streamable: list[str] = []
    for symbol in symbols:
        try:
            normalized = normalize_stream_symbol(symbol)
        except ValueError:
            continue
        if normalized not in streamable:
            streamable.append(normalized)
    unique = tuple(streamable)
    if limit is None:
        return TickerSubscriptionPlan(unique, (), None)
    return TickerSubscriptionPlan(unique[:limit], unique[limit:], limit)
