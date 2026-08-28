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
    filed: str,
    accession: str,
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
        return {"TEST": {"cik": "0000000001", "name": "R17 Fixture"}}

    def companyfacts(self, _cik):
        return {
            "entityName": "R17 Fixture",
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


def growth(rows: list[dict[str, object]]):
    return snapshot(rows).sections["Growth"]["Revenue growth"]


def select_one(row: dict[str, object], *, metric_type: str, mode: str):
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


def test_required_july_4_q2_frame_and_same_fiscal_quarter_yoy() -> None:
    rows = [
        fact(110, start="2026-04-05", end="2026-07-04", filed="2026-08-01",
             accession="current", fy=2026, fp="Q2", frame="CY2026Q2"),
        fact(100, start="2025-03-30", end="2025-06-28", filed="2025-08-01",
             accession="prior", fy=2025, fp="Q2", frame="CY2025Q2"),
    ]
    result = snapshot(rows)
    assert result.sections["Overview"]["Revenue"].value == Decimal(110)
    assert result.sections["Growth"]["Revenue growth"].value == Decimal(10)


def test_required_january_3_annual_frame_is_not_discarded() -> None:
    rows = [
        fact(1100, start="2025-01-05", end="2026-01-03", filed="2026-03-01",
             accession="current", fy=2025, fp="FY", frame="CY2025", form="10-K"),
        fact(1000, start="2023-12-31", end="2024-12-28", filed="2025-03-01",
             accession="prior", fy=2024, fp="FY", frame="CY2024", form="10-K"),
    ]
    result = snapshot(rows, mode="annual")
    assert result.sections["Overview"]["Revenue"].value == Decimal(1100)


@pytest.mark.parametrize(("quarter", "frame", "ends"), [
    (1, "CY2025Q1", ("2025-03-27", "2025-03-31", "2025-04-04")),
    (2, "CY2025Q2", ("2025-06-26", "2025-06-30", "2025-07-04")),
    (3, "CY2025Q3", ("2025-09-26", "2025-09-30", "2025-10-04")),
    (4, "CY2025Q4", ("2025-12-27", "2025-12-31", "2026-01-04")),
])
def test_quarter_frames_accept_dates_near_nominal_boundaries(
    quarter: int, frame: str, ends: tuple[str, ...]
) -> None:
    for index, end in enumerate(ends):
        end_date = date.fromisoformat(end)
        start = (end_date - timedelta(days=89)).isoformat()
        row = fact(10 + index, start=start, end=end, filed="2026-02-01",
                   accession=f"q{quarter}-{index}", fy=2025, fp=f"Q{quarter}", frame=frame)
        assert select_one(row, metric_type="duration", mode="quarterly").availability is Availability.AVAILABLE


@pytest.mark.parametrize("end", ["2025-12-27", "2025-12-31", "2026-01-04"])
def test_annual_frames_accept_dates_near_nominal_boundary(end: str) -> None:
    row = fact(100, start="2025-01-01", end=end, filed="2026-03-01",
               accession=end, fy=2025, fp="FY", frame="CY2025", form="10-K")
    assert select_one(row, metric_type="duration", mode="annual").availability is Availability.AVAILABLE



def test_quarter_frame_does_not_override_implausible_duration() -> None:
    row = fact(10, start="2025-06-25", end="2025-07-04", filed="2025-08-01",
               accession="too-short", fy=2025, fp="Q2", frame="CY2025Q2")
    assert select_one(row, metric_type="duration", mode="quarterly").availability is Availability.NOT_AVAILABLE


@pytest.mark.parametrize(("end", "frame", "metric_type", "mode", "form"), [
    ("2025-05-01", "CY2025Q1", "duration", "quarterly", "10-Q"),
    ("2025-08-01", "CY2025Q2", "duration", "quarterly", "10-Q"),
    ("2026-02-01", "CY2025", "duration", "annual", "10-K"),
    ("2025-08-01", "CY2025Q2I", "instant", "quarterly", "10-Q"),
])
def test_distant_frame_labels_fail_closed(
    end: str, frame: str, metric_type: str, mode: str, form: str
) -> None:
    row = fact(10, start=None if metric_type == "instant" else "2025-01-01",
               end=end, filed="2026-03-01", accession="distant", frame=frame, form=form)
    assert select_one(row, metric_type=metric_type, mode=mode).availability is Availability.NOT_AVAILABLE


@pytest.mark.parametrize(("end", "frame", "mode", "form"), [
    ("2025-07-04", "CY2025Q2I", "quarterly", "10-Q"),
    ("2026-01-03", "CY2025I", "annual", "10-K"),
])
def test_instant_frames_use_the_same_bounded_nominal_alignment(
    end: str, frame: str, mode: str, form: str
) -> None:
    row = fact(10, start=None, end=end, filed="2026-03-01",
               accession="instant", frame=frame, form=form)
    assert select_one(row, metric_type="instant", mode=mode).availability is Availability.AVAILABLE


def hybrid_rows(*, current_fy: int | None, current_fp: str | None) -> list[dict[str, object]]:
    return [
        fact(110, start="2024-10-01", end="2024-12-31", filed="2025-02-01",
             accession="current", fy=current_fy, fp=current_fp, frame="CY2024Q4"),
        fact(100, start="2023-01-01", end="2023-03-31", filed="2024-05-01",
             accession="actual-prior-q1", fy=2024, fp="Q1", frame="CY2023Q1"),
        fact(80, start="2023-10-01", end="2023-12-31", filed="2024-02-01",
             accession="wrong-quarter-decoy", fy=2024, fp="Q4", frame="CY2023Q4"),
    ]


@pytest.mark.parametrize(("fy", "fp"), [(2025, None), (None, "Q1")])
def test_partial_fiscal_identity_never_hybridizes_with_calendar_frame(
    fy: int | None, fp: str | None
) -> None:
    result = growth(hybrid_rows(current_fy=fy, current_fp=fp))
    assert result.availability is Availability.NOT_AVAILABLE
    assert result.value is None


def test_both_fiscal_components_absent_use_labeled_calendar_fallback() -> None:
    rows = [
        fact(110, start="2024-10-01", end="2024-12-31", filed="2025-02-01",
             accession="current", frame="CY2024Q4"),
        fact(100, start="2023-10-01", end="2023-12-31", filed="2024-02-01",
             accession="prior", frame="CY2023Q4"),
    ]
    result = growth(rows)
    assert result.value == Decimal(10)
    assert "calendar year-over-year fallback" in (result.comparability_result or "")
    assert "not filer fiscal identity" in (result.comparability_result or "")


def test_complete_fiscal_pair_wins_when_calendar_frame_differs() -> None:
    rows = [
        fact(110, start="2024-09-29", end="2024-12-28", filed="2025-02-01",
             accession="current", fy=2025, fp="Q1", frame="CY2024Q4"),
        fact(100, start="2023-10-01", end="2023-12-30", filed="2024-02-01",
             accession="prior", fy=2024, fp="Q1", frame="CY2023Q4"),
    ]
    result = growth(rows)
    assert result.value == Decimal(10)
    assert result.comparability_result == (
        "quarterly year-over-year: Q1 fiscal year 2024 compared with fiscal year 2025"
    )


def test_wrong_quarter_decoy_is_never_selected_in_any_row_order() -> None:
    current = fact(110, start="2024-10-01", end="2024-12-31", filed="2025-02-01",
                   accession="current", fy=2025, fp="Q1", frame="CY2024Q4")
    prior = fact(100, start="2023-10-01", end="2023-12-31", filed="2024-02-01",
                 accession="prior", fy=2024, fp="Q1", frame="CY2023Q4")
    decoy = fact(80, start="2023-10-01", end="2023-12-31", filed="2024-03-01",
                 accession="decoy", fy=2024, fp="Q4", frame="CY2023Q4")
    outcomes = {
        (growth(list(order)).value, growth(list(order)).period)
        for order in permutations((current, prior, decoy))
    }
    assert outcomes == {(Decimal(10), "2023-12-31 to 2024-12-31")}