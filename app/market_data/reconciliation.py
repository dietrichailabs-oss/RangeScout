"""Compatibility-aware discrepancy checks without inventing consensus prices."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.market_data.contracts import FabricResult


def quote_price(result: FabricResult) -> Decimal | None:
    if not isinstance(result.payload, dict):
        return None
    raw = result.payload.get("price", result.payload.get("last"))
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def discrepancy_warning(primary: FabricResult, comparison: FabricResult, tolerance_percent: Decimal = Decimal("1.0")) -> str | None:
    if primary.currency != comparison.currency or primary.delay_class != comparison.delay_class:
        return "Cross-provider observations are not directly comparable (currency or delay class differs)."
    if primary.venue and comparison.venue and primary.venue != comparison.venue:
        return "Cross-provider observations refer to different venues."
    left = quote_price(primary)
    right = quote_price(comparison)
    if left is None or right is None or left == 0:
        return None
    delta = abs(left - right) / abs(left) * Decimal("100")
    if delta > tolerance_percent:
        return f"Provider observations disagree by {delta.quantize(Decimal('0.01'))}%; fastest value was not averaged."
    return None
