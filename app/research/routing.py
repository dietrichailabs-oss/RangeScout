"""Capability-led Research routing for canonical instrument types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.application.active_symbol import SymbolRequest
from app.research.models import Availability, CompanyProfile, ResearchSnapshot, ResearchValue
from app.research.fund import FundResearchService


class ResearchRoute(str, Enum):
    CORPORATE = "corporate"
    FUND = "fund"
    MARKET_INSTRUMENT = "market_instrument"


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    route: ResearchRoute
    sec_applicable: bool
    analyst_applicable: bool
    visible_sections: tuple[str, ...]
    state: Availability
    message: str


_CORPORATE_ASSETS = frozenset({"equity", "stock", "preferred", "adr", "otc"})
_FUND_TYPES = frozenset({"closed_end_fund", "etf", "mutual_fund"})


def plan_research(asset_class: str, subtype: str = "") -> ResearchPlan:
    asset = str(asset_class or "unknown").lower()
    kind = str(subtype or "").lower().replace(" ", "_")
    if kind in _FUND_TYPES or asset in {"etf", "mutual_fund", "closed_end_fund"}:
        return ResearchPlan(
            ResearchRoute.FUND, True, False,
            ("Overview", "Financials", "Performance", "Catalysts & News"),
            Availability.NOT_APPLICABLE,
            "Corporate analyst and operating-company tables do not apply to this fund structure.",
        )
    if asset in _CORPORATE_ASSETS or asset == "unknown":
        return ResearchPlan(
            ResearchRoute.CORPORATE, True, True,
            ("Overview", "Valuation", "Earnings", "Growth", "Financials", "Financial Health",
             "Performance", "Peers", "Analyst Outlook", "Catalysts & News"),
            Availability.AVAILABLE, "Corporate Research routed to eligible configured sources.",
        )
    return ResearchPlan(
        ResearchRoute.MARKET_INSTRUMENT, False, False,
        ("Overview", "Performance", "Catalysts & News"), Availability.NOT_APPLICABLE,
        "SEC corporate fundamentals and analyst estimates do not apply to this instrument type.",
    )


def route_snapshot(service, request: SymbolRequest, period_mode: str) -> ResearchSnapshot:
    plan = plan_research(request.asset_class, request.subtype)
    if not plan.sec_applicable:
        return unavailable_snapshot(request, plan)
    if plan.route is ResearchRoute.FUND:
        client = getattr(service, "client", None)
        if client is None:
            missing = ResearchPlan(
                ResearchRoute.FUND, False, False, plan.visible_sections,
                Availability.PROVIDER_NOT_SUPPORTED,
                "Fund Research requires the approved SEC submissions client; corporate companyfacts was not used.",
            )
            return unavailable_snapshot(request, missing)
        return FundResearchService(client).load(request.symbol, request.generation, period_mode)
    return service.load(request.symbol, request.generation, period_mode)


def unavailable_snapshot(request: SymbolRequest, plan: ResearchPlan) -> ResearchSnapshot:
    profile = CompanyProfile(request.symbol, None, request.symbol, request.venue or None, None, None,
                             source="RangeScout canonical instrument catalog")
    state = ResearchValue(
        None, "RangeScout capability router", availability=plan.state, selection_reason=plan.message,
    )
    sections = {name: {"Data state": state} for name in plan.visible_sections}
    return ResearchSnapshot(request.symbol, request.generation, profile, sections,
                            datetime.now(timezone.utc), (plan.message,))
