from __future__ import annotations

from decimal import Decimal
from itertools import permutations

import pytest

from app.research.fundamentals import ResearchService
from app.research.models import Availability


def fact(value: int, *, start: str, end: str, filed: str, accession: str,
         fy: int | None = None, fp: str | None = None, frame: str | None = None,
         form: str = "10-Q") -> dict[str, object]:
    row: dict[str, object] = {
        "val": value, "start": start, "end": end, "filed": filed,
        "form": form, "accn": accession,
    }
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
        return {"TEST": {"cik": "0000000001", "name": "R16 Noncalendar Fixture"}}

    def companyfacts(self, _cik):
        return {"entityName": "R16 Noncalendar Fixture", "facts": self.facts}

    def submissions(self, _cik):
        return {"exchanges": ["NYSE"], "sic": "1000", "sicDescription": "Fixture"}


def snapshot(rows: list[dict[str, object]], *, taxonomy: str = "us-gaap", currency: str = "USD"):
    facts = {taxonomy: {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {currency: rows}}
    }}
    return ResearchService(FixtureClient(facts)).load("TEST", period_mode="quarterly")


def growth(rows: list[dict[str, object]]):
    return snapshot(rows).sections["Growth"]["Revenue growth"]


NONCALENDAR_CASES = (
    ("Q1", "2024-09-29", "2024-12-28", "CY2024Q4", "2023-10-01", "2023-12-30", "CY2023Q4"),
    ("Q2", "2024-12-29", "2025-03-29", "CY2025Q1", "2023-12-31", "2024-03-30", "CY2024Q1"),
    ("Q3", "2025-03-30", "2025-06-28", "CY2025Q2", "2024-03-31", "2024-06-29", "CY2024Q2"),
    ("Q4", "2025-06-29", "2025-09-27", "CY2025Q3", "2024-06-30", "2024-09-28", "CY2024Q3"),
)


def noncalendar_rows(case: tuple[str, str, str, str, str, str, str], *, frames: bool = True):
    fp, current_start, current_end, current_frame, prior_start, prior_end, prior_frame = case
    return [
        fact(110, start=current_start, end=current_end, filed="2025-11-01", accession="current",
             fy=2025, fp=fp, frame=current_frame if frames else None),
        fact(100, start=prior_start, end=prior_end, filed="2024-11-01", accession="prior",
             fy=2024, fp=fp, frame=prior_frame if frames else None),
    ]


@pytest.mark.parametrize("case", NONCALENDAR_CASES)
def test_valid_noncalendar_fiscal_quarters_use_fp_fy_not_calendar_frame(case) -> None:
    result = growth(noncalendar_rows(case))
    assert result.value == Decimal(10)
    assert f"{case[0]} fiscal year 2024 compared with fiscal year 2025" in (result.comparability_result or "")


@pytest.mark.parametrize("case", NONCALENDAR_CASES)
def test_frame_present_and_omitted_are_fiscally_equivalent(case) -> None:
    with_frame = growth(noncalendar_rows(case, frames=True))
    without_frame = growth(noncalendar_rows(case, frames=False))
    assert (with_frame.value, with_frame.comparability_result) == (
        without_frame.value, without_frame.comparability_result
    )


def test_apple_like_52_53_week_q1_crosses_calendar_and_fiscal_year_boundary() -> None:
    result = growth(noncalendar_rows(NONCALENDAR_CASES[0]))
    assert result.value == Decimal(10)
    assert result.period == "2023-12-30 to 2024-12-28"
    assert "Q1 fiscal year 2024 compared with fiscal year 2025" in (result.selection_reason or "")


