from __future__ import annotations

from app.research.models import ResearchValue


def build_earnings(values: dict[str, ResearchValue]) -> dict[str, ResearchValue]:
    return {key: values[key] for key in ("Revenue", "Operating income", "Net income", "Diluted EPS")}
