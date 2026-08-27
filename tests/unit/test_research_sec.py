from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.research.caching import ResearchCache
from app.research.fundamentals import SEC_MIN_REQUEST_INTERVAL_SECONDS, ResearchService, SecCompanyFactsClient, SecFactSelector
from app.research.models import Availability


def _fact(
    value: int, *, end: str, filed: str, form: str = "10-K", accn: str = "1",
    fy: int = 2025, fp: str = "FY", start: str | None = None, frame: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "val": value, "end": end, "filed": filed, "form": form, "accn": accn, "fy": fy, "fp": fp,
    }
    if start:
        result["start"] = start
    if frame:
        result["frame"] = frame
    return result


@pytest.mark.parametrize(
    ("rows", "expected", "reason_fragment"),
    [
        ([_fact(10, end="2025-12-31", filed="2026-02-01"), _fact(11, end="2025-12-31", filed="2026-02-02", accn="2")], Decimal(11), "latest filing 2026-02-02"),
        ([_fact(10, end="2025-12-31", filed="2026-02-01"), _fact(12, end="2025-12-31", filed="2026-03-01", form="10-K/A", accn="2")], Decimal(12), "amendment/restatement precedence"),
        ([_fact(20, end="2025-09-27", filed="2025-11-01", fy=2025), _fact(19, end="2024-09-28", filed="2024-11-01", fy=2024)], Decimal(20), "latest period end 2025-09-27"),
    ],
)
def test_selector_is_deterministic_for_duplicates_amendments_and_noncalendar_years(rows, expected, reason_fragment) -> None:
    facts = {"Revenue": {"units": {"USD": rows}}}
    selected = SecFactSelector().select(facts, ("Revenue",), ("USD",))
    assert selected.value == expected
    assert selected.availability is Availability.AVAILABLE
    assert reason_fragment in selected.selection_reason


def test_selector_uses_standard_concept_priority_and_rejects_custom_or_bad_units() -> None:
    facts = {
        "Preferred": {"units": {"USD": [_fact(5, end="2025-12-31", filed="2026-02-01")]}},
        "Fallback": {"units": {"EUR": [_fact(99, end="2026-12-31", filed="2027-02-01")]}},
        "CompanyCustomRevenue": {"units": {"USD": [_fact(1000, end="2026-12-31", filed="2027-02-01")]}},
    }
    selected = SecFactSelector().select(facts, ("Preferred", "Fallback"), ("USD",))
    assert selected.value == Decimal(5)
    assert "concept priority 1" in selected.selection_reason


def test_selector_returns_explicit_na_for_missing_or_malformed_facts() -> None:
    selected = SecFactSelector().select({"Revenue": {"units": {"USD": [{"val": "bad"}]}}}, ("Revenue",), ("USD",))
    assert selected.availability is Availability.NOT_AVAILABLE
    assert selected.value is None


def test_selector_respects_annual_and_quarterly_filing_modes() -> None:
    facts = {"Revenue": {"units": {"USD": [
        _fact(400, end="2025-12-31", filed="2026-02-01", form="10-K", start="2025-01-01", frame="CY2025"),
        _fact(110, end="2026-03-31", filed="2026-05-01", form="10-Q", fy=2026, fp="Q1", start="2026-01-01", frame="CY2026Q1"),
    ]}}}
    selector = SecFactSelector()
    annual = selector.select(
        facts, ("Revenue",), ("USD",), forms=frozenset({"10-K", "10-K/A"}),
        metric_type="duration", period_mode="annual", taxonomy="us-gaap",
    )
    quarterly = selector.select(
        facts, ("Revenue",), ("USD",), forms=frozenset({"10-Q", "10-Q/A"}),
        metric_type="duration", period_mode="quarterly", taxonomy="us-gaap",
    )
    assert annual.value == Decimal(400)
    assert annual.period == "2025-12-31"
    assert quarterly.value == Decimal(110)
    assert quarterly.period == "2026-03-31"


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_sec_client_declares_identity_rate_limits_and_caches(tmp_path: Path) -> None:
    requests = []
    clock = [1.0]
    sleeps = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return _Response({"ok": True})

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    client = SecCompanyFactsClient(ResearchCache(tmp_path), opener=opener, monotonic=lambda: clock[0], sleeper=sleep)
    first = client._get_json("https://data.sec.gov/example-1")
    second = client._get_json("https://data.sec.gov/example-2")
    cached = client._get_json("https://data.sec.gov/example-1")
    assert first == second == cached == {"ok": True}
    assert len(requests) == 2
    assert "Dietrich AI Labs" in requests[0][0].get_header("User-agent")
    assert sleeps and sleeps[0] == pytest.approx(SEC_MIN_REQUEST_INTERVAL_SECONDS)


def test_research_service_preserves_provenance_and_explicit_unavailable_sections(tmp_path: Path) -> None:
    class Client:
        def company_map(self):
            return {"ACME": {"cik": "0000000001", "name": "Acme Corp"}}

        def companyfacts(self, _cik):
            return {"entityName": "Acme Corp", "facts": {"us-gaap": {
                "Assets": {"units": {"USD": [_fact(100, end="2025-12-31", filed="2026-02-01")]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                    _fact(120, end="2025-12-31", filed="2026-02-01", start="2025-01-01", frame="CY2025"),
                    _fact(100, end="2024-12-31", filed="2025-02-01", fy=2024, start="2024-01-01", frame="CY2024"),
                ]}},
            }}}

        def submissions(self, _cik):
            return {"exchanges": ["NYSE"], "sic": "1234", "sicDescription": "Widgets"}

    snapshot = ResearchService(Client()).load("acme", generation=7)
    assert snapshot.symbol == "ACME"
    assert snapshot.generation == 7
    assert snapshot.profile.cik == "0000000001"
    assert snapshot.sections["Overview"]["Assets"].source == "SEC companyfacts"
    assert snapshot.sections["Growth"]["Revenue growth"].value == Decimal(20)
    assert snapshot.sections["Analyst Outlook"]["Coverage"].availability is Availability.NOT_AVAILABLE


def test_research_cache_is_bounded_and_expired_entries_are_not_returned(tmp_path: Path) -> None:
    cache = ResearchCache(tmp_path, max_entries=2, max_bytes=100000)
    cache.put("one", {"value": 1})
    cache.put("two", {"value": 2})
    cache.put("three", {"value": 3})
    assert len(list(tmp_path.glob("*.json"))) <= 2
    assert cache.get("three") == {"value": 3}
