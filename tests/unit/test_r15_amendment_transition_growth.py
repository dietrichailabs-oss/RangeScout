from __future__ import annotations

from decimal import Decimal
from itertools import permutations

import pytest

from app.research.fundamentals import ResearchService, SecFactSelector
from app.research.models import Availability


def fact(
    value: int,
    *,
    start: str | None,
    end: str,
    filed: str,
    form: str,
    accession: str,
    fy: int | None = None,
    fp: str | None = None,
    frame: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "val": value,
        "end": end,
        "filed": filed,
        "form": form,
        "accn": accession,
    }
    if start is not None:
        row["start"] = start
    if fy is not None:
        row["fy"] = fy
    if fp is not None:
        row["fp"] = fp
    if frame is not None:
        row["frame"] = frame
    return row


class FixtureClient:
    def __init__(self, facts: dict[str, object]) -> None:
        self.facts = facts

    def company_map(self):
        return {"TEST": {"cik": "0000000001", "name": "R15 Fixture"}}

    def companyfacts(self, _cik):
        return {"entityName": "R15 Fixture", "facts": self.facts}

    def submissions(self, _cik):
        return {"exchanges": ["NYSE"], "sic": "1000", "sicDescription": "Fixture"}


def snapshot(facts: dict[str, object], *, mode: str = "annual"):
    return ResearchService(FixtureClient(facts)).load("TEST", period_mode=mode)


def us_gaap_partial(
    *,
    form: str = "10-K",
    amendment_form: str = "10-K/A",
    revenue_rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    original_revenue = fact(
        1000, start="2025-01-01", end="2025-12-31", filed="2026-02-01",
        form=form, accession="orig", fy=2025, fp="FY", frame="CY2025",
    )
    amended_revenue = fact(
        1100, start="2025-01-01", end="2025-12-31", filed="2026-03-01",
        form=amendment_form, accession="amend", fy=2025, fp="FY", frame="CY2025",
    )
    net_income = fact(
        100, start="2025-01-01", end="2025-12-31", filed="2026-02-01",
        form=form, accession="orig", fy=2025, fp="FY", frame="CY2025",
    )
    assets = fact(
        5000, start=None, end="2025-12-31", filed="2026-02-01",
        form=form, accession="orig", fy=2025, fp="FY",
    )
    return {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": revenue_rows or [original_revenue, amended_revenue]}
            },
            "NetIncomeLoss": {"units": {"USD": [net_income]}},
            "Assets": {"units": {"USD": [assets]}},
        }
    }


