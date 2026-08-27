"""Official SEC company mapping, company-facts access, and fact selection."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import json
import re
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
class ReportingRegime:
    taxonomy: str
    currency: str
    period: date
    filed: date
    form: str
    accession: str


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
        source: str = "SEC companyfacts",
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
            return ResearchValue.unavailable(source, "No eligible standard-taxonomy fact with an accepted unit.")

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
            source,
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
    """Build a traceable, taxonomy-consistent snapshot without inventing unavailable data."""

    US_GAAP_FACTS = {
        "Revenue": (("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"), "monetary"),
        "Net income": (("NetIncomeLoss", "ProfitLoss"), "monetary"),
        "Operating income": (("OperatingIncomeLoss",), "monetary"),
        "Cost of revenue": (("CostOfRevenue", "CostOfGoodsAndServicesSold"), "monetary"),
        "Gross profit": (("GrossProfit",), "monetary"),
        "Assets": (("Assets",), "monetary"),
        "Liabilities": (("Liabilities",), "monetary"),
        "Equity": (("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), "monetary"),
        "Cash": (("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"), "monetary"),
        "Debt": (("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent", "LongTermDebt"), "monetary"),
        "Current assets": (("AssetsCurrent",), "monetary"),
        "Current liabilities": (("LiabilitiesCurrent",), "monetary"),
        "Inventory": (("InventoryNet",), "monetary"),
        "Operating cash flow": (("NetCashProvidedByUsedInOperatingActivities",), "monetary"),
        "Capital expenditures": (("PaymentsToAcquirePropertyPlantAndEquipment",), "monetary"),
        "Interest expense": (("InterestExpenseNonOperating", "InterestExpense"), "monetary"),
        "Diluted EPS": (("EarningsPerShareDiluted",), "per_share"),
        "Shares outstanding": (("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"), "shares"),
    }
    IFRS_FACTS = {
        "Revenue": (("Revenue",), "monetary"),
        "Net income": (("ProfitLoss", "ProfitLossAttributableToOwnersOfParent"), "monetary"),
        "Operating income": (("OperatingProfitLoss",), "monetary"),
        "Cost of revenue": (("CostOfSales",), "monetary"),
        "Gross profit": (("GrossProfit",), "monetary"),
        "Assets": (("Assets",), "monetary"),
        "Liabilities": (("Liabilities",), "monetary"),
        "Equity": (("Equity", "EquityAttributableToOwnersOfParent"), "monetary"),
        "Cash": (("CashAndCashEquivalents",), "monetary"),
        "Debt": (("BorrowingsNoncurrent", "LongtermDebt"), "monetary"),
        "Current assets": (("CurrentAssets",), "monetary"),
        "Current liabilities": (("CurrentLiabilities",), "monetary"),
        "Inventory": (("Inventories",), "monetary"),
        "Operating cash flow": (("CashFlowsFromUsedInOperatingActivities",), "monetary"),
        "Capital expenditures": (("PurchaseOfPropertyPlantAndEquipment", "PaymentsToAcquirePropertyPlantAndEquipment"), "monetary"),
        "Interest expense": (("FinanceCosts",), "monetary"),
        "Diluted EPS": (("DilutedEarningsLossPerShare", "DilutedEarningsPerShare"), "per_share"),
        "Shares outstanding": (("NumberOfSharesOutstanding",), "shares"),
    }
    TAXONOMIES = {"us-gaap": US_GAAP_FACTS, "ifrs-full": IFRS_FACTS}
    FACTS = US_GAAP_FACTS
    ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
    QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
    NATIVE_FORMS = {
        "us-gaap": frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"}),
        "ifrs-full": frozenset({"20-F", "20-F/A", "40-F", "40-F/A"}),
    }

    def __init__(self, client: SecCompanyFactsClient) -> None:
        self.client = client
        self.selector = SecFactSelector()

    def load(
        self, symbol: str, generation: int = 0, period_mode: str = "annual", *, cik: str | None = None,
    ) -> ResearchSnapshot:
        normalized = symbol.strip().upper()
        company_map = self.client.company_map()
        company = company_map.get(normalized)
        if company is None and cik:
            company = {"cik": str(cik).zfill(10), "name": normalized}
        if company is None:
            for candidate in self._issuer_symbol_candidates(normalized):
                company = company_map.get(candidate)
                if company is not None:
                    break
        now = datetime.now(timezone.utc)
        if company is None:
            profile = CompanyProfile(normalized, None, None, None, None, None)
            return ResearchSnapshot(normalized, generation, profile, {"Overview": {}}, now, ("No official SEC ticker-to-CIK match was found.",))
        cik_value = str(cik or company["cik"]).zfill(10)
        facts_payload = self.client.companyfacts(cik_value)
        submissions = self.client.submissions(cik_value)
        facts_root = facts_payload.get("facts", {})
        if not isinstance(facts_root, dict):
            facts_root = {}
        forms = self.QUARTERLY_FORMS if period_mode == "quarterly" else self.ANNUAL_FORMS
        regime = self._select_reporting_regime(facts_root, forms)
        taxonomy = regime.taxonomy if regime is not None else "us-gaap"
        mapping = self.TAXONOMIES[taxonomy]
        taxonomy_facts = facts_root.get(taxonomy, {})
        if not isinstance(taxonomy_facts, dict):
            taxonomy_facts = {}
        currency = regime.currency if regime is not None else None
        current_facts = self._facts_for_regime(taxonomy_facts, mapping, regime)
        source = "SEC companyfacts" if taxonomy == "us-gaap" else "SEC companyfacts (ifrs-full)"
        profile = CompanyProfile(
            normalized,
            cik_value,
            str(facts_payload.get("entityName") or company.get("name") or "") or None,
            _first_string(submissions.get("exchanges")),
            str(submissions.get("sic") or "") or None,
            str(submissions.get("sicDescription") or "") or None,
        )
        selected: dict[str, ResearchValue] = {}
        for name, (concepts, unit_kind) in mapping.items():
            units = self._units_for(unit_kind, currency)
            selected[name] = self.selector.select(
                current_facts, concepts, units, forms=forms, source=source,
            )
        selected["Revenue previous"] = self._previous(
            taxonomy_facts, mapping, "Revenue", selected["Revenue"], forms, source,
        )
        selected["Net income previous"] = self._previous(
            taxonomy_facts, mapping, "Net income", selected["Net income"], forms, source,
        )
        regime_detail = (
            f"; current period: {regime.period.isoformat()}; filing: {regime.filed.isoformat()} "
            f"{regime.form}; accession: {regime.accession or 'not supplied'}"
            if regime is not None else "; no eligible current reporting regime"
        )
        warnings = (
            f"SEC taxonomy: {taxonomy}; reporting currency: {currency or 'not available'}"
            f"{regime_detail}; no currency conversion performed.",
        )
        return ResearchSnapshot(normalized, generation, profile, _build_sections(selected), now, warnings)

    @staticmethod
    def _issuer_symbol_candidates(symbol: str) -> tuple[str, ...]:
        candidates = []
        for pattern in (r"\$[A-Z0-9]+$", r"[.-]P[A-Z0-9]*$", r"\.[A-Z]$"):
            candidate = re.sub(pattern, "", symbol)
            if candidate and candidate != symbol and candidate not in candidates:
                candidates.append(candidate)
        return tuple(candidates)

    def _choose_taxonomy(self, facts_root: dict[str, Any], forms: frozenset[str], period_mode: str) -> str:
        regime = self._select_reporting_regime(facts_root, forms)
        if regime is not None:
            return regime.taxonomy
        return "ifrs-full" if period_mode == "quarterly" and facts_root.get("ifrs-full") else "us-gaap"

    def _select_reporting_regime(
        self, facts_root: dict[str, Any], forms: frozenset[str],
    ) -> ReportingRegime | None:
        grouped: dict[tuple[str, date, str, str], dict[str, object]] = {}
        for taxonomy, mapping in self.TAXONOMIES.items():
            facts = facts_root.get(taxonomy, {})
            if not isinstance(facts, dict):
                continue
            for metric, (concepts, kind) in mapping.items():
                if kind != "monetary":
                    continue
                for concept in concepts:
                    node = facts.get(concept)
                    units = node.get("units", {}) if isinstance(node, dict) else {}
                    if not isinstance(units, dict):
                        continue
                    for unit, rows in units.items():
                        currency = str(unit).upper()
                        if not re.fullmatch(r"[A-Z]{3}", currency) or not isinstance(rows, list):
                            continue
                        for raw in rows:
                            candidate = self.selector._candidate(concept, currency, raw)
                            if candidate is None or candidate.form not in forms:
                                continue
                            filing_key = candidate.accession or candidate.filed.isoformat()
                            key = (taxonomy, candidate.end, candidate.form, filing_key)
                            group = grouped.setdefault(key, {
                                "filed": candidate.filed,
                                "accession": candidate.accession,
                                "metrics": set(),
                                "currencies": {},
                            })
                            group["filed"] = max(group["filed"], candidate.filed)
                            group["metrics"].add(metric)
                            currencies = group["currencies"]
                            currencies.setdefault(currency, set()).add(metric)
        if not grouped:
            return None

        def group_rank(item):
            (taxonomy, period, form, _filing_key), data = item
            native = int(form in self.NATIVE_FORMS[taxonomy])
            amended = int(form.endswith("/A"))
            return (
                period,
                data["filed"],
                native,
                amended,
                len(data["metrics"]),
                int(taxonomy == "us-gaap"),
            )

        (taxonomy, period, form, _filing_key), data = max(grouped.items(), key=group_rank)
        currencies = data["currencies"]
        currency = max(currencies, key=lambda value: (len(currencies[value]), value))
        return ReportingRegime(
            taxonomy, currency, period, data["filed"], form, str(data["accession"] or ""),
        )

    @staticmethod
    def _reporting_currency(
        facts: dict[str, Any], mapping: dict[str, tuple[tuple[str, ...], str]], forms: frozenset[str],
    ) -> str | None:
        regimes: dict[tuple[date, date, str], set[str]] = {}
        selector = SecFactSelector()
        for concepts, kind in mapping.values():
            if kind != "monetary":
                continue
            for concept in concepts:
                node = facts.get(concept)
                units = node.get("units", {}) if isinstance(node, dict) else {}
                if not isinstance(units, dict):
                    continue
                for unit, rows in units.items():
                    currency = str(unit).upper()
                    if not re.fullmatch(r"[A-Z]{3}", currency) or not isinstance(rows, list):
                        continue
                    for raw in rows:
                        candidate = selector._candidate(concept, currency, raw)
                        if candidate is None or candidate.form not in forms:
                            continue
                        key = (candidate.end, candidate.filed, candidate.accession)
                        regimes.setdefault(key, set()).add(currency)
        if not regimes:
            return None
        latest = max(regimes)
        return sorted(regimes[latest])[0]

    @staticmethod
    def _facts_for_regime(
        facts: dict[str, Any],
        mapping: dict[str, tuple[tuple[str, ...], str]],
        regime: ReportingRegime | None,
    ) -> dict[str, Any]:
        if regime is None:
            return {}
        filtered: dict[str, Any] = {}
        for concepts, kind in mapping.values():
            accepted_units = (
                {regime.currency} if kind == "monetary"
                else {f"{regime.currency}/shares"} if kind == "per_share"
                else {"shares"}
            )
            for concept in concepts:
                node = facts.get(concept)
                units = node.get("units", {}) if isinstance(node, dict) else {}
                if not isinstance(units, dict):
                    continue
                kept_units: dict[str, list[object]] = {}
                for unit, rows in units.items():
                    if unit not in accepted_units or not isinstance(rows, list):
                        continue
                    kept = []
                    for row in rows:
                        if not isinstance(row, dict) or str(row.get("end") or "") != regime.period.isoformat():
                            continue
                        if regime.accession:
                            if str(row.get("accn") or "") != regime.accession:
                                continue
                        elif str(row.get("filed") or "") != regime.filed.isoformat():
                            continue
                        kept.append(row)
                    if kept:
                        kept_units[str(unit)] = kept
                if kept_units:
                    filtered[concept] = {"units": kept_units}
        return filtered

    @staticmethod
    def _units_for(kind: str, currency: str | None) -> tuple[str, ...]:
        if kind == "shares":
            return ("shares",)
        if kind == "per_share":
            return (f"{currency}/shares",) if currency else ()
        return (currency,) if currency else ()

    def _previous(
        self,
        facts: dict[str, Any],
        mapping: dict[str, tuple[tuple[str, ...], str]],
        fact_name: str,
        latest: ResearchValue,
        forms: frozenset[str],
        source: str,
    ) -> ResearchValue:
        if latest.availability is not Availability.AVAILABLE or not latest.period or not latest.units:
            return ResearchValue.unavailable(source, "A latest comparable period with a stable unit was not selected.")
        try:
            latest_end = date.fromisoformat(latest.period)
        except ValueError:
            return ResearchValue.unavailable(source, "The selected filing period could not be compared.")
        concepts, _kind = mapping[fact_name]
        filtered: dict[str, Any] = {}
        for concept in concepts:
            node = facts.get(concept)
            if not isinstance(node, dict):
                continue
            rows = node.get("units", {}).get(latest.units, []) if isinstance(node.get("units"), dict) else []
            filtered[concept] = {"units": {latest.units: [
                row for row in rows
                if isinstance(row, dict) and isinstance(row.get("end"), str) and row["end"] < latest_end.isoformat()
            ]}}
        return self.selector.select(
            filtered, concepts, (latest.units,), period="previous comparable filing period", forms=forms, source=source,
        )

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
