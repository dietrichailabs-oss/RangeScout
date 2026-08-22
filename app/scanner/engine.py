"""Local scanner constrained to already available/subscribed symbols."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ScannerObservation:
    symbol: str
    price: Decimal
    vwap: Decimal | None = None
    previous_price: Decimal | None = None
    opening_range_high: Decimal | None = None
    opening_range_low: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    rvol: Decimal | None = None
    gap_percent: Decimal | None = None
    volatility_ratio: Decimal | None = None
    news_catalyst: bool = False
    sec_catalyst: bool = False
    government_catalyst: bool = False
    halt_status: str | None = None


@dataclass(frozen=True, slots=True)
class ScanHit:
    symbol: str
    rule: str
    detail: str


def scan_observations(observations: list[ScannerObservation], allowed_symbols: set[str]) -> list[ScanHit]:
    allowed = {symbol.upper() for symbol in allowed_symbols}
    hits: list[ScanHit] = []
    for item in observations:
        if item.symbol.upper() not in allowed:
            continue
        if item.rvol is not None and item.rvol >= Decimal("2"): hits.append(ScanHit(item.symbol, "unusual_volume", f"RVOL {item.rvol:.2f}"))
        if item.gap_percent is not None and abs(item.gap_percent) >= Decimal("3"): hits.append(ScanHit(item.symbol, "gap", f"Gap {item.gap_percent:+.2f}%"))
        if item.vwap is not None and item.previous_price is not None and (item.previous_price - item.vwap) * (item.price - item.vwap) <= 0 and item.previous_price != item.price: hits.append(ScanHit(item.symbol, "vwap_cross", f"Price crossed VWAP {item.vwap}"))
        if item.opening_range_high is not None and item.price > item.opening_range_high: hits.append(ScanHit(item.symbol, "opening_range_break", "Above opening range"))
        elif item.opening_range_low is not None and item.price < item.opening_range_low: hits.append(ScanHit(item.symbol, "opening_range_break", "Below opening range"))
        if item.day_high is not None and item.price >= item.day_high: hits.append(ScanHit(item.symbol, "new_day_high", f"Day high {item.day_high}"))
        if item.day_low is not None and item.price <= item.day_low: hits.append(ScanHit(item.symbol, "new_day_low", f"Day low {item.day_low}"))
        if item.volatility_ratio is not None and item.volatility_ratio >= Decimal("2"): hits.append(ScanHit(item.symbol, "volatility_spike", f"Volatility {item.volatility_ratio:.2f}x"))
        if item.news_catalyst: hits.append(ScanHit(item.symbol, "news_catalyst", "Official/news catalyst"))
        if item.sec_catalyst: hits.append(ScanHit(item.symbol, "sec_catalyst", "SEC filing catalyst"))
        if item.government_catalyst: hits.append(ScanHit(item.symbol, "government_catalyst", "Government catalyst"))
        if item.halt_status: hits.append(ScanHit(item.symbol, "halt_resumption", item.halt_status))
    return sorted(hits, key=lambda hit: (hit.symbol, hit.rule, hit.detail))


def permitted_scan_universe(active_symbol: str, subscriptions: tuple[str, ...], watchlist_symbols: list[str]) -> set[str]:
    return {value.strip().upper() for value in (active_symbol, *subscriptions, *watchlist_symbols) if value.strip()}
