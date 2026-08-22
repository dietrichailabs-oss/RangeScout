"""Peer comparison helpers intentionally require explicit comparable data."""

from __future__ import annotations

from app.research.models import ResearchValue


def unavailable_peers() -> dict[str, ResearchValue]:
    return {"Peers": ResearchValue.unavailable("SEC company profiles", "No deterministic peer universe is available for this company.")}


CURATED_PEERS: dict[str, tuple[str, ...]] = {
    "BA": ("LMT", "GD", "NOC", "RTX"),
    "NVDA": ("AMD", "AVGO", "INTC", "QCOM"),
    "AAPL": ("MSFT", "GOOGL", "META", "AMZN"),
    "MSFT": ("AAPL", "GOOGL", "ORCL", "AMZN"),
}


def curated_peers(symbol: str) -> tuple[str, ...]:
    return CURATED_PEERS.get(symbol.strip().upper(), ())
