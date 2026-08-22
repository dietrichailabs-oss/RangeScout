"""Official Alpha Vantage EARNINGS_ESTIMATES adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import urlencode

from app.market_data.contracts import FabricProviderError, RateLimited
from app.market_data.providers.http import JsonTransport
from app.research.analyst.models import AnalystProviderError, AnalystState


ALPHA_VANTAGE_QUERY_URL = "https://www.alphavantage.co/query"


class AlphaVantageEarningsEstimatesClient:
    provider_id = "alpha_vantage"
    dataset = "earnings_estimates"

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or JsonTransport()

    def fetch(self, symbol: str, api_key: str) -> tuple[dict[str, Any], str | None]:
        try:
            payload = self.transport.get_json(
                ALPHA_VANTAGE_QUERY_URL + "?" + urlencode(
                    {"function": "EARNINGS_ESTIMATES", "symbol": symbol, "apikey": api_key}
                )
            )
        except RateLimited:
            raise AnalystProviderError(AnalystState.RATE_LIMITED, "Alpha Vantage earnings estimates are rate limited.") from None
        except FabricProviderError as exc:
            state = AnalystState.UNAUTHORIZED if "401" in str(exc) else AnalystState.UNAVAILABLE
            raise AnalystProviderError(state, "Alpha Vantage earnings estimates are unavailable.") from None
        if not isinstance(payload, dict):
            raise AnalystProviderError(AnalystState.UNAVAILABLE, "Alpha Vantage returned malformed earnings estimates.")
        notice = str(payload.get("Note") or payload.get("Information") or "").lower()
        error = str(payload.get("Error Message") or "").lower()
        if notice:
            state = AnalystState.RATE_LIMITED if any(word in notice for word in ("rate", "frequency", "limit", "calls")) else AnalystState.ENTITLEMENT_UNAVAILABLE
            raise AnalystProviderError(state, "Alpha Vantage earnings estimates are unavailable for the configured quota or plan.")
        if error:
            state = AnalystState.UNAUTHORIZED if "key" in error else AnalystState.UNAVAILABLE
            raise AnalystProviderError(state, "Alpha Vantage rejected the earnings-estimates request.")
        annual = _rows(payload, "annualEarningsEstimates", "annual_earnings_estimates")
        quarterly = _rows(payload, "quarterlyEarningsEstimates", "quarterly_earnings_estimates")
        values: dict[str, Any] = {}
        provider_period: str | None = None
        for label, row in (("Current-year", _select(annual, "current", 0)), ("Next-year", _select(annual, "next", 1))):
            if row is None:
                continue
            provider_period = provider_period or _text(row, "fiscalDateEnding", "fiscal_date_ending", "date")
            _copy_estimate(values, label, row, include_revisions=label == "Current-year")
        for label, row in (("Current-quarter", _select(quarterly, "current", 0)), ("Next-quarter", _select(quarterly, "next", 1))):
            if row is None:
                continue
            provider_period = provider_period or _text(row, "fiscalDateEnding", "fiscal_date_ending", "date")
            _copy_estimate(values, label, row, include_revisions=label == "Current-quarter", include_revenue=False)
        if not values:
            raise AnalystProviderError(AnalystState.UNAVAILABLE, "Alpha Vantage returned no supported estimate fields.")
        values["Retrieved At"] = datetime.now(timezone.utc).isoformat()
        return values, provider_period


def _rows(payload: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _select(rows: list[dict[str, Any]], horizon: str, fallback_index: int) -> dict[str, Any] | None:
    for row in rows:
        text = _text(row, "horizon", "period", "estimatePeriod", "estimate_period").lower()
        if horizon in text:
            return row
    return rows[fallback_index] if len(rows) > fallback_index else None


def _copy_estimate(values: dict[str, Any], label: str, row: dict[str, Any], *, include_revisions: bool, include_revenue: bool = True) -> None:
    mappings = [
        (f"{label} EPS Estimate", ("epsEstimateAverage", "eps_estimate_average", "averageEstimate", "average_estimate")),
        (f"{label} Analyst Count", ("epsEstimateAnalystCount", "eps_estimate_analyst_count", "numberOfAnalysts", "number_of_analysts")),
    ]
    if include_revenue:
        mappings.append((f"{label} Revenue Estimate", ("revenueEstimateAverage", "revenue_estimate_average")))
    if include_revisions:
        mappings.extend(
            [
                (f"{label} Revisions Up (30d)", ("epsEstimateRevisionUpTrailing30Days", "eps_estimate_revision_up_trailing_30_days")),
                (f"{label} Revisions Down (30d)", ("epsEstimateRevisionDownTrailing30Days", "eps_estimate_revision_down_trailing_30_days")),
            ]
        )
    for output, aliases in mappings:
        value = _number(row, *aliases)
        if value is not None:
            values[output] = str(value)
    period = _text(row, "fiscalDateEnding", "fiscal_date_ending", "date")
    if period:
        values[f"{label} Period"] = period


def _number(row: dict[str, Any], *names: str) -> Decimal | None:
    value = _value(row, *names)
    if value in (None, "", "None", "null", "N/A", "-"):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _text(row: dict[str, Any], *names: str) -> str:
    value = _value(row, *names)
    return str(value).strip() if value is not None else ""


def _value(row: dict[str, Any], *names: str) -> Any:
    normalized = {_normalize(key): value for key, value in row.items()}
    for name in names:
        if _normalize(name) in normalized:
            return normalized[_normalize(name)]
    return None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())
