from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.research.models import Availability, ResearchValue


def _ratio(numerator: ResearchValue, denominator: ResearchValue, label: str) -> ResearchValue:
    if (
        not isinstance(numerator.value, Decimal) or not isinstance(denominator.value, Decimal) or denominator.value == 0
        or not numerator.units or numerator.units != denominator.units
    ):
        return ResearchValue.unavailable("Calculated from SEC companyfacts", f"Insufficient same-unit compatible facts for {label}.")
    filing_dates = [value for value in (numerator.filing_date, denominator.filing_date) if value is not None]
    return ResearchValue(
        numerator.value / denominator.value,
        "Calculated from SEC companyfacts",
        period=numerator.period,
        units="ratio",
        filing_date=max(filing_dates) if filing_dates else None,
        calculated_at=datetime.now(timezone.utc),
        availability=Availability.AVAILABLE,
        selection_reason=f"{label} uses selected SEC facts with compatible {numerator.units} units.",
    )


def _difference(left: ResearchValue, right: ResearchValue, label: str) -> ResearchValue:
    if (
        not isinstance(left.value, Decimal) or not isinstance(right.value, Decimal)
        or not left.units or left.units != right.units
    ):
        return ResearchValue.unavailable("Calculated from SEC companyfacts", f"Insufficient same-unit compatible facts for {label}.")
    return ResearchValue(
        left.value - right.value,
        "Calculated from SEC companyfacts",
        period=left.period,
        units=left.units,
        filing_date=left.filing_date,
        calculated_at=datetime.now(timezone.utc),
        availability=Availability.AVAILABLE,
        selection_reason=f"{label} subtracts deterministically selected compatible SEC facts.",
    )


def build_financial_health(values: dict[str, ResearchValue]) -> dict[str, ResearchValue]:
    quick_assets = _difference(values["Current assets"], values["Inventory"], "quick assets")
    return {
        "Cash": values["Cash"],
        "Debt": values["Debt"],
        "Net debt": _difference(values["Debt"], values["Cash"], "net debt"),
        "Current ratio": _ratio(values["Current assets"], values["Current liabilities"], "current ratio"),
        "Quick ratio": _ratio(quick_assets, values["Current liabilities"], "quick ratio"),
        "Debt / equity": _ratio(values["Debt"], values["Equity"], "debt-to-equity"),
        "Liabilities / assets": _ratio(values["Liabilities"], values["Assets"], "liabilities-to-assets"),
        "Operating cash flow": values["Operating cash flow"],
        "Free cash flow": _difference(values["Operating cash flow"], values["Capital expenditures"], "free cash flow"),
        "Gross margin": _ratio(values["Gross profit"], values["Revenue"], "gross margin"),
        "Operating margin": _ratio(values["Operating income"], values["Revenue"], "operating margin"),
        "Net margin": _ratio(values["Net income"], values["Revenue"], "net margin"),
        "Interest coverage": _ratio(values["Operating income"], values["Interest expense"], "interest coverage"),
    }
