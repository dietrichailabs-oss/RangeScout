from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.research.models import Availability, ResearchValue


def _growth(current: ResearchValue, previous: ResearchValue, label: str) -> ResearchValue:
    if not isinstance(current.value, Decimal) or not isinstance(previous.value, Decimal) or previous.value == 0:
        return ResearchValue.unavailable("Calculated from SEC companyfacts", f"Two compatible nonzero periods are required for {label}.")
    return ResearchValue(
        (current.value - previous.value) / abs(previous.value) * Decimal(100),
        "Calculated from SEC companyfacts",
        period=f"{previous.period or 'prior'} to {current.period or 'current'}",
        units="percent",
        filing_date=current.filing_date,
        calculated_at=datetime.now(timezone.utc),
        availability=Availability.AVAILABLE,
        selection_reason=f"{label} uses the latest two deterministically selected compatible SEC filing periods.",
    )


def build_growth(values: dict[str, ResearchValue]) -> dict[str, ResearchValue]:
    return {
        "Revenue growth": _growth(values["Revenue"], values["Revenue previous"], "revenue growth"),
        "Earnings growth": _growth(values["Net income"], values["Net income previous"], "earnings growth"),
    }
