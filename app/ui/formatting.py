"""Semantic display formatting that preserves raw model values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class FormattedValue:
    raw: Any
    text: str
    alignment: str = "right"


def format_financial_value(value: Any, semantic: str = "number", currency: str = "USD") -> FormattedValue:
    if value is None or value == "":
        return FormattedValue(value, "N/A", "left")
    kind = str(semantic or "number").lower()
    if kind in {"date", "year", "id", "accession", "text"}:
        if isinstance(value, (date, datetime)):
            return FormattedValue(value, value.isoformat(), "left")
        return FormattedValue(value, str(value), "left")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return FormattedValue(value, str(value), "left")
    if kind in {"percent", "percentage"}:
        return FormattedValue(value, f"{number:,.2f}%")
    if kind in {"ratio", "multiple"}:
        return FormattedValue(value, f"{number:,.2f}×")
    if kind in {"money", "currency", "eps"}:
        symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "
        decimals = 2 if kind == "eps" or abs(number) < Decimal("1000") else 0
        return FormattedValue(value, f"{symbol}{number:,.{decimals}f}")
    decimals = 2 if number != number.to_integral_value() else 0
    return FormattedValue(value, f"{number:,.{decimals}f}")
