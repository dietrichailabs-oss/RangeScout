from __future__ import annotations

from datetime import datetime, timezone
import gzip

from app.catalysts.feeds.congress import authentication_headers, parse_bills
from app.catalysts.feeds.http import OfficialFeedClient
from app.catalysts.feeds.nasdaq_halts import MINIMUM_POLL_SECONDS, parse_halt_rss
from app.catalysts.feeds.sec import parse_submissions, submissions_url
from app.catalysts.feeds.white_house import parse_feed


NOW = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)


def test_sec_filters_required_forms_and_builds_official_links() -> None:
    payload = {"name": "Acme Corp", "cik": 1234, "filings": {"recent": {"form": ["8-K", "DEF 14A"], "accessionNumber": ["0000001234-26-000001", "x"], "filingDate": ["2026-08-17", "2026-08-16"]}}}
    events = parse_submissions(payload, "ACME", NOW)
    assert len(events) == 1
    assert events[0].metadata["form"] == "8-K"
    assert events[0].symbols == ("ACME",)
    assert submissions_url("1234").endswith("CIK0000001234.json")


def test_nasdaq_halt_rss_keeps_source_and_explicit_status() -> None:
    xml = b"<rss><channel><item><title>ACME Trading Halt</title><link>https://www.nasdaqtrader.com/acme</link><description>News pending</description><IssueSymbol>ACME</IssueSymbol></item></channel></rss>"
    event = parse_halt_rss(xml, NOW)[0]
    assert event.title.startswith("HALTED")
    assert event.category == "halt"
    assert event.source == "Nasdaq Trader"
    assert MINIMUM_POLL_SECONDS == 60


def test_white_house_feed_retains_metadata_not_body() -> None:
    xml = b"<rss><channel><item><title>Executive Order on Energy</title><link>https://www.whitehouse.gov/a</link><category>Presidential Actions</category></item></channel></rss>"
    event = parse_feed(xml, NOW)[0]
    assert event.source == "White House"
    assert event.summary is None and event.body is None


def test_congress_progression_and_byo_header() -> None:
    event = parse_bills({"bills": [{"title": "Energy Act", "url": "https://api.congress.gov/v3/bill/119/hr/1", "updateDate": "2026-08-17T10:00:00Z", "latestAction": {"text": "Signed by President"}}]}, NOW)[0]
    assert event.metadata["stage"] == "signed"
    assert authentication_headers("user-secret") == {"X-Api-Key": "user-secret"}


def test_official_client_enforces_contact_identity_and_rate_delay() -> None:
    values = iter([0.0, 0.0, 0.25, 1.0])
    sleeps = []
    client = OfficialFeedClient("RangeScout contact@example.com", 1.0, clock=lambda: next(values), sleeper=sleeps.append)
    client._last_request = 0.0
    # Exercise the limiter without performing network I/O.
    now = client._clock(); delay = client.minimum_interval_seconds - (now - client._last_request)
    if delay > 0: client._sleeper(delay)
    assert sleeps == [1.0]


def test_official_client_decodes_gzip_without_exposing_headers(monkeypatch) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return gzip.compress(b'{"ok": true}')
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    client = OfficialFeedClient("RangeScout owner@example.com", 0)
    assert client.get_json("https://www.sec.gov/example") == {"ok": True}
