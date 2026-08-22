from __future__ import annotations

from app.research.models import ResearchValue


def build_financials(values: dict[str, ResearchValue]) -> dict[str, ResearchValue]:
    return {key: values[key] for key in (
        "Revenue", "Cost of revenue", "Gross profit", "Operating income", "Net income", "Diluted EPS",
        "Cash", "Assets", "Current assets", "Liabilities", "Current liabilities", "Equity", "Debt",
        "Operating cash flow", "Capital expenditures",
    )}
