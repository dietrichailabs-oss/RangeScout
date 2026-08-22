from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

# Initialize the existing production composition package before importing its
# nested logo package, matching the supported application/test entry path.
from app.application.bootstrap import RangeScoutApplication as _Application  # noqa: F401
from app.company_logos.provider import (
    MAX_LOGO_BYTES,
    MAX_LOGO_METADATA_BYTES,
    LogoProviderError,
    TwelveDataLogoClient,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"deterministic-image"


class Headers:
    def __init__(self, content_type: str) -> None:
        self.content_type = content_type

    def get_content_type(self) -> str:
        return self.content_type


class Response:
    def __init__(
        self,
        payload: bytes,
        *,
        content_type: str,
        status: int = 200,
        final_url: str | None = None,
    ) -> None:
        self.payload = payload
        self.headers = Headers(content_type)
        self.status = status
        self.final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self.payload if amount < 0 else self.payload[:amount]

    def geturl(self) -> str:
        return self.final_url or "https://api.twelvedata.com/logo/apple.com"


def metadata(payload: object, **kwargs) -> Response:
    return Response(json.dumps(payload).encode(), content_type="application/json", **kwargs)


def test_official_stock_json_contract_uses_canonical_symbol_exchange_then_fetches_image() -> None:
    calls = []

    def opener(request, timeout):  # noqa: ARG001
        calls.append(request)
        if len(calls) == 1:
            return metadata({"meta": {"symbol": "BRK.B"}, "url": "https://api.twelvedata.com/logo/berkshirehathaway.com"})
        return Response(
            PNG,
            content_type="image/png",
            final_url="https://api.twelvedata.com/logo/berkshirehathaway.com",
        )

    result = TwelveDataLogoClient(opener=opener).fetch("brk.b", "NYSE", "TD_SECRET")
    query = parse_qs(urlsplit(calls[0].full_url).query)
    assert query == {"symbol": ["BRK.B"], "exchange": ["NYSE"], "apikey": ["TD_SECRET"]}
    assert calls[0].headers["Accept"] == "application/json"
    assert calls[1].full_url == "https://api.twelvedata.com/logo/berkshirehathaway.com"
    assert result.content == PNG
    assert result.content_type == "image/png"
    assert result.source_url == calls[1].full_url
    assert result.lookup_identifier == "BRK.B@NYSE"


def test_mic_identity_is_sent_without_logo_dev_suffix_rules() -> None:
    calls = []

    def opener(request, timeout):  # noqa: ARG001
        calls.append(request.full_url)
        if len(calls) == 1:
            return metadata({"url": "https://logo.twelvedata.com/stocks/bp.png"})
        return Response(PNG, content_type="image/png", final_url="https://logo.twelvedata.com/stocks/bp.png")

    result = TwelveDataLogoClient(opener=opener).fetch("BP", "XLON", "KEY")
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["symbol"] == ["BP"]
    assert query["mic_code"] == ["XLON"]
    assert "exchange" not in query
    assert result.lookup_identifier == "BP@XLON"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        pytest.param(metadata({"meta": {"symbol": "AAPL"}}), "not_found", id="missing-url"),
        pytest.param(metadata({"url": "   "}), "not_found", id="blank-url"),
        pytest.param(Response(b"{not-json", content_type="application/json"), "invalid_response", id="malformed-json"),
        pytest.param(metadata({"status": "error", "code": 401, "message": "bad key"}), "authentication", id="json-auth-error"),
        pytest.param(metadata({"status": "error", "code": 429, "message": "quota"}), "rate_limited", id="json-rate-limit"),
        pytest.param(Response(b"{}", content_type="text/html"), "content_type", id="wrong-metadata-type"),
        pytest.param(Response(b"x" * (MAX_LOGO_METADATA_BYTES + 1), content_type="application/json"), "too_large", id="oversized-json"),
    ],
)
def test_metadata_failures_are_truthful_and_bounded(response: Response, expected_code: str) -> None:
    with pytest.raises(LogoProviderError) as caught:
        TwelveDataLogoClient(opener=lambda request, timeout: response).fetch("AAPL", "NASDAQ", "PRIVATE_KEY")
    assert caught.value.code == expected_code
    assert "PRIVATE_KEY" not in str(caught.value)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://api.twelvedata.com/logo/apple.com",
        "https://user:pass@api.twelvedata.com/logo/apple.com",
        "https://localhost/logo/apple.com",
        "https://127.0.0.1/logo/apple.com",
        "https://api.twelvedata.com.evil.example/logo/apple.com",
        "https://example.com/logo/apple.com",
    ],
)
def test_metadata_rejects_unsafe_or_non_twelve_logo_urls(unsafe_url: str) -> None:
    with pytest.raises(LogoProviderError) as caught:
        TwelveDataLogoClient(opener=lambda request, timeout: metadata({"url": unsafe_url})).fetch(
            "AAPL", "NASDAQ", "PRIVATE_KEY"
        )
    assert caught.value.code == "unsafe_url"
    assert "PRIVATE_KEY" not in str(caught.value)


def test_redirect_escape_is_rejected_after_image_open() -> None:
    responses = iter(
        [
            metadata({"url": "https://api.twelvedata.com/logo/apple.com"}),
            Response(PNG, content_type="image/png", final_url="https://evil.example/redirect.png"),
        ]
    )
    with pytest.raises(LogoProviderError) as caught:
        TwelveDataLogoClient(opener=lambda request, timeout: next(responses)).fetch("AAPL", "NASDAQ", "KEY")
    assert caught.value.code == "unsafe_url"


@pytest.mark.parametrize(
    ("image", "content_type", "expected_code"),
    [
        pytest.param(b"<html>no</html>", "text/html", "content_type", id="wrong-image-type"),
        pytest.param(b"not-a-png", "image/png", "invalid_image", id="invalid-signature"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_LOGO_BYTES, "image/png", "too_large", id="oversized-image"),
        pytest.param(b"", "image/png", "empty", id="empty-image"),
    ],
)
def test_image_stage_rejects_wrong_type_signature_size_and_empty(
    image: bytes, content_type: str, expected_code: str
) -> None:
    responses = iter(
        [
            metadata({"url": "https://api.twelvedata.com/logo/apple.com"}),
            Response(image, content_type=content_type),
        ]
    )
    with pytest.raises(LogoProviderError) as caught:
        TwelveDataLogoClient(opener=lambda request, timeout: next(responses)).fetch("AAPL", "NASDAQ", "KEY")
    assert caught.value.code == expected_code


@pytest.mark.parametrize(("status", "code"), [(401, "authentication"), (403, "authentication"), (429, "rate_limited")])
def test_http_auth_and_rate_limit_errors_are_sanitized(status: int, code: str) -> None:
    secret = "TD_NEVER_LEAK"

    def opener(request, timeout):  # noqa: ARG001
        raise HTTPError(request.full_url, status, "provider failure", {}, None)

    with pytest.raises(LogoProviderError) as caught:
        TwelveDataLogoClient(opener=opener).fetch("AAPL", "NASDAQ", secret)
    assert caught.value.code == code
    assert secret not in str(caught.value)
