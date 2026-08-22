"""Local scanner constrained to already available/subscribed symbols."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime


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


@dataclass(frozen=True, slots=True)
class ScannerRow:
    symbol: str
    company: str
    price: Decimal
    change: Decimal | None = None
    change_percent: Decimal | None = None
    volume: int | None = None
    relative_volume: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    vwap: Decimal | None = None
    breakout_state: str | None = None
    catalyst: bool = False
    halt_state: str | None = None
    freshness: str = "Latest Available"
    sources: tuple[str, ...] = ()
    updated_at: datetime | None = None


def aggregate_scanner_rows(rows: list[ScannerRow]) -> list[ScannerRow]:
    """Merge progressive provider rows field-by-field without fabricating missing values."""
    merged: dict[str, ScannerRow] = {}
    floor = datetime.min
    for row in rows:
        symbol = row.symbol.strip().upper()
        previous = merged.get(symbol)
        if previous is None:
            merged[symbol] = row
            continue
        newest = row if (row.updated_at or floor) >= (previous.updated_at or floor) else previous
        other = previous if newest is row else row
        merged[symbol] = ScannerRow(
            symbol=symbol, company=newest.company or other.company, price=newest.price,
            change=newest.change if newest.change is not None else other.change,
            change_percent=newest.change_percent if newest.change_percent is not None else other.change_percent,
            volume=newest.volume if newest.volume is not None else other.volume,
            relative_volume=newest.relative_volume if newest.relative_volume is not None else other.relative_volume,
            day_high=newest.day_high if newest.day_high is not None else other.day_high,
            day_low=newest.day_low if newest.day_low is not None else other.day_low,
            vwap=newest.vwap if newest.vwap is not None else other.vwap,
            breakout_state=newest.breakout_state or other.breakout_state,
            catalyst=newest.catalyst or other.catalyst, halt_state=newest.halt_state or other.halt_state,
            freshness=newest.freshness, sources=tuple(dict.fromkeys((*previous.sources, *row.sources))),
            updated_at=newest.updated_at,
        )
    return sorted(merged.values(), key=lambda item: (-(item.change_percent or Decimal("-999999")), item.symbol))


def filter_scanner_rows(rows: list[ScannerRow], filter_name: str, watchlist: set[str] | None = None) -> list[ScannerRow]:
    name = str(filter_name or "All Live")
    watched = {value.strip().upper() for value in (watchlist or set())}
    if name == "Top Gainers":
        return [row for row in rows if row.change_percent is not None and row.change_percent > 0]
    if name == "Relative Volume":
        return [row for row in rows if row.relative_volume is not None and row.relative_volume >= Decimal("2")]
    if name == "Breakout":
        return [row for row in rows if bool(row.breakout_state)]
    if name == "Opening Range":
        return [row for row in rows if row.breakout_state and "opening" in row.breakout_state.lower()]
    if name == "VWAP Cross":
        return [row for row in rows if row.breakout_state and "vwap" in row.breakout_state.lower()]
    if name == "News Catalyst":
        return [row for row in rows if row.catalyst]
    if name == "Watchlist Only":
        return [row for row in rows if row.symbol.upper() in watched]
    return list(rows)


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