@pytest.mark.parametrize("reversed_rows", [False, True])
def test_partial_10k_amendment_merges_each_metric_and_preserves_accession(reversed_rows: bool) -> None:
    payload = us_gaap_partial()
    rows = payload["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    if reversed_rows:
        rows.reverse()
    result = snapshot(payload)
    overview = result.sections["Overview"]
    assert overview["Revenue"].value == Decimal(1100)
    assert overview["Revenue"].accession == "amend"
    assert overview["Revenue"].form == "10-K/A"
    assert overview["Net income"].value == Decimal(100)
    assert overview["Net income"].accession == "orig"
    assert overview["Net income"].form == "10-K"
    assert overview["Assets"].value == Decimal(5000)
    assert overview["Assets"].accession == "orig"
    assert "metric-level current provenance spans accessions: amend, orig" in result.warnings[0]


def test_multiple_amendments_select_latest_replacement_per_metric() -> None:
    original = fact(1000, start="2025-01-01", end="2025-12-31", filed="2026-02-01", form="10-K", accession="orig", fy=2025, fp="FY", frame="CY2025")
    first = fact(1050, start="2025-01-01", end="2025-12-31", filed="2026-03-01", form="10-K/A", accession="amend-1", fy=2025, fp="FY", frame="CY2025")
    second = fact(1100, start="2025-01-01", end="2025-12-31", filed="2026-04-01", form="10-K/A", accession="amend-2", fy=2025, fp="FY", frame="CY2025")
    payload = us_gaap_partial(revenue_rows=[second, original, first])
    result = snapshot(payload)
    revenue = result.sections["Overview"]["Revenue"]
    assert revenue.value == Decimal(1100)
    assert revenue.accession == "amend-2"
    assert result.sections["Overview"]["Net income"].accession == "orig"


def test_amendment_can_replace_multiple_metrics_without_erasing_others() -> None:
    payload = us_gaap_partial()
    payload["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(
        fact(120, start="2025-01-01", end="2025-12-31", filed="2026-03-01", form="10-K/A", accession="amend", fy=2025, fp="FY", frame="CY2025")
    )
    result = snapshot(payload)
    overview = result.sections["Overview"]
    assert (overview["Revenue"].value, overview["Revenue"].accession) == (Decimal(1100), "amend")
    assert (overview["Net income"].value, overview["Net income"].accession) == (Decimal(120), "amend")
    assert (overview["Assets"].value, overview["Assets"].accession) == (Decimal(5000), "orig")


@pytest.mark.parametrize(("form", "amendment_form"), [("20-F", "20-F/A"), ("40-F", "40-F/A")])
def test_foreign_partial_amendment_merges_same_filing_family(form: str, amendment_form: str) -> None:
    original_revenue = fact(900, start="2025-01-01", end="2025-12-31", filed="2026-03-01", form=form, accession="foreign-orig", fy=2025, fp="FY", frame="CY2025")
    amended_revenue = fact(950, start="2025-01-01", end="2025-12-31", filed="2026-04-01", form=amendment_form, accession="foreign-amend", fy=2025, fp="FY", frame="CY2025")
    original_profit = fact(90, start="2025-01-01", end="2025-12-31", filed="2026-03-01", form=form, accession="foreign-orig", fy=2025, fp="FY", frame="CY2025")
    assets = fact(4000, start=None, end="2025-12-31", filed="2026-03-01", form=form, accession="foreign-orig", fy=2025, fp="FY")
    payload = {"ifrs-full": {
        "Revenue": {"units": {"EUR": [amended_revenue, original_revenue]}},
        "ProfitLoss": {"units": {"EUR": [original_profit]}},
        "Assets": {"units": {"EUR": [assets]}},
    }}
    overview = snapshot(payload).sections["Overview"]
    assert (overview["Revenue"].value, overview["Revenue"].accession) == (Decimal(950), "foreign-amend")
    assert (overview["Net income"].value, overview["Net income"].accession) == (Decimal(90), "foreign-orig")
    assert (overview["Assets"].value, overview["Assets"].accession) == (Decimal(4000), "foreign-orig")


def test_partial_amendment_never_falls_back_across_periods() -> None:
    payload = us_gaap_partial()
    payload["us-gaap"]["NetIncomeLoss"]["units"]["USD"] = [
        fact(80, start="2024-01-01", end="2024-12-31", filed="2025-02-01", form="10-K", accession="old", fy=2024, fp="FY", frame="CY2024")
    ]
    net_income = snapshot(payload).sections["Overview"]["Net income"]
    assert net_income.availability is Availability.NOT_AVAILABLE
    assert net_income.value is None


def select_annual(row: dict[str, object]):
    return SecFactSelector().select(
        {"Revenue": {"units": {"USD": [row]}}}, ("Revenue",), ("USD",),
        forms=SecFactSelector.ANNUAL_FORMS, metric_type="duration", period_mode="annual", taxonomy="us-gaap",
    )


@pytest.mark.parametrize(("form", "frame"), [
    ("10-K", "CY2025"),
    ("10-K", None),
    ("20-F", None),
    ("40-F", None),
])
def test_short_annual_transition_is_supported_with_strong_filing_context(form: str, frame: str | None) -> None:
    selected = select_annual(fact(
        500, start="2025-07-01", end="2025-12-31", filed="2026-03-01",
        form=form, accession="transition", fy=2025, fp="FY", frame=frame,
    ))
    assert selected.value == Decimal(500)
    assert selected.period_semantics == "annual_transition"
    assert selected.duration_days == 184


@pytest.mark.parametrize(("start", "end", "days"), [
    ("2025-01-05", "2026-01-03", 364),
    ("2025-01-05", "2026-01-10", 371),
    ("2024-01-01", "2024-12-31", 366),
])
def test_annual_duration_accepts_52_53_week_and_leap_years(start: str, end: str, days: int) -> None:
    selected = select_annual(fact(1200, start=start, end=end, filed="2026-03-01", form="10-K", accession="annual", fy=2025, fp="FY"))
    assert selected.value == Decimal(1200)
    assert selected.period_semantics == "annual"
    assert selected.duration_days == days


@pytest.mark.parametrize("row", [
    fact(300, start="2025-10-01", end="2025-12-31", filed="2026-03-01", form="10-K", accession="q4", fy=2025, fp="FY", frame="CY2025Q4"),
    fact(900, start="2025-01-01", end="2025-09-30", filed="2026-03-01", form="10-K", accession="ytd", fy=2025, fp="FY", frame="CY2025Q3YTD"),
    fact(100, start="2025-10-01", end="2025-12-31", filed="2026-03-01", form="10-K", accession="ambiguous-short", fy=2025, fp="FY"),
    fact(1200, start=None, end="2025-12-31", filed="2026-03-01", form="10-K", accession="missing-start", fy=2025, fp="FY"),
])
def test_nonannual_or_ambiguous_rows_are_not_reclassified_as_transition(row: dict[str, object]) -> None:
    selected = select_annual(row)
    assert selected.availability is Availability.NOT_AVAILABLE


def quarterly_payload(*, include_fp: bool, include_frame: bool, include_prior: bool = True):
    kwargs_current = {"fy": 2025, "fp": "Q3" if include_fp else None, "frame": "CY2025Q3" if include_frame else None}
    kwargs_q2 = {"fy": 2025, "fp": "Q2" if include_fp else None, "frame": "CY2025Q2" if include_frame else None}
    kwargs_prior = {"fy": 2024, "fp": "Q3" if include_fp else None, "frame": "CY2024Q3" if include_frame else None}
    rows = [
        fact(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="current-q3", **kwargs_current),
        fact(350, start="2025-04-01", end="2025-06-30", filed="2025-08-01", form="10-Q", accession="current-q2", **kwargs_q2),
    ]
    if include_prior:
        rows.append(fact(300, start="2024-07-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="prior-q3", **kwargs_prior))
    return {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}}}}


