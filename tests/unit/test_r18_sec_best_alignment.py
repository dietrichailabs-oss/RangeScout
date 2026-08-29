from __future__ import annotations

from datetime import date, timedelta
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
    filed: str = "2026-03-01",
    accession: str = "fixture",
    fy: int | None = None,
    fp: str | None = None,
    frame: str | None = None,
    form: str = "10-Q",
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
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def company_map(self):
        return {"TEST": {"cik": "0000000001", "name": "R18 Fixture"}}

    def companyfacts(self, _cik):
        return {
            "entityName": "R18 Fixture",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": self.rows}
                    }
                }
            },
        }

    def submissions(self, _cik):
        return {"exchanges": ["NYSE"], "sic": "1000", "sicDescription": "Fixture"}


def snapshot(rows: list[dict[str, object]], *, mode: str = "quarterly"):
    return ResearchService(FixtureClient(rows)).load("TEST", period_mode=mode)


def select_one(row: dict[str, object], *, metric_type: str = "duration", mode: str = "quarterly"):
    forms = SecFactSelector.QUARTERLY_FORMS if mode == "quarterly" else SecFactSelector.ANNUAL_FORMS
    return SecFactSelector().select(
        {"Revenue": {"units": {"USD": [row]}}},
        ("Revenue",),
        ("USD",),
        forms=forms,
        metric_type=metric_type,
        period_mode=mode,
        taxonomy="us-gaap",
    )


def test_r17_hold_reproducer_february_to_april_fiscal_q1_yoy() -> None:
    rows = [
        fact(110, start="2025-02-01", end="2025-04-30", filed="2025-06-01",
             accession="current", fy=2026, fp="Q1", frame="CY2025Q1"),
        fact(100, start="2024-02-01", end="2024-04-30", filed="2024-06-01",
             accession="prior", fy=2025, fp="Q1", frame="CY2024Q1"),
    ]
    result = snapshot(rows)
    assert result.sections["Overview"]["Revenue"].value == Decimal(110)
    assert result.sections["Growth"]["Revenue growth"].value == Decimal(10)
    assert "quarterly year-over-year: Q1 fiscal year 2025 compared with fiscal year 2026" == (
        result.sections["Growth"]["Revenue growth"].comparability_result
    )


@pytest.mark.parametrize(("start", "end", "frame"), [
    ("2025-05-01", "2025-07-31", "CY2025Q2"),
    ("2025-08-01", "2025-10-31", "CY2025Q3"),
    ("2025-11-01", "2026-01-31", "CY2025Q4"),
])
def test_off_calendar_quarters_use_unique_best_two_endpoint_alignment(
    start: str, end: str, frame: str
) -> None:
    row = fact(10, start=start, end=end, frame=frame)
    assert select_one(row).availability is Availability.AVAILABLE


def test_february_to_january_fiscal_year_accepts_prior_calendar_frame() -> None:
    rows = [
        fact(1100, start="2024-02-01", end="2025-01-31", accession="current",
             fy=2025, fp="FY", frame="CY2024", form="10-K"),
        fact(1000, start="2023-02-01", end="2024-01-31", accession="prior",
             fy=2024, fp="FY", frame="CY2023", form="10-K"),
    ]
    result = snapshot(rows, mode="annual")
    assert result.sections["Overview"]["Revenue"].value == Decimal(1100)


def test_august_to_july_annual_calendar_fallback_best_aligns_to_end_year() -> None:
    row = fact(10, start="2024-08-01", end="2025-07-31", frame="CY2025", form="10-K")
    assert select_one(row, mode="annual").availability is Availability.AVAILABLE


def test_july_4_quarter_and_january_3_annual_boundaries_remain_valid() -> None:
    quarterly = fact(10, start="2025-04-05", end="2025-07-04", frame="CY2025Q2")
    annual = fact(10, start="2025-01-05", end="2026-01-03", frame="CY2025", form="10-K")
    assert select_one(quarterly).availability is Availability.AVAILABLE
    assert select_one(annual, mode="annual").availability is Availability.AVAILABLE


@pytest.mark.parametrize(("start", "end", "frame"), [
    ("2024-12-29", "2025-12-27", "CY2025"),
    ("2023-12-31", "2025-01-04", "CY2024"),
])
def test_52_and_53_week_years_with_weekday_offsets_remain_valid(
    start: str, end: str, frame: str
) -> None:
    row = fact(10, start=start, end=end, frame=frame, form="10-K")
    assert select_one(row, mode="annual").availability is Availability.AVAILABLE


def test_complete_fiscal_pair_is_authoritative_when_calendar_frame_identity_differs() -> None:
    row = fact(10, start="2024-09-29", end="2024-12-28", fy=2025, fp="Q1", frame="CY2024Q4")
    assert select_one(row).availability is Availability.AVAILABLE


