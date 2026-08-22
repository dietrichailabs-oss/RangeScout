from __future__ import annotations

from app.research.models import ResearchValue


def build_valuation(values: dict[str, ResearchValue]) -> dict[str, ResearchValue]:
    return {
        "Diluted EPS": values["Diluted EPS"],
        "Shares outstanding": values["Shares outstanding"],
        "P/E": ResearchValue.unavailable("Calculated", "A market price and compatible earnings period are required."),
        "Enterprise value": ResearchValue.unavailable("Calculated", "Market capitalization and complete debt/cash facts are required."),
    }
