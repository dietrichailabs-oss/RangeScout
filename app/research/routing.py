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


def plan_research(
    asset_class: str, subtype: str = "", issuer_type: str = "", security_role: str = "",
) -> ResearchPlan:
    asset = str(asset_class or "unknown").lower()
    kind = str(subtype or "").lower().replace(" ", "_")
    issuer = str(issuer_type or "unknown").lower().replace(" ", "_")
    role = str(security_role or "unknown").lower().replace(" ", "_")
    if asset == "unit" and role == "primary_common" and issuer in {"operating_partnership", "operating_company"}:
        return ResearchPlan(
            ResearchRoute.CORPORATE, True, True,
            ("Overview", "Valuation", "Earnings", "Growth", "Financials", "Financial Health",
             "Performance", "Peers", "Analyst Outlook", "Catalysts & News"),
            Availability.AVAILABLE,
            "Issuer and SEC Research applies to this primary operating-partnership common unit.",
        )
    if asset == "unit" and role == "preferred_security" and issuer in {"operating_partnership", "operating_company"}:
        return ResearchPlan(
            ResearchRoute.CORPORATE, True, False,
            ("Overview", "Financials", "Financial Health", "Performance", "Catalysts & News"),
            Availability.AVAILABLE,
            "Issuer and SEC Research is shown in operating-partnership context for this preferred unit.",
        )
    if asset == "unit" and issuer in {"fund_vehicle", "trust_vehicle"}:
        return ResearchPlan(
            ResearchRoute.FUND, True, False,
            ("Overview", "Financials", "Performance", "Catalysts & News"),
            Availability.NOT_APPLICABLE,
            "Research follows the SEC-reporting trust or fund issuer context for this unit.",
        )
    if asset == "unit" and role != "primary_common":
        return ResearchPlan(
            ResearchRoute.MARKET_INSTRUMENT, False, False,
            ("Overview", "Performance", "Catalysts & News"), Availability.NOT_APPLICABLE,
            "This packaged or alternate unit retains market-instrument Research context.",
        )
    if issuer == "closed_end_fund":
        context = (
            "This preferred security retains its own Quote/History identity while Research uses its "
            "closed-end-fund issuer context. Ordinary-company earnings and analyst tables do not apply."
            if role == "preferred_security" or asset == "preferred" else
            "Corporate analyst and operating-company tables do not apply to this closed-end fund."
        )
        return ResearchPlan(
            ResearchRoute.FUND, True, False,
            ("Overview", "Financials", "Performance", "Catalysts & News"),
            Availability.NOT_APPLICABLE, context,
        )
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
    plan = plan_research(request.asset_class, request.subtype, request.issuer_type, request.security_role)
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
    return service.load(request.symbol, request.generation, period_mode, cik=request.cik or None)


def unavailable_snapshot(request: SymbolRequest, plan: ResearchPlan) -> ResearchSnapshot:
    profile = CompanyProfile(request.symbol, None, request.symbol, request.venue or None, None, None,
                             source="RangeScout canonical instrument catalog")
    state = ResearchValue(
        None, "RangeScout capability router", availability=plan.state, selection_reason=plan.message,
    )
    sections = {name: {"Data state": state} for name in plan.visible_sections}
    return ResearchSnapshot(request.symbol, request.generation, profile, sections,
                            datetime.now(timezone.utc), (plan.message,))
