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
    filing_family: str


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
    """Select one auditable fact using period-aware stable tie-breakers."""

    ELIGIBLE_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})
    ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"})
    QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
    _ANNUAL_FRAME = re.compile(r"^CY\d{4}$", re.IGNORECASE)
    _ANNUAL_FRAME_CAPTURE = re.compile(r"^CY(\d{4})$", re.IGNORECASE)
    _QUARTER_FRAME = re.compile(r"^CY\d{4}Q[1-4]$", re.IGNORECASE)
    _QUARTER_FRAME_CAPTURE = re.compile(r"^CY(\d{4})Q([1-4])$", re.IGNORECASE)
    _YTD_FRAME_CAPTURE = re.compile(r"^CY(\d{4})Q([1-4])YTD$", re.IGNORECASE)
    _INSTANT_FRAME_CAPTURE = re.compile(r"^CY(\d{4})(?:Q([1-4]))?I$", re.IGNORECASE)

    def select(
        self,
        facts: dict[str, Any],
        concepts: Iterable[str],
        units: Iterable[str],
        *,
        period: str = "latest",
        forms: frozenset[str] | None = None,
        source: str = "SEC companyfacts",
        metric_type: str = "auto",
        period_mode: str | None = None,
        taxonomy: str | None = None,
        before_end: date | None = None,
        required_fiscal_period: str | None = None,
        required_period_semantics: str | None = None,
        comparable_duration_days: int | None = None,
        required_quarter_identity: str | None = None,
        required_fiscal_year: int | None = None,
        comparison_basis: str | None = None,
    ) -> ResearchValue:
        concept_order = {concept: index for index, concept in enumerate(concepts)}
        unit_order = {unit: index for index, unit in enumerate(units)}
        candidates: list[SecFactCandidate] = []
        for concept in concept_order:
            node = facts.get(concept)
            if not isinstance(node, dict):
                continue
            unit_payloads = node.get("units", {})
            if not isinstance(unit_payloads, dict):
                continue
            for unit in unit_order:
                rows = unit_payloads.get(unit, [])
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    candidate = self._candidate(concept, unit, row)
                    if candidate is not None and (forms is None or candidate.form in forms):
                        candidates.append(candidate)
        if not candidates:
            return ResearchValue.unavailable(source, "No eligible standard-taxonomy fact with an accepted unit.")

        resolved_metric_type = metric_type
        if resolved_metric_type == "auto":
            resolved_metric_type = "duration" if any(item.start is not None for item in candidates) else "instant"
        if resolved_metric_type not in {"instant", "duration"}:
            raise ValueError(f"Unsupported SEC metric type: {resolved_metric_type}")
        resolved_mode = period_mode or self._infer_period_mode(forms)
        if resolved_metric_type == "duration" and resolved_mode not in {"annual", "quarterly"}:
            raise ValueError("Duration fact selection requires annual or quarterly period mode.")

        compatible: list[tuple[SecFactCandidate, str, int | None]] = []
        for item in candidates:
            if before_end is not None and item.end >= before_end:
                continue
            semantics, duration_days = self.period_semantics(item, resolved_metric_type, resolved_mode)
            if not self.frame_matches_economic_period(item, semantics):
                continue
            if resolved_metric_type == "instant":
                if semantics != "instant":
                    continue
            elif resolved_mode == "annual":
                if semantics not in {"annual", "annual_transition"}:
                    continue
            elif semantics != "quarterly":
                continue
            if required_period_semantics and semantics != required_period_semantics:
                continue
            if required_fiscal_period and item.fiscal_period != required_fiscal_period:
                continue
            if resolved_mode == "quarterly" and (
                required_quarter_identity or required_fiscal_year is not None
            ):
                identity = self.coherent_quarterly_identity(item, semantics)
                if identity is None:
                    continue
                quarter_identity, year_identity, _provenance = identity
                if required_quarter_identity and quarter_identity != required_quarter_identity:
                    continue
                if required_fiscal_year is not None and year_identity != required_fiscal_year:
                    continue
            else:
                if required_quarter_identity and self.quarter_identity(item) != required_quarter_identity:
                    continue
                if required_fiscal_year is not None and self.fiscal_year_identity(item) != required_fiscal_year:
                    continue
            if comparable_duration_days is not None:
                if duration_days is None:
                    continue
                tolerance = 45 if resolved_mode == "annual" else 21
                if abs(duration_days - comparable_duration_days) > tolerance:
                    continue
            compatible.append((item, semantics, duration_days))

        if not compatible:
            target = "point-in-time" if resolved_metric_type == "instant" else f"genuine {resolved_mode}-duration"
            return ResearchValue.unavailable(
                source,
                f"No {target} fact exists in the selected taxonomy, unit, form and reporting regime; "
                "duration-incompatible quarter/YTD facts were not substituted.",
            )

        def rank(entry: tuple[SecFactCandidate, str, int | None]) -> tuple[object, ...]:
            item, semantics, duration_days = entry
            amended = 1 if item.form.endswith("/A") else 0
            evidence = self._semantic_evidence(item, semantics, duration_days)
            duration_target = 365 if resolved_mode == "annual" else 91
            duration_distance = abs((duration_days or duration_target) - duration_target)
            return (
                item.end,
                item.filed,
                amended,
                evidence,
                -duration_distance,
                -concept_order[item.concept],
                -unit_order[item.unit],
                item.accession,
                item.start or date.min,
                item.frame or "",
                item.fiscal_period or "",
                str(item.value),
            )

        winner, semantics, duration_days = max(compatible, key=rank)
        start_text = winner.start.isoformat() if winner.start else "instant"
        reason = (
            f"latest period end {winner.end.isoformat()}; latest filing {winner.filed.isoformat()}; "
            f"amendment/restatement precedence applied; period-aware {semantics} selection; "
            f"taxonomy {taxonomy or 'not supplied'}; "
            f"concept {winner.concept}; unit {winner.unit}; form {winner.form}; "
            f"accession {winner.accession or 'not supplied'}; filed {winner.filed.isoformat()}; "
            f"start {start_text}; end {winner.end.isoformat()}; "
            f"duration {duration_days if duration_days is not None else 'instant'} days; "
            f"fiscal period {winner.fiscal_period or 'not supplied'}; frame {winner.frame or 'not supplied'}; "
            f"comparison basis {comparison_basis or 'current-period selection'}; "
            f"concept priority {concept_order[winner.concept] + 1}; unit priority {unit_order[winner.unit] + 1}; "
            "row-order-independent deterministic tie-breakers applied"
        )
        return ResearchValue(
            winner.value,
            source,
            period=winner.end.isoformat(),
            units=winner.unit,
            filing_date=winner.filed,
            availability=Availability.AVAILABLE,
            selection_reason=reason,
            taxonomy=taxonomy,
            concept=winner.concept,
            accession=winner.accession or None,
            form=winner.form,
            start_date=winner.start,
            end_date=winner.end,
            fiscal_year=winner.fiscal_year,
            fiscal_period=winner.fiscal_period,
            frame=winner.frame,
            duration_days=duration_days,
            period_semantics=semantics,
            period_mode=resolved_mode,
            comparability_result=(
                "current fact selected with explicit duration semantics"
                if before_end is None else (
                    comparison_basis
                    or "prior fact matches taxonomy, unit, fiscal period and duration semantics"
                )
            ),
        )

    @staticmethod
    def _infer_period_mode(forms: frozenset[str] | None) -> str | None:
        if forms and forms.issubset(SecFactSelector.QUARTERLY_FORMS):
            return "quarterly"
        if forms and forms.issubset(SecFactSelector.ANNUAL_FORMS):
            return "annual"
        return None

    @classmethod
    def period_semantics(
        cls, item: SecFactCandidate, metric_type: str, period_mode: str | None,
    ) -> tuple[str, int | None]:
        if metric_type == "instant":
            return ("instant", None) if item.start is None else ("duration_incompatible", cls._duration_days(item))
        if item.start is None:
            return "duration_unknown", None
        duration_days = cls._duration_days(item)
        frame = (item.frame or "").upper()
        fp = (item.fiscal_period or "").upper()
        if "YTD" in frame:
            return (
                ("year_to_date", duration_days)
                if 45 <= duration_days < 360
                else ("duration_unknown", duration_days)
            )
        if cls._QUARTER_FRAME.fullmatch(frame):
            return (
                ("quarterly", duration_days)
                if 45 <= duration_days <= 140
                else ("duration_unknown", duration_days)
            )
        if re.search(r"Q[1-4]", frame):
            return "quarterly" if 45 <= duration_days <= 140 else "year_to_date", duration_days
        if cls._ANNUAL_FRAME.fullmatch(frame):
            if 250 <= duration_days <= 430:
                return "annual", duration_days
            if item.form in cls.ANNUAL_FORMS and fp in {"", "FY"} and 120 <= duration_days < 250:
                return "annual_transition", duration_days
            return "duration_unknown", duration_days
        if item.form in cls.QUARTERLY_FORMS or fp in {"Q1", "Q2", "Q3", "Q4"}:
            return ("quarterly" if 45 <= duration_days <= 140 else "year_to_date"), duration_days
        if item.form in cls.ANNUAL_FORMS and fp == "FY":
            if 250 <= duration_days <= 430:
                return "annual", duration_days
            if 120 <= duration_days < 250:
                return "annual_transition", duration_days
            return "duration_unknown", duration_days
        return "duration_unknown", duration_days

    @classmethod
    def quarter_identity(cls, item: SecFactCandidate) -> str | None:
        fiscal_period = (item.fiscal_period or "").upper()
        fp_identity = fiscal_period if fiscal_period in {"Q1", "Q2", "Q3", "Q4"} else None
        match = cls._QUARTER_FRAME_CAPTURE.fullmatch((item.frame or "").upper())
        frame_identity = f"Q{match.group(2)}" if match else None
        return fp_identity or frame_identity

    @classmethod
    def fiscal_year_identity(cls, item: SecFactCandidate) -> int | None:
        frame = (item.frame or "").upper()
        match = cls._QUARTER_FRAME_CAPTURE.fullmatch(frame) or cls._ANNUAL_FRAME_CAPTURE.fullmatch(frame)
        frame_year = int(match.group(1)) if match else None
        return item.fiscal_year if item.fiscal_year is not None else frame_year

    @classmethod
    def coherent_quarterly_identity(
        cls,
        item: SecFactCandidate,
        semantics: str,
    ) -> tuple[str, int, str] | None:
        """Return one coherent fiscal pair or one coherent calendar-frame fallback."""
        fiscal_period_text = (item.fiscal_period or "").strip().upper()
        fiscal_quarter = (
            fiscal_period_text
            if fiscal_period_text in {"Q1", "Q2", "Q3", "Q4"}
            else None
        )
        fiscal_year = item.fiscal_year
        fiscal_period_supplied = bool(fiscal_period_text)
        fiscal_year_supplied = fiscal_year is not None

        if fiscal_quarter is not None and fiscal_year_supplied:
            return fiscal_quarter, fiscal_year, "fiscal"
        if fiscal_period_supplied or fiscal_year_supplied:
            return None

        frame = (item.frame or "").upper()
        if semantics == "instant":
            match = cls._INSTANT_FRAME_CAPTURE.fullmatch(frame)
            if match is None or match.group(2) is None:
                return None
        else:
            match = cls._QUARTER_FRAME_CAPTURE.fullmatch(frame)
            if match is None:
                return None
        frame_year = int(match.group(1))
        frame_quarter = int(match.group(2))
        if not cls.frame_matches_economic_period(item, semantics):
            return None
        return f"Q{frame_quarter}", frame_year, "calendar_frame_fallback"

    @classmethod
    def frame_matches_economic_period(cls, item: SecFactCandidate, semantics: str) -> bool:
        """Validate frames using SEC-compatible deterministic best alignment.

        A complete fiscal identity remains authoritative for comparison, while
        frame syntax, semantic class, and obvious adjacent-period errors are
        still validated. A duration frame must uniquely minimize the combined
        start/end distance among itself and adjacent periods. Frame-only
        fallback additionally uses the SEC duration bands. Instant frames use
        the nearest nominal boundary, with ties rejected.
        """
        frame = (item.frame or "").upper()
        if not frame:
            return True
        quarter = cls._QUARTER_FRAME_CAPTURE.fullmatch(frame)
        if quarter:
            if semantics != "quarterly":
                return False
            duration_days = cls._duration_days(item) if item.start is not None else None
            return (
                duration_days is not None
                and (
                    cls._has_complete_fiscal_identity(item, semantics)
                    or 61 <= duration_days <= 121
                )
                and cls._duration_frame_is_unique_best(
                    item.start,
                    item.end,
                    cls._adjacent_quarter_periods(int(quarter.group(1)), int(quarter.group(2))),
                )
            )
        annual = cls._ANNUAL_FRAME_CAPTURE.fullmatch(frame)
        if annual:
            if semantics not in {"annual", "annual_transition"}:
                return False
            duration_days = cls._duration_days(item) if item.start is not None else None
            return (
                duration_days is not None
                and (
                    cls._has_complete_fiscal_identity(item, semantics)
                    or 335 <= duration_days <= 395
                )
                and cls._duration_frame_is_unique_best(
                    item.start,
                    item.end,
                    cls._adjacent_annual_periods(int(annual.group(1))),
                )
            )
        ytd = cls._YTD_FRAME_CAPTURE.fullmatch(frame)
        if ytd:
            return semantics == "year_to_date" and cls._duration_frame_is_unique_best(
                item.start,
                item.end,
                cls._adjacent_ytd_periods(int(ytd.group(1)), int(ytd.group(2))),
            )
        instant = cls._INSTANT_FRAME_CAPTURE.fullmatch(frame)
        if instant:
            quarter_number = instant.group(2)
            return semantics == "instant" and cls._instant_frame_is_unique_best(
                item.end,
                int(instant.group(1)),
                int(quarter_number) if quarter_number is not None else None,
            )
        return False

    @classmethod
    def _has_complete_fiscal_identity(
        cls,
        item: SecFactCandidate,
        semantics: str,
    ) -> bool:
        fiscal_period = (item.fiscal_period or "").strip().upper()
        if item.fiscal_year is None:
            return False
        if semantics == "quarterly":
            return fiscal_period in {"Q1", "Q2", "Q3", "Q4"}
        if semantics in {"annual", "annual_transition"}:
            return fiscal_period == "FY"
        return False

    @staticmethod
    def _duration_frame_is_unique_best(
        start: date | None,
        end: date,
        periods: tuple[tuple[date, date], tuple[date, date], tuple[date, date]],
    ) -> bool:
        if start is None or end < start:
            return False
        scores = tuple(
            abs((start - nominal_start).days) + abs((end - nominal_end).days)
            for nominal_start, nominal_end in periods
        )
        supplied_score = scores[1]
        return scores.count(supplied_score) == 1 and supplied_score == min(scores)

    @classmethod
    def _instant_frame_is_unique_best(
        cls,
        instant: date,
        frame_year: int,
        frame_quarter: int | None,
    ) -> bool:
        if frame_quarter is None:
            boundaries = tuple(date(year, 12, 31) for year in (frame_year - 1, frame_year, frame_year + 1))
        else:
            boundaries = tuple(
                cls._quarter_period(year, quarter)[1]
                for year, quarter in cls._adjacent_quarter_identities(frame_year, frame_quarter)
            )
        scores = tuple(abs((instant - boundary).days) for boundary in boundaries)
        supplied_score = scores[1]
        return scores.count(supplied_score) == 1 and supplied_score == min(scores)

    @classmethod
    def _adjacent_quarter_periods(
        cls,
        frame_year: int,
        frame_quarter: int,
    ) -> tuple[tuple[date, date], tuple[date, date], tuple[date, date]]:
        previous, supplied, following = cls._adjacent_quarter_identities(frame_year, frame_quarter)
        return (
            cls._quarter_period(*previous),
            cls._quarter_period(*supplied),
            cls._quarter_period(*following),
        )

    @staticmethod
    def _adjacent_quarter_identities(
        frame_year: int,
        frame_quarter: int,
    ) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
        previous = (frame_year - 1, 4) if frame_quarter == 1 else (frame_year, frame_quarter - 1)
        following = (frame_year + 1, 1) if frame_quarter == 4 else (frame_year, frame_quarter + 1)
        return previous, (frame_year, frame_quarter), following

    @staticmethod
    def _quarter_period(frame_year: int, frame_quarter: int) -> tuple[date, date]:
        start_month = 1 + (frame_quarter - 1) * 3
        end_month, end_day = {
            1: (3, 31),
            2: (6, 30),
            3: (9, 30),
            4: (12, 31),
        }[frame_quarter]
        return date(frame_year, start_month, 1), date(frame_year, end_month, end_day)

    @staticmethod
    def _adjacent_annual_periods(
        frame_year: int,
    ) -> tuple[tuple[date, date], tuple[date, date], tuple[date, date]]:
        previous_year = frame_year - 1
        following_year = frame_year + 1
        return (
            (date(previous_year, 1, 1), date(previous_year, 12, 31)),
            (date(frame_year, 1, 1), date(frame_year, 12, 31)),
            (date(following_year, 1, 1), date(following_year, 12, 31)),
        )

    @classmethod
    def _adjacent_ytd_periods(
        cls,
        frame_year: int,
        frame_quarter: int,
    ) -> tuple[tuple[date, date], tuple[date, date], tuple[date, date]]:
        previous, supplied, following = cls._adjacent_quarter_identities(frame_year, frame_quarter)

        def ytd_period(identity: tuple[int, int]) -> tuple[date, date]:
            year, quarter = identity
            return date(year, 1, 1), cls._quarter_period(year, quarter)[1]

        return ytd_period(previous), ytd_period(supplied), ytd_period(following)

    @staticmethod
    def _duration_days(item: SecFactCandidate) -> int:
        assert item.start is not None
        return (item.end - item.start).days + 1

    @classmethod
    def _semantic_evidence(cls, item: SecFactCandidate, semantics: str, duration_days: int | None) -> int:
        frame = (item.frame or "").upper()
        fp = (item.fiscal_period or "").upper()
        if semantics == "instant":
            return 3
        if semantics in {"annual", "annual_transition"} and cls._ANNUAL_FRAME.fullmatch(frame):
            return 4
        if semantics == "quarterly" and cls._QUARTER_FRAME.fullmatch(frame):
            return 4
        if semantics in {"annual", "annual_transition"} and fp == "FY":
            return 3
        if semantics == "quarterly" and fp in {"Q1", "Q2", "Q3", "Q4"}:
            return 3
        return 1 if duration_days is not None else 0

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
        if start is not None and start > end:
            return None
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
    INSTANT_METRICS = frozenset({
        "Assets", "Liabilities", "Equity", "Cash", "Debt", "Current assets",
        "Current liabilities", "Inventory", "Shares outstanding",
    })

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
            metric_type = "instant" if name in self.INSTANT_METRICS else "duration"
            selected[name] = self.selector.select(
                current_facts, concepts, units, forms=forms, source=source,
                metric_type=metric_type, period_mode=period_mode, taxonomy=taxonomy,
            )
        current_accessions = sorted({
            value.accession for value in selected.values()
            if value.availability is Availability.AVAILABLE and value.accession
        })
        selected["Revenue previous"] = self._previous(
            taxonomy_facts, mapping, "Revenue", selected["Revenue"], forms, source,
        )
        selected["Net income previous"] = self._previous(
            taxonomy_facts, mapping, "Net income", selected["Net income"], forms, source,
        )
        regime_detail = (
            f"; current period: {regime.period.isoformat()}; filing: {regime.filed.isoformat()} "
            f"{regime.form}; accession: {regime.accession or 'not supplied'}; "
            f"filing family: {regime.filing_family}"
            if regime is not None else "; no eligible current reporting regime"
        )
        provenance_detail = (
            f"; metric-level current provenance spans accessions: {', '.join(current_accessions)}"
            if len(current_accessions) > 1 else ""
        )
        warnings = (
            f"SEC taxonomy: {taxonomy}; reporting currency: {currency or 'not available'}"
            f"{regime_detail}{provenance_detail}; no currency conversion performed.",
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
            self._form_family(form),
        )

    @staticmethod
    def _form_family(form: str) -> str:
        return form[:-2] if form.endswith("/A") else form

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
                        row_form = str(row.get("form") or "")
                        if row_form not in SecFactSelector.ELIGIBLE_FORMS:
                            continue
                        if ResearchService._form_family(row_form) != regime.filing_family:
                            continue
                        try:
                            row_filed = date.fromisoformat(str(row.get("filed") or ""))
                        except ValueError:
                            continue
                        if row_filed > regime.filed:
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
        if (
            latest.availability is not Availability.AVAILABLE
            or not latest.units
            or latest.end_date is None
            or not latest.concept
            or not latest.period_semantics
        ):
            return ResearchValue.unavailable(
                source, "A latest fact with explicit taxonomy, unit and duration semantics was not selected."
            )
        node = facts.get(latest.concept)
        if not isinstance(node, dict):
            return ResearchValue.unavailable(source, "The selected current concept has no prior same-taxonomy facts.")
        unit_rows = node.get("units", {})
        rows = unit_rows.get(latest.units, []) if isinstance(unit_rows, dict) else []
        filtered = {latest.concept: {"units": {latest.units: list(rows) if isinstance(rows, list) else []}}}
        metric_type = "instant" if fact_name in self.INSTANT_METRICS else "duration"
        required_fiscal_period = latest.fiscal_period
        required_quarter_identity = None
        current_fiscal_year = self._research_value_fiscal_year(latest)
        required_fiscal_year = current_fiscal_year - 1 if current_fiscal_year is not None else None
        comparison_basis = (
            f"annual fiscal-year-over-year: fiscal year {required_fiscal_year} compared with "
            f"fiscal year {current_fiscal_year}"
            if current_fiscal_year is not None else "same-period prior comparable fact"
        )
        if latest.period_mode == "quarterly":
            current_identity = self._research_value_coherent_quarterly_identity(latest)
            if current_identity is None:
                return ResearchValue.unavailable(
                    source,
                    "Quarterly year-over-year comparison requires either a complete SEC fiscal fy/fp pair "
                    "or a complete date-compatible SEC calendar frame; partial fiscal identity is not mixed "
                    "with calendar-frame identity.",
                )
            required_quarter_identity, current_fiscal_year, identity_provenance = current_identity
            required_fiscal_period = None
            required_fiscal_year = current_fiscal_year - 1
            if identity_provenance == "fiscal":
                comparison_basis = (
                    f"quarterly year-over-year: {required_quarter_identity} fiscal year "
                    f"{required_fiscal_year} compared with fiscal year {current_fiscal_year}"
                )
            else:
                comparison_basis = (
                    f"quarterly calendar year-over-year fallback: {required_quarter_identity} calendar year "
                    f"{required_fiscal_year} compared with calendar year {current_fiscal_year}; "
                    "coherent calendar fallback from SEC frame, not filer fiscal identity; "
                    "quarter identity from SEC calendar frame; year identity from SEC calendar frame"
                )
        return self.selector.select(
            filtered,
            (latest.concept,),
            (latest.units,),
            period="previous comparable filing period",
            forms=forms,
            source=source,
            metric_type=metric_type,
            period_mode=latest.period_mode,
            taxonomy=latest.taxonomy,
            before_end=latest.end_date,
            required_fiscal_period=required_fiscal_period,
            required_period_semantics=latest.period_semantics,
            comparable_duration_days=latest.duration_days,
            required_quarter_identity=required_quarter_identity,
            required_fiscal_year=required_fiscal_year,
            comparison_basis=comparison_basis,
        )

    @staticmethod
    def _research_value_coherent_quarterly_identity(
        value: ResearchValue,
    ) -> tuple[str, int, str] | None:
        fiscal_period_text = (value.fiscal_period or "").strip().upper()
        fiscal_quarter = (
            fiscal_period_text
            if fiscal_period_text in {"Q1", "Q2", "Q3", "Q4"}
            else None
        )
        fiscal_year = value.fiscal_year
        fiscal_period_supplied = bool(fiscal_period_text)
        fiscal_year_supplied = fiscal_year is not None
        if fiscal_quarter is not None and fiscal_year_supplied:
            return fiscal_quarter, fiscal_year, "fiscal"
        if fiscal_period_supplied or fiscal_year_supplied or value.end_date is None:
            return None

        frame = (value.frame or "").upper()
        if value.period_semantics == "instant":
            match = SecFactSelector._INSTANT_FRAME_CAPTURE.fullmatch(frame)
            if match is None or match.group(2) is None:
                return None
        else:
            match = SecFactSelector._QUARTER_FRAME_CAPTURE.fullmatch(frame)
            if match is None:
                return None
        frame_year = int(match.group(1))
        frame_quarter = int(match.group(2))
        if value.period_semantics == "instant":
            aligned = SecFactSelector._instant_frame_is_unique_best(
                value.end_date, frame_year, frame_quarter
            )
        else:
            aligned = SecFactSelector._duration_frame_is_unique_best(
                value.start_date,
                value.end_date,
                SecFactSelector._adjacent_quarter_periods(frame_year, frame_quarter),
            )
        if not aligned:
            return None
        return f"Q{frame_quarter}", frame_year, "calendar_frame_fallback"

    @staticmethod
    def _research_value_fiscal_year(value: ResearchValue) -> int | None:
        frame = (value.frame or "").upper()
        match = (
            SecFactSelector._QUARTER_FRAME_CAPTURE.fullmatch(frame)
            or SecFactSelector._ANNUAL_FRAME_CAPTURE.fullmatch(frame)
        )
        frame_year = int(match.group(1)) if match else None
        return value.fiscal_year if value.fiscal_year is not None else frame_year

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
