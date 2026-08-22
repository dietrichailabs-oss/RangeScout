"""Analyst data policy: never fabricate consensus or targets."""

from __future__ import annotations

from app.research.models import ResearchValue


def unavailable_analyst_outlook() -> dict[str, ResearchValue]:
    return {"Coverage": ResearchValue.unavailable("No licensed analyst source", "Analyst consensus and targets are unavailable.")}