@pytest.mark.parametrize(("fy", "fp"), [(2026, None), (None, "Q1")])
def test_partial_fiscal_identity_still_fails_closed_for_yoy(fy: int | None, fp: str | None) -> None:
    rows = [
        fact(110, start="2025-02-01", end="2025-04-30", accession="current",
             fy=fy, fp=fp, frame="CY2025Q1"),
        fact(100, start="2024-02-01", end="2024-04-30", accession="prior",
             fy=2025, fp="Q1", frame="CY2024Q1"),
    ]
    assert snapshot(rows).sections["Growth"]["Revenue growth"].availability is Availability.NOT_AVAILABLE


def test_both_fiscal_components_absent_use_coherent_calendar_fallback() -> None:
    rows = [
        fact(110, start="2025-02-01", end="2025-04-30", accession="current", frame="CY2025Q1"),
        fact(100, start="2024-02-01", end="2024-04-30", accession="prior", frame="CY2024Q1"),
    ]
    growth = snapshot(rows).sections["Growth"]["Revenue growth"]
    assert growth.value == Decimal(10)
    assert "calendar year-over-year fallback" in (growth.comparability_result or "")


def test_wrong_adjacent_quarter_frame_fails_closed() -> None:
    row = fact(10, start="2025-04-01", end="2025-06-30", frame="CY2025Q1")
    assert select_one(row).availability is Availability.NOT_AVAILABLE


def test_wrong_adjacent_annual_frame_fails_closed() -> None:
    row = fact(10, start="2025-01-01", end="2025-12-31", frame="CY2024", form="10-K")
    assert select_one(row, mode="annual").availability is Availability.NOT_AVAILABLE


@pytest.mark.parametrize("frame", ["CY25Q1", "CY2025Q5", "2025Q1", "CY2025QQ1"])
def test_malformed_frames_fail_closed(frame: str) -> None:
    row = fact(10, start="2025-01-01", end="2025-03-31", frame=frame)
    assert select_one(row).availability is Availability.NOT_AVAILABLE


@pytest.mark.parametrize(("start", "end", "frame", "mode", "form"), [
    ("2025-01-01", "2025-12-31", "CY2025Q4", "annual", "10-K"),
    ("2025-04-01", "2025-06-30", "CY2025", "quarterly", "10-Q"),
])
def test_annual_and_quarter_frame_semantic_mismatches_fail_closed(
    start: str, end: str, frame: str, mode: str, form: str
) -> None:
    row = fact(10, start=start, end=end, frame=frame, form=form)
    assert select_one(row, mode=mode).availability is Availability.NOT_AVAILABLE


def test_ytd_frame_is_not_substituted_for_a_discrete_quarter() -> None:
    row = fact(10, start="2025-01-01", end="2025-06-30", frame="CY2025Q2YTD")
    assert select_one(row).availability is Availability.NOT_AVAILABLE


def test_instant_frames_use_unique_nearest_boundary_and_reject_ties_or_adjacent_errors() -> None:
    valid_quarter = fact(10, start=None, end="2025-07-04", frame="CY2025Q2I")
    valid_annual = fact(10, start=None, end="2026-01-03", frame="CY2025I", form="10-K")
    tie = fact(10, start=None, end="2025-08-15", frame="CY2025Q2I")
    wrong_adjacent = fact(10, start=None, end="2025-09-01", frame="CY2025Q2I")
    assert select_one(valid_quarter, metric_type="instant").availability is Availability.AVAILABLE
    assert select_one(valid_annual, metric_type="instant", mode="annual").availability is Availability.AVAILABLE
    assert select_one(tie, metric_type="instant").availability is Availability.NOT_AVAILABLE
    assert select_one(wrong_adjacent, metric_type="instant").availability is Availability.NOT_AVAILABLE


def test_duration_alignment_tie_fails_closed() -> None:
    start = date(2025, 8, 31)
    end = start + timedelta(days=61)
    row = fact(10, start=start.isoformat(), end=end.isoformat(), frame="CY2025Q3")
    q3 = SecFactSelector._quarter_period(2025, 3)
    q4 = SecFactSelector._quarter_period(2025, 4)
    assert (
        abs((start - q3[0]).days) + abs((end - q3[1]).days)
        == abs((start - q4[0]).days) + abs((end - q4[1]).days)
    )
    assert select_one(row).availability is Availability.NOT_AVAILABLE


def test_wrong_decoy_and_row_order_cannot_change_fiscal_yoy_result() -> None:
    current = fact(110, start="2025-02-01", end="2025-04-30", accession="current",
                   fy=2026, fp="Q1", frame="CY2025Q1")
    prior = fact(100, start="2024-02-01", end="2024-04-30", accession="prior",
                 fy=2025, fp="Q1", frame="CY2024Q1")
    decoy = fact(80, start="2024-05-01", end="2024-07-31", filed="2025-01-01",
                 accession="wrong-q2", fy=2025, fp="Q2", frame="CY2024Q2")
    outcomes = {
        snapshot(list(order)).sections["Growth"]["Revenue growth"].value
        for order in permutations((current, prior, decoy))
    }
    assert outcomes == {Decimal(10)}
