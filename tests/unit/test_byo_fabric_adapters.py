from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.market_data.contracts import AssetClass, Capability, FabricProviderError, FabricRequest, FreshnessPolicy, RateLimited
from app.market_data.instruments import build_continuous_series, parse_futures_symbol
from app.market_data.providers.byo_free_tier import AlphaVantageAdapter, FredAdapter, LocalQuota, TwelveDataAdapter
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials


class Transport:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get_json(self, url, headers=None):
        self.urls.append(url)
        return self.response


def request(symbol="AAPL", capability=Capability.QUOTE, asset=AssetClass.EQUITY):
    return FabricRequest(
        f"{asset.value}:{symbol}", symbol, asset, capability,
        freshness=FreshnessPolicy(timedelta(days=3), allow_delayed=True, allow_end_of_day=True),
        request_id="byo-1",
    )


def store(provider_id, secret="SUPER_SECRET_API_KEY_123456"):
    value = InMemoryCredentialStore()
    value.save(ProviderCredentials(provider_id, {"api_key": secret}))
    return value


def test_twelve_data_quote_uses_user_key_and_returns_no_secret_in_result() -> None:
    secret = "TWELVE_SECRET_123456"
    transport = Transport({"close": "230.50", "volume": "100", "currency": "USD", "timestamp": 1787065200})
    adapter = TwelveDataAdapter(store("twelve_data", secret), transport, LocalQuota(2, 60))
    result = adapter.request(request())
    assert result.payload["price"] == "230.50"
    assert secret not in repr(result) and secret not in str(result)
    assert adapter.rate_limit_state().remaining == 1


def test_missing_credentials_and_local_quota_fail_truthfully() -> None:
    empty = InMemoryCredentialStore()
    adapter = TwelveDataAdapter(empty, Transport({}), LocalQuota(1, 60))
    with pytest.raises(FabricProviderError, match="credentials are required"):
        adapter.request(request())
    limited = TwelveDataAdapter(store("twelve_data"), Transport({"close": "1"}), LocalQuota(1, 60))
    limited.request(request())
    with pytest.raises(RateLimited):
        limited.request(request())


def test_alpha_vantage_is_low_frequency_end_of_day() -> None:
    transport = Transport({"Global Quote": {"05. price": "10", "06. volume": "20", "07. latest trading day": "2026-08-18"}})
    adapter = AlphaVantageAdapter(store("alpha_vantage"), transport, LocalQuota(2, 86400))
    result = adapter.request(request())
    assert result.payload["price"] == "10" and result.cache_ttl_seconds == 3600
    assert adapter.descriptor.minimum_request_interval_seconds >= 12


def test_fred_is_macro_only_and_never_enters_quote_pool() -> None:
    adapter = FredAdapter(store("fred"), Transport({"observations": [{"date": "2026-08-01", "value": "4.2"}]}))
    with pytest.raises(FabricProviderError, match="not a market quote"):
        adapter.request(request())
    result = adapter.request(request("CPIAUCSL", Capability.MACRO_SERIES, AssetClass.MACRO))
    assert result.payload["observations"][0]["value"] == "4.2"


def test_continuous_futures_discloses_contract_and_optional_roll_adjustment() -> None:
    march = parse_futures_symbol("ESH27", "CME", date(2027, 3, 19))
    june = parse_futures_symbol("ESM27", "CME", date(2027, 6, 18))
    raw = [(date(2027, 3, 14), march, Decimal("5000")), (date(2027, 3, 15), june, Decimal("5025"))]
    unadjusted = build_continuous_series(raw)
    adjusted = build_continuous_series(raw, adjustment="difference")
    assert unadjusted == [(date(2027, 3, 14), Decimal("5000"), "ESH27"), (date(2027, 3, 15), Decimal("5025"), "ESM27")]
    assert adjusted[-1] == (date(2027, 3, 15), Decimal("5000"), "ESM27")
    with pytest.raises(ValueError, match="Unsupported"):
        build_continuous_series(raw, adjustment="opaque")
