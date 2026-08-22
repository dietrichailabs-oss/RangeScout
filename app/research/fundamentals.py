"""Official SEC company mapping, company-facts access, and fact selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import json
from pathlib import Path
from threading import Lock
import time
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

from app.research.caching import ResearchCache
from app.research.models import Availability, CompanyProfile, ResearchSnapshot, ResearchValue


SEC_USER_AGENT = "RangeScout/1.3 Dietrich AI Labs dietrichailabs@gmail.com"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.125  # 8 requests/sec, below SEC's published 10/sec ceiling.


@dataclass(frozen=True, slots=True)
class SecFactCandidate:
    concept: str
    unit: str
    value: Decimal
    start: date | None
    end: date
    filed: date
    form: str
    accession: str
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None


class SecFactSelector:
    """Select one auditable fact using stable, documented tie-breakers."""

    ELIGIBLE_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})

    def select(
        self,
        facts: dict[str, Any],
        concepts: Iterable[str],
        units: Iterable[str],
        *,
        period: str = "latest",
        forms: frozenset[str] | None = None,
    ) -> ResearchValue:
        concept_order = {concept: index for index, concept in enumerate(concepts)}
        unit_order = {unit: index for index, unit in enumerate(units)}
        candidates: list[SecFactCandidate] = []
        for concept, concept_priority in concept_order.items():
            node = facts.get(concept)
            if not isinstance(node, dict):
                continue
            unit_payloads = node.get("units", {})
            if not isinstance(unit_payloads, dict):
                continue
            for unit, unit_priority in unit_order.items():
                rows = unit_payloads.get(unit, [])
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    candidate = self._candidate(concept, unit, row)
                    if candidate is not None and (forms is None or candidate.form in forms):
                        candidates.append(candidate)
        if not candidates:
            return ResearchValue.unavailable("SEC companyfacts", "No eligible standard-taxonomy fact with an accepted unit.")

        def rank(item: SecFactCandidate) -> tuple[date, date, int, int, int, str]:
            amended = 1 if item.form.endswith("/A") else 0
            return (
                item.end,
                item.filed,
                amended,
                -concept_order[item.concept],
                -unit_order[item.unit],
                item.accession,
            )

        winner = max(candidates, key=rank)
        reason = (
            f"latest period end {winner.end.isoformat()}; latest filing {winner.filed.isoformat()}; "
            f"amendment/restatement precedence applied; concept priority {concept_order[winner.concept] + 1}; "
            f"unit priority {unit_order[winner.unit] + 1}; accession {winner.accession}"
        )
        return ResearchValue(
            winner.value,
            "SEC companyfacts",
            period=period if period != "latest" else winner.end.isoformat(),
            units=winner.unit,
            filing_date=winner.filed,
            availability=Availability.AVAILABLE,
            selection_reason=reason,
        )

    def _candidate(self, concept: str, unit: str, row: object) -> SecFactCandidate | None:
        if not isinstance(row, dict) or row.get("form") not in self.ELIGIBLE_FORMS:
            return None
        try:
            value = Decimal(str(row["val"]))
            end = date.fromisoformat(str(row["end"]))
            filed = date.fromisoformat(str(row["filed"]))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            return None
        start_text = row.get("start")
        try:
            start = date.fromisoformat(str(start_text)) if start_text else None
        except ValueError:
            start = None
        fy = row.get("fy")
        return SecFactCandidate(
            concept=concept,
            unit=unit,
            value=value,
            start=start,
            end=end,
            filed=filed,
            form=str(row["form"]),
            accession=str(row.get("accn", "")),
            fiscal_year=int(fy) if isinstance(fy, int) else None,
            fiscal_period=str(row.get("fp")) if row.get("fp") else None,
            frame=str(row.get("frame")) if row.get("frame") else None,
        )


class SecCompanyFactsClient:
    def __init__(
        self,
        cache: ResearchCache,
        *,
        user_agent: str = SEC_USER_AGENT,
        opener: Callable[..., Any] = urlopen,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 15.0,
    ) -> None:
        if "@" not in user_agent or "RangeScout/" not in user_agent:
            raise ValueError("SEC User-Agent must identify RangeScout and a contact email.")
        self.cache = cache
        self.user_agent = user_agent
        self.opener = opener
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.timeout_seconds = timeout_seconds
        self._last_request_at = 0.0
        self._rate_lock = Lock()

    def _get_json(self, url: str) -> dict[str, Any]:
        cached = self.cache.get(url)
        if isinstance(cached, dict):
            return cached
        with self._rate_lock:
            elapsed = self.monotonic() - self._last_request_at
            if elapsed < SEC_MIN_REQUEST_INTERVAL_SECONDS:
                self.sleeper(SEC_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
            request = Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"})
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                headers = getattr(response, "headers", None)
                encoding = headers.get("Content-Encoding", "") if headers is not None else ""
                if str(encoding).lower() == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8"))
            self._last_request_at = self.monotonic()
        if not isinstance(payload, dict):
            raise ValueError("SEC returned a non-object JSON payload.")
        self.cache.put(url, payload)
        return payload

    def company_map(self) -> dict[str, dict[str, str]]:
        payload = self._get_json(SEC_COMPANY_TICKERS_URL)
        result: dict[str, dict[str, str]] = {}
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("ticker", "")).strip().upper()
            cik = row.get("cik_str")
            if symbol and isinstance(cik, int):
                result[symbol] = {"cik": f"{cik:010d}", "name": str(row.get("title", "")).strip()}
        return result

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self._get_json(SEC_COMPANY_FACTS_URL.format(cik=str(cik).zfill(10)))

    def submissions(self, cik: str) -> dict[str, Any]:
        return self._get_json(SEC_SUBMISSIONS_URL.format(cik=str(cik).zfill(10)))


class ResearchService:
    """Build a traceable research snapshot without inventing unavailable data."""

    FACTS = {
        "Revenue": (("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"), ("USD",)),
        "Net income": (("NetIncomeLoss", "ProfitLoss"), ("USD",)),
        "Operating income": (("OperatingIncomeLoss",), ("USD",)),
        "Cost of revenue": (("CostOfRevenue", "CostOfGoodsAndServicesSold"), ("USD",)),
        "Gross profit": (("GrossProfit",), ("USD",)),
        "Assets": (("Assets",), ("USD",)),
        "Liabilities": (("Liabilities",), ("USD",)),
        "Equity": (("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), ("USD",)),
        "Cash": (("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), ("USD",)),
        "Debt": (("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent", "LongTermDebt"), ("USD",)),
        "Current assets": (("AssetsCurrent",), ("USD",)),
        "Current liabilities": (("LiabilitiesCurrent",), ("USD",)),
        "Inventory": (("InventoryNet",), ("USD",)),
        "Operating cash flow": (("NetCashProvidedByUsedInOperatingActivities",), ("USD",)),
        "Capital expenditures": (("PaymentsToAcquirePropertyPlantAndEquipment",), ("USD",)),
        "Interest expense": (("InterestExpenseNonOperating", "InterestExpense"), ("USD",)),
        "Diluted EPS": (("EarningsPerShareDiluted",), ("USD/shares",)),
        "Shares outstanding": (("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"), ("shares",)),
    }

    def __init__(self, client: SecCompanyFactsClient) -> None:
        self.client = client
        self.selector = SecFactSelector()

    def load(self, symbol: str, generation: int = 0, period_mode: str = "annual") -> ResearchSnapshot:
        normalized = symbol.strip().upper()
        company = self.client.company_map().get(normalized)
        now = datetime.now(timezone.utc)
        if company is None:
            profile = CompanyProfile(normalized, None, None, None, None, None)
            return ResearchSnapshot(normalized, generation, profile, {"Overview": {}}, now, ("No official SEC ticker-to-CIK match was found.",))
        cik = company["cik"]
        facts_payload = self.client.companyfacts(cik)
        submissions = self.client.submissions(cik)
        us_gaap = facts_payload.get("facts", {}).get("us-gaap", {})
        profile = CompanyProfile(
            normalized,
            cik,
            str(facts_payload.get("entityName") or company.get("name") or "") or None,
            _first_string(submissions.get("exchanges")),
            str(submissions.get("sic") or "") or None,
            str(submissions.get("sicDescription") or "") or None,
        )
        forms = frozenset({"10-Q", "10-Q/A"}) if period_mode == "quarterly" else frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
        selected = {name: self.selector.select(us_gaap, concepts, units, forms=forms) for name, (concepts, units) in self.FACTS.items()}
        selected["Revenue previous"] = self._previous(us_gaap, "Revenue", selected["Revenue"], forms)
        selected["Net income previous"] = self._previous(us_gaap, "Net income", selected["Net income"], forms)
        sections = _build_sections(selected)
        return ResearchSnapshot(normalized, generation, profile, sections, now)

    def _previous(self, facts: dict[str, Any], fact_name: str, latest: ResearchValue, forms: frozenset[str]) -> ResearchValue:
        if latest.availability is not Availability.AVAILABLE or not latest.period:
            return ResearchValue.unavailable("SEC companyfacts", "A latest comparable period was not selected.")
        try:
            latest_end = date.fromisoformat(latest.period)
        except ValueError:
            return ResearchValue.unavailable("SEC companyfacts", "The selected filing period could not be compared.")
        concepts, units = self.FACTS[fact_name]
        filtered: dict[str, Any] = {}
        for concept in concepts:
            node = facts.get(concept)
            if not isinstance(node, dict):
                continue
            selected_units: dict[str, list[object]] = {}
            for unit in units:
                rows = node.get("units", {}).get(unit, []) if isinstance(node.get("units"), dict) else []
                selected_units[unit] = [
                    row for row in rows
                    if isinstance(row, dict) and isinstance(row.get("end"), str) and row["end"] < latest_end.isoformat()
                ]
            filtered[concept] = {"units": selected_units}
        return self.selector.select(filtered, concepts, units, period="previous comparable filing period", forms=forms)


def _first_string(value: object) -> str | None:
    if isinstance(value, list):
        return next((str(item) for item in value if str(item).strip()), None)
    return None


def _build_sections(values: dict[str, ResearchValue]) -> dict[str, dict[str, ResearchValue]]:
    from app.research.earnings import build_earnings
    from app.research.financial_health import build_financial_health
    from app.research.financials import build_financials
    from app.research.growth import build_growth
    from app.research.performance import build_performance
    from app.research.valuation import build_valuation

    return {
        "Overview": {key: values[key] for key in ("Revenue", "Net income", "Assets", "Equity", "Shares outstanding")},
        "Valuation": build_valuation(values),
        "Earnings": build_earnings(values),
        "Growth": build_growth(values),
        "Financials": build_financials(values),
        "Financial Health": build_financial_health(values),
        "Performance": build_performance(values),
        "Peers": {},
        "Analyst Outlook": {"Coverage": ResearchValue.unavailable("No licensed analyst source", "Analyst estimates are not available from the configured public sources.")},
        "Catalysts & News": {},
    }
