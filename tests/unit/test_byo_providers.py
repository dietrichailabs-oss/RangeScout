from __future__ import annotations

import io
import traceback
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.schemas import AdjustmentMode, InstrumentIdentifier
from app.application.bootstrap import RangeScoutApplication
from app.configuration.settings import AppSettings
from app.providers.base import ProviderUnavailable
from app.providers.byo_provider import AlpacaProvider, FinnhubProvider
from app.providers.configuration import ProviderConfigurationService
from app.providers.registry import default_provider_registry
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials


class _Response:
    def __init__(self, payload: str) -> None:
        self._stream = io.BytesIO(payload.encode("utf-8"))

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._stream.read()


def _configured_store() -> InMemoryCredentialStore:
    store = InMemoryCredentialStore()
    store.save(ProviderCredentials("finnhub", {"api_key": "FINNHUB_SECRET_123456789"}))
    store.save(
        ProviderCredentials(
            "alpaca",
            {"key_id": "ALPACA_KEY_ID_123456789", "secret_key": "ALPACA_SECRET_123456789"},
        )
    )
    return store


def test_registry_contains_all_m1_providers_in_stable_order() -> None:
    store = InMemoryCredentialStore()
    registry = default_provider_registry(credential_store=store)
    assert registry.list_available() == ["yahoo", "finnhub"]
    assert registry.get("yahoo").provider_id == "yahoo"


def test_configuration_service_reports_and_deletes_without_secret_echo() -> None:
    store = InMemoryCredentialStore()
    service = ProviderConfigurationService(default_provider_registry(credential_store=store), store)
    assert service.status("yahoo").configured is True
    assert service.status("finnhub").configuration_text == "Credentials required"

    secret = "FINNHUB_SECRET_123456789"
    service.save_credentials("finnhub", {"api_key": secret})
    status = service.status("finnhub")
    assert status.configured is True
    assert secret not in repr(status)
    assert service.delete_credentials("finnhub") is True
    assert service.status("finnhub").configured is False


def test_missing_credentials_fail_explicitly_without_fallback() -> None:
    store = InMemoryCredentialStore()
    registry = default_provider_registry(credential_store=store)
    provider = registry.get("finnhub")
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ProviderUnavailable, match="credentials are required"):
            provider.fetch_quote("AAPL")
    urlopen.assert_not_called()
    assert provider.provider_id == "finnhub"


def test_application_migrates_deferred_alpaca_provider_to_yahoo(tmp_path) -> None:
    store = InMemoryCredentialStore()
    settings = AppSettings(default_provider="alpaca")
    app = RangeScoutApplication(data_dir=tmp_path, settings=settings, credential_store=store)
    try:
        assert app.provider_id == "yahoo"
        assert app.provider.provider_id == "yahoo"
    finally:
        app.store.close()


def test_finnhub_quote_uses_header_not_secret_url() -> None:
    store = _configured_store()
    provider = FinnhubProvider(store.load)
    with patch(
        "urllib.request.urlopen",
        return_value=_Response('{"c":261.74,"pc":259.45,"t":1786970000}'),
    ) as urlopen:
        result = provider.fetch_quote("AAPL")
    request = urlopen.call_args.args[0]
    assert "FINNHUB_SECRET" not in request.full_url
    assert request.get_header("X-finnhub-token") == "FINNHUB_SECRET_123456789"
    assert result.payload.last == Decimal("261.74")
    assert result.payload.provider_timestamp == datetime.fromtimestamp(1786970000, tz=timezone.utc)
    with pytest.raises(ProviderUnavailable, match="historical-candle entitlement"):
        provider.fetch_historical(InstrumentIdentifier("AAPL"))


def test_provider_http_failure_trace_does_not_contain_credentials() -> None:
    store = _configured_store()
    provider = FinnhubProvider(store.load)
    error = urllib.error.HTTPError("https://finnhub.io/api/v1/quote?symbol=AAPL", 401, "no", {}, None)
    with patch("urllib.request.urlopen", side_effect=error):
        try:
            provider.fetch_quote("AAPL")
        except ProviderUnavailable as exc:
            trace = traceback.format_exc()
            assert "FINNHUB_SECRET_123456789" not in str(exc)
            assert "FINNHUB_SECRET_123456789" not in trace
        else:  # pragma: no cover
            raise AssertionError("expected provider failure")


def test_alpaca_quote_and_history_use_byo_headers_and_iex() -> None:
    store = _configured_store()
    provider = AlpacaProvider(store.load)
    snapshot = (
        '{"latestTrade":{"p":190.25,"t":"2026-08-17T13:40:01.123456Z"},'
        '"dailyBar":{"v":1234},"prevDailyBar":{"c":188.50}}'
    )
    bars = (
        '{"bars":[{"t":"2026-08-15T04:00:00Z","o":188,"h":191,"l":187,'
        '"c":190,"v":1000}],"next_page_token":null}'
    )
    with patch("urllib.request.urlopen", side_effect=[_Response(snapshot), _Response(bars)]) as urlopen:
        quote = provider.fetch_quote("AAPL")
        historical = provider.fetch_historical(InstrumentIdentifier("AAPL"))
    requests = [call.args[0] for call in urlopen.call_args_list]
    for request in requests:
        assert request.get_header("Apca-api-key-id") == "ALPACA_KEY_ID_123456789"
        assert request.get_header("Apca-api-secret-key") == "ALPACA_SECRET_123456789"
        assert "ALPACA_" not in request.full_url
        assert "feed=iex" in request.full_url
    assert quote.payload.last == Decimal("190.25")
    assert quote.payload.previous_close == Decimal("188.5")
    assert historical.payload[0][0].provider == "alpaca"
    with pytest.raises(ProviderUnavailable, match="adjusted historical"):
        provider.fetch_historical(InstrumentIdentifier("AAPL"), adjusted=AdjustmentMode.ADJUSTED)
