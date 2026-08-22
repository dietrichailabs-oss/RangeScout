from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from app.historical_store.repository import HistoricalStore
from app.research.analyst.alpha_vantage import AlphaVantageEarningsEstimatesClient
from app.research.analyst.finnhub import FinnhubRecommendationClient
from app.research.analyst.models import AnalystState
from app.research.analyst.service import AnalystService
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def get_json(self, url, headers=None):  # noqa: ANN001, ARG002
        self.calls += 1
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "history.sqlite"
    store = HistoricalStore(path)
    store.close()
    return path


def _service(tmp_path: Path, store, finnhub_payloads, alpha_payloads, *, now_fn=None):
    finnhub_transport = FakeTransport(finnhub_payloads)
    alpha_transport = FakeTransport(alpha_payloads)
    service = AnalystService(
        _database(tmp_path),
        store,
        finnhub=FinnhubRecommendationClient(finnhub_transport),
        alpha_vantage=AlphaVantageEarningsEstimatesClient(alpha_transport),
        now_fn=now_fn,
    )
    return service, finnhub_transport, alpha_transport


def test_no_keys_returns_helpful_state_without_http(tmp_path: Path) -> None:
    service, finnhub, alpha = _service(tmp_path, InMemoryCredentialStore(), [], [])
    result = service.load("AAPL", 7)
    assert not result.values
    assert result.generation == 7
    assert result.provider_states == {
        "finnhub": AnalystState.NOT_CONFIGURED,
        "alpha_vantage": AnalystState.NOT_CONFIGURED,
    }
    assert "Analyst data not configured" in result.messages[0]
    assert finnhub.calls == alpha.calls == 0


def test_finnhub_and_alpha_normalize_then_use_fresh_sqlite_cache(tmp_path: Path) -> None:
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("finnhub", {"api_key": "key_for_test_only"}))
    credentials.save(ProviderCredentials("alpha_vantage", {"api_key": "key_for_test_only"}))
    finnhub_payload = [{"symbol": "AAPL", "period": "2026-08-01", "strongBuy": 10, "buy": 8, "hold": 4, "sell": 1, "strongSell": 0}]
    alpha_payload = {
        "annualEarningsEstimates": [
            {"horizon": "current fiscal year", "fiscalDateEnding": "2026-09-30", "epsEstimateAverage": "7.25", "revenueEstimateAverage": "410000000000", "epsEstimateAnalystCount": "31", "epsEstimateRevisionUpTrailing30Days": "3", "epsEstimateRevisionDownTrailing30Days": "1"},
            {"horizon": "next fiscal year", "fiscalDateEnding": "2027-09-30", "epsEstimateAverage": "8.10", "revenueEstimateAverage": "438000000000", "epsEstimateAnalystCount": "29"},
        ],
        "quarterlyEarningsEstimates": [
            {"horizon": "current quarter", "fiscalDateEnding": "2026-09-30", "epsEstimateAverage": "1.65", "epsEstimateAnalystCount": "28"},
            {"horizon": "next quarter", "fiscalDateEnding": "2026-12-31", "epsEstimateAverage": "2.05", "epsEstimateAnalystCount": "26"},
        ],
    }
    service, finnhub, alpha = _service(tmp_path, credentials, [finnhub_payload], [alpha_payload])
    first = service.load("AAPL", 1)
    second = service.load("AAPL", 1)
    assert str(first.values["Total Analysts"].value) == "23"
    assert str(first.values["Current-year EPS Estimate"].value) == "7.25"
    assert str(first.values["Next-year Revenue Estimate"].value) == "438000000000"
    assert str(first.values["Next-quarter EPS Estimate"].value) == "2.05"
    assert first.provider_states == {"finnhub": AnalystState.FRESH, "alpha_vantage": AnalystState.FRESH}
    assert second.provider_states == {"finnhub": AnalystState.CACHED, "alpha_vantage": AnalystState.CACHED}
    assert finnhub.calls == alpha.calls == 1
    with sqlite3.connect(service.cache.path) as connection:
        rows = connection.execute("SELECT payload_json FROM rs_analyst_cache").fetchall()
    assert len(rows) == 2
    assert all("key_for_test_only" not in row[0] for row in rows)


def test_entitlement_state_is_safe_and_suppresses_repeat_call(tmp_path: Path) -> None:
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("finnhub", {"api_key": "key_for_test_only"}))
    service, finnhub, _alpha = _service(
        tmp_path, credentials, [{"error": "This endpoint requires a premium plan"}], []
    )
    first = service.load("BA")
    second = service.load("BA")
    assert first.provider_states["finnhub"] is AnalystState.ENTITLEMENT_UNAVAILABLE
    assert second.provider_states["finnhub"] is AnalystState.ENTITLEMENT_UNAVAILABLE
    assert finnhub.calls == 1
    assert "key_for_test_only" not in " ".join(first.messages)


def test_expired_cache_falls_back_stale_on_rate_limit(tmp_path: Path) -> None:
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("finnhub", {"api_key": "key_for_test_only"}))
    clock = [datetime.now(timezone.utc)]
    from app.market_data.contracts import RateLimited

    service, finnhub, _alpha = _service(
        tmp_path,
        credentials,
        [[{"period": "2026-08-01", "strongBuy": 1, "buy": 2, "hold": 3, "sell": 0, "strongSell": 0}], RateLimited(60)],
        [],
        now_fn=lambda: clock[0],
    )
    service.load("MSFT")
    clock[0] += timedelta(hours=7)
    stale = service.load("MSFT")
    assert stale.provider_states["finnhub"] is AnalystState.STALE_CACHED
    assert "Total Analysts" in stale.values
    assert finnhub.calls == 2


def test_malformed_cache_fails_closed_and_migration_preserves_existing_rows(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO instruments(symbol, exchange, provider, currency) VALUES('AAPL','NASDAQ','yahoo','USD')")
        connection.execute(
            "INSERT INTO rs_analyst_cache VALUES(?,?,?,?,?,?,?,?)",
            ("finnhub", "AAPL", "recommendation_trends", "{bad", None, datetime.now(timezone.utc).isoformat(), (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "ok"),
        )
        connection.commit()
    credentials = InMemoryCredentialStore()
    service = AnalystService(path, credentials)
    assert service.cache.get("finnhub", "AAPL", "recommendation_trends") is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM instruments WHERE symbol='AAPL'").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
