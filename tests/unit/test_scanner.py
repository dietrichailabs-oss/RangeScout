from __future__ import annotations

from decimal import Decimal

from app.scanner.engine import ScannerObservation, permitted_scan_universe, scan_observations


def test_scanner_rules_and_scope_are_local_and_deterministic() -> None:
    observation = ScannerObservation("AAPL", Decimal("12"), vwap=Decimal("10"), previous_price=Decimal("9"), opening_range_high=Decimal("11"), opening_range_low=Decimal("8"), day_high=Decimal("12"), day_low=Decimal("8"), rvol=Decimal("3"), gap_percent=Decimal("4"), volatility_ratio=Decimal("2.5"), news_catalyst=True, sec_catalyst=True, government_catalyst=True, halt_status="RESUMED")
    hits = scan_observations([observation, ScannerObservation("OUTSIDE", Decimal("1"), rvol=Decimal("9"))], {"AAPL"})
    rules = {hit.rule for hit in hits}
    assert rules == {"unusual_volume", "gap", "vwap_cross", "opening_range_break", "new_day_high", "volatility_spike", "news_catalyst", "sec_catalyst", "government_catalyst", "halt_resumption"}
    assert all(hit.symbol == "AAPL" for hit in hits)


def test_permitted_universe_is_active_subscribed_and_watchlists_only() -> None:
    assert permitted_scan_universe("aapl", ("MSFT",), ["NVDA", "AAPL"]) == {"AAPL", "MSFT", "NVDA"}