@pytest.mark.parametrize(("include_fp", "include_frame"), [(True, True), (False, True), (True, False)])
def test_quarterly_growth_is_stable_same_quarter_yoy(include_fp: bool, include_frame: bool) -> None:
    result = snapshot(quarterly_payload(include_fp=include_fp, include_frame=include_frame), mode="quarterly")
    growth = result.sections["Growth"]["Revenue growth"]
    assert growth.value == Decimal("33.33333333333333333333333333")
    assert "quarterly year-over-year" in growth.selection_reason
    assert "Q3" in (growth.comparability_result or "")
    assert growth.period == "2024-09-30 to 2025-09-30"


def test_quarterly_growth_never_silently_switches_to_qoq_without_fp_or_frame() -> None:
    result = snapshot(quarterly_payload(include_fp=False, include_frame=False), mode="quarterly")
    growth = result.sections["Growth"]["Revenue growth"]
    assert growth.availability is Availability.NOT_AVAILABLE
    assert growth.value is None


def test_quarterly_growth_is_unavailable_without_prior_same_quarter() -> None:
    result = snapshot(quarterly_payload(include_fp=True, include_frame=True, include_prior=False), mode="quarterly")
    growth = result.sections["Growth"]["Revenue growth"]
    assert growth.availability is Availability.NOT_AVAILABLE


def test_noncalendar_fiscal_quarter_uses_fp_not_calendar_sequence() -> None:
    rows = [
        fact(240, start="2025-09-01", end="2025-11-30", filed="2026-01-10", form="10-Q", accession="fy25-q2", fy=2025, fp="Q2"),
        fact(200, start="2024-09-01", end="2024-11-30", filed="2025-01-10", form="10-Q", accession="fy24-q2", fy=2024, fp="Q2"),
        fact(220, start="2025-06-01", end="2025-08-31", filed="2025-10-10", form="10-Q", accession="fy25-q1", fy=2025, fp="Q1"),
    ]
    payload = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}}}}
    growth = snapshot(payload, mode="quarterly").sections["Growth"]["Revenue growth"]
    assert growth.value == Decimal(20)
    assert "Q2" in (growth.comparability_result or "")