def test_noncalendar_row_order_and_quarter_ytd_disambiguation_are_stable() -> None:
    current, prior = noncalendar_rows(NONCALENDAR_CASES[0])
    ytd_current = fact(330, start="2024-06-30", end="2024-12-28", filed="2025-11-01",
                       accession="current-ytd", fy=2025, fp="Q1", frame="CY2024Q4YTD")
    ytd_prior = fact(300, start="2023-07-02", end="2023-12-30", filed="2024-11-01",
                     accession="prior-ytd", fy=2024, fp="Q1", frame="CY2023Q4YTD")
    outcomes = set()
    for order in permutations((current, prior, ytd_current, ytd_prior)):
        result = snapshot(list(order))
        revenue = result.sections["Overview"]["Revenue"]
        yoy = result.sections["Growth"]["Revenue growth"]
        outcomes.add((revenue.value, revenue.accession, revenue.period_semantics, yoy.value, yoy.period))
    assert outcomes == {(Decimal(110), "current", "quarterly", Decimal(10), "2023-12-30 to 2024-12-28")}


def test_noncalendar_partial_amendment_preserves_fiscal_yoy_and_latest_metric() -> None:
    current, prior = noncalendar_rows(NONCALENDAR_CASES[0])
    amended = dict(current)
    amended.update({"val": 120, "filed": "2025-12-01", "form": "10-Q/A", "accn": "current-amend"})
    result = snapshot([prior, current, amended])
    revenue = result.sections["Overview"]["Revenue"]
    yoy = result.sections["Growth"]["Revenue growth"]
    assert (revenue.value, revenue.accession, revenue.form) == (Decimal(120), "current-amend", "10-Q/A")
    assert yoy.value == Decimal(20)


def test_frame_fallback_is_calendar_labeled_when_fiscal_identity_is_absent() -> None:
    rows = [
        fact(110, start="2024-10-01", end="2024-12-31", filed="2025-02-01",
             accession="current", frame="CY2024Q4"),
        fact(100, start="2023-10-01", end="2023-12-31", filed="2024-02-01",
             accession="prior", frame="CY2023Q4"),
    ]
    result = growth(rows)
    assert result.value == Decimal(10)
    assert "quarter identity from SEC calendar frame" in (result.comparability_result or "")
    assert "year identity from SEC calendar frame" in (result.comparability_result or "")


@pytest.mark.parametrize("bad_frame", ["CY2024Q3", "CY2025Q4", "CY2024Q5", "not-a-frame"])
def test_genuinely_unusable_or_date_contradictory_frames_remain_rejected(bad_frame: str) -> None:
    current, prior = noncalendar_rows(NONCALENDAR_CASES[0])
    current["frame"] = bad_frame
    assert growth([current, prior]).availability is Availability.NOT_AVAILABLE


def test_q4_quarter_does_not_collide_with_annual_fact_at_same_end() -> None:
    current, prior = noncalendar_rows(NONCALENDAR_CASES[3])
    annual = fact(440, start="2024-09-29", end=current["end"], filed="2025-11-15",
                  accession="annual", fy=2025, fp="FY", frame="CY2025", form="10-K")
    result = snapshot([annual, current, prior])
    revenue = result.sections["Overview"]["Revenue"]
    assert (revenue.value, revenue.accession, revenue.period_semantics) == (Decimal(110), "current", "quarterly")
    assert result.sections["Growth"]["Revenue growth"].value == Decimal(10)


def test_taxonomy_transition_does_not_cross_for_previous_comparison() -> None:
    current = noncalendar_rows(NONCALENDAR_CASES[0])[0]
    prior = noncalendar_rows(NONCALENDAR_CASES[0])[1]
    facts = {
        "us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [current]}}},
        "ifrs-full": {"Revenue": {"units": {"USD": [prior]}}},
    }
    result = ResearchService(FixtureClient(facts)).load("TEST", period_mode="quarterly")
    assert result.sections["Growth"]["Revenue growth"].availability is Availability.NOT_AVAILABLE


def test_currency_transition_does_not_cross_for_previous_comparison() -> None:
    current, prior = noncalendar_rows(NONCALENDAR_CASES[0])
    facts = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax": {
        "units": {"USD": [current], "EUR": [prior]}
    }}}
    result = ResearchService(FixtureClient(facts)).load("TEST", period_mode="quarterly")
    assert result.sections["Growth"]["Revenue growth"].availability is Availability.NOT_AVAILABLE