def test_52_53_week_quarters_remain_duration_comparable() -> None:
    rows = [
        fact(260, start="2025-06-25", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="fy25-q3", fy=2025, fp="Q3"),
        fact(200, start="2024-07-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="fy24-q3", fy=2024, fp="Q3"),
    ]
    payload = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}}}}
    growth = snapshot(payload, mode="quarterly").sections["Growth"]["Revenue growth"]
    assert growth.value == Decimal(30)


def test_quarterly_growth_does_not_cross_reporting_currency() -> None:
    current = fact(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="usd-current", fy=2025, fp="Q3", frame="CY2025Q3")
    prior_eur = fact(300, start="2024-07-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="eur-prior", fy=2024, fp="Q3", frame="CY2024Q3")
    payload = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [current], "EUR": [prior_eur]}}}}
    growth = snapshot(payload, mode="quarterly").sections["Growth"]["Revenue growth"]
    assert growth.availability is Availability.NOT_AVAILABLE


def test_amendment_and_growth_results_are_row_order_independent() -> None:
    current = fact(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="current", fy=2025, fp="Q3", frame="CY2025Q3")
    amended = fact(420, start="2025-07-01", end="2025-09-30", filed="2025-12-01", form="10-Q/A", accession="current-amend", fy=2025, fp="Q3", frame="CY2025Q3")
    q2 = fact(350, start="2025-04-01", end="2025-06-30", filed="2025-08-01", form="10-Q", accession="q2", fy=2025, fp="Q2", frame="CY2025Q2")
    prior = fact(300, start="2024-07-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="prior", fy=2024, fp="Q3", frame="CY2024Q3")
    outcomes = set()
    for order in permutations((current, amended, q2, prior)):
        payload = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": list(order)}}}}
        result = snapshot(payload, mode="quarterly")
        revenue = result.sections["Overview"]["Revenue"]
        growth = result.sections["Growth"]["Revenue growth"]
        outcomes.add((revenue.value, revenue.accession, growth.value, growth.period, growth.comparability_result))
    assert outcomes == {(
        Decimal(420), "current-amend", Decimal(40), "2024-09-30 to 2025-09-30",
        "quarterly year-over-year: Q3 fiscal year 2024 compared with fiscal year 2025",
    )}



def test_conflicting_quarter_fp_and_frame_is_not_used_for_growth() -> None:
    rows = [
        fact(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="conflict", fy=2025, fp="Q3", frame="CY2025Q2"),
        fact(300, start="2024-07-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="prior", fy=2024, fp="Q3", frame="CY2024Q3"),
    ]
    payload = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}}}}
    growth = snapshot(payload, mode="quarterly").sections["Growth"]["Revenue growth"]
    assert growth.availability is Availability.NOT_AVAILABLE


def test_conflicting_fiscal_year_and_frame_is_not_used_for_growth() -> None:
    rows = [
        fact(400, start="2025-07-01", end="2025-09-30", filed="2025-11-01", form="10-Q", accession="conflict", fy=2024, fp="Q3", frame="CY2025Q3"),
        fact(300, start="2024-07-01", end="2024-09-30", filed="2024-11-01", form="10-Q", accession="prior", fy=2024, fp="Q3", frame="CY2024Q3"),
    ]
    payload = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rows}}}}
    growth = snapshot(payload, mode="quarterly").sections["Growth"]["Revenue growth"]
    assert growth.availability is Availability.NOT_AVAILABLE


def test_partial_amendment_never_crosses_annual_form_families() -> None:
    payload = us_gaap_partial()
    payload["us-gaap"]["NetIncomeLoss"]["units"]["USD"] = [
        fact(999, start="2025-01-01", end="2025-12-31", filed="2026-02-15", form="40-F", accession="other-family", fy=2025, fp="FY", frame="CY2025")
    ]
    net_income = snapshot(payload).sections["Overview"]["Net income"]
    assert net_income.availability is Availability.NOT_AVAILABLE
