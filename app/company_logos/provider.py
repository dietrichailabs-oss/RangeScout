"""Logo.dev ticker-image client.

This client deliberately uses the documented stock-ticker image endpoint. It
never scrapes consumer pages, never logs the publishable token, and does not
persist returned third-party image bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, ContextManager, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import json
import ipaddress
import re


LOGO_DEV_PROVIDER_ID = "logo_dev"
FINNHUB_LOGO_PROVIDER_ID = "finnhub_profile"
TWELVE_DATA_LOGO_PROVIDER_ID = "twelve_data_logo"
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_METADATA_BYTES = 256 * 1024
_ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_TWELVE_DATA_LOGO_HOSTS = frozenset({"api.twelvedata.com", "logo.twelvedata.com"})
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,31}$")

# Logo.dev documents suffixes for global exchanges. Unknown exchange names are
# intentionally left unsuffixed rather than guessed.
_EXCHANGE_SUFFIXES = {
    "LSE": ".L",
    "LONDON": ".L",
    "LONDON STOCK EXCHANGE": ".L",
    "TSX": ".TO",
    "TORONTO": ".TO",
    "TSXV": ".V",
    "ASX": ".AX",
    "AUSTRALIAN SECURITIES EXCHANGE": ".AX",
    "TSE": ".T",
    "TOKYO": ".T",
    "TOKYO STOCK EXCHANGE": ".T",
    "HKEX": ".HK",
    "HONG KONG": ".HK",
    "EURONEXT AMSTERDAM": ".AS",
    "EURONEXT PARIS": ".PA",
    "BORSA ITALIANA": ".MI",
    "MILAN": ".MI",
    "SIX": ".SW",
    "SIX SWISS EXCHANGE": ".SW",
    "NASDAQ STOCKHOLM": ".ST",
}
_US_EXCHANGES = frozenset({"", "NASDAQ", "NYSE", "NYSEARCA", "ARCA", "AMEX", "BATS", "CBOE"})


class LogoProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class LogoFetchResponse:
    content: bytes
    content_type: str
    source_url: str | None = None
    lookup_identifier: str | None = None


class _Response(Protocol):
    headers: object
    status: int

    def read(self, amt: int = -1) -> bytes: ...


OpenFn = Callable[[Request, float], ContextManager[_Response]]


class LogoDevClient:
    provider_id = LOGO_DEV_PROVIDER_ID

    def __init__(self, *, timeout_seconds: float = 6.0, opener: OpenFn | None = None) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._opener = opener or _default_open

    @staticmethod
    def ticker_identifier(symbol: str, exchange: str | None = None) -> str:
        normalized = str(symbol).strip().upper()
        if not _SYMBOL_RE.fullmatch(normalized):
            raise ValueError("Invalid stock ticker for company-logo lookup.")
        normalized_exchange = str(exchange or "").strip().upper()
        if normalized_exchange in _US_EXCHANGES:
            return normalized
        suffix = _EXCHANGE_SUFFIXES.get(normalized_exchange)
        if suffix and not normalized.endswith(suffix):
            return f"{normalized}{suffix}"
        return normalized

    def build_url(self, symbol: str, exchange: str | None, publishable_key: str, *, theme: str) -> str:
        key = str(publishable_key).strip()
        if not key:
            raise ValueError("Logo.dev publishable key is required.")
        identifier = self.ticker_identifier(symbol, exchange)
        query = urlencode(
            {
                "token": key,
                "size": "96",
                "retina": "true",
                "format": "png",
                "theme": "light" if theme == "light" else "dark",
                "fallback": "404",
            }
        )
        return f"https://img.logo.dev/ticker/{quote(identifier, safe='.-')}?{query}"

    def fetch(self, symbol: str, exchange: str | None, publishable_key: str, *, theme: str = "dark") -> LogoFetchResponse:
        # Keep the token in the request only. Never include the URL or the source
        # exception text in errors because HTTP exceptions may echo query strings.
        url = self.build_url(symbol, exchange, publishable_key, theme=theme)
        request = Request(
            url,
            headers={
                "User-Agent": "RangeScout/CompanyLogo (+https://logo.dev)",
                "Accept": "image/png,image/webp,image/jpeg;q=0.9",
            },
            method="GET",
        )
        try:
            with self._opener(request, self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if status not in {200, 202}:
                    raise LogoProviderError("http_status", f"Logo provider returned HTTP {status}.")
                content_type = _content_type(response.headers)
                if content_type not in _ALLOWED_CONTENT_TYPES:
                    raise LogoProviderError("content_type", "Logo provider returned a non-image response.", retryable=False)
                content = response.read(MAX_LOGO_BYTES + 1)
                if not content:
                    raise LogoProviderError("empty", "Logo provider returned an empty image.")
                if len(content) > MAX_LOGO_BYTES:
                    raise LogoProviderError("too_large", "Logo image exceeded the local safety limit.", retryable=False)
                return LogoFetchResponse(
                    content=content,
                    content_type=content_type,
                    source_url="https://img.logo.dev/ticker/",
                    lookup_identifier=self.ticker_identifier(symbol, exchange),
                )
        except HTTPError as exc:
            if exc.code == 404:
                raise LogoProviderError("not_found", "No company logo is available for this ticker.", retryable=False) from None
            if exc.code == 429:
                raise LogoProviderError("rate_limited", "Company-logo provider rate limit reached.") from None
            if exc.code in {401, 403}:
                raise LogoProviderError("authentication", "Company-logo publishable key was rejected.", retryable=False) from None
            raise LogoProviderError("http_error", f"Company-logo provider returned HTTP {exc.code}.") from None
        except URLError:
            raise LogoProviderError("network", "Company-logo provider is temporarily unavailable.") from None
        except TimeoutError:
            raise LogoProviderError("timeout", "Company-logo provider timed out.") from None


def _content_type(headers: object) -> str:
    getter = getattr(headers, "get_content_type", None)
    if callable(getter):
        return str(getter()).lower()
    get = getattr(headers, "get", None)
    value = get("Content-Type", "") if callable(get) else ""
    return str(value).split(";", 1)[0].strip().lower()


def _default_open(request: Request, timeout: float):
    return urlopen(request, timeout=timeout)  # noqa: S310 - fixed trusted host assembled by client


class FinnhubProfileLogoClient:
    """Official Profile 2 lookup followed by a bounded session-only image fetch."""

    provider_id = FINNHUB_LOGO_PROVIDER_ID
    profile_url = "https://finnhub.io/api/v1/stock/profile2"

    def __init__(self, *, timeout_seconds: float = 6.0, opener: OpenFn | None = None) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._opener = opener or _default_open

    def fetch(self, symbol: str, exchange: str | None, api_key: str, *, theme: str = "dark") -> LogoFetchResponse:  # noqa: ARG002
        normalized = LogoDevClient.ticker_identifier(symbol, exchange)
        key = str(api_key).strip()
        if not key:
            raise ValueError("Finnhub API key is required.")
        url = self.profile_url + "?" + urlencode({"symbol": normalized, "token": key})
        request = Request(url, headers={"User-Agent": "RangeScout/CompanyLogo", "Accept": "application/json"})
        try:
            with self._opener(request, self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    raise LogoProviderError("http_status", f"Finnhub returned HTTP {status}.")
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise LogoProviderError("too_large", "Finnhub profile response exceeded the safety limit.", retryable=False)
            payload = json.loads(raw.decode("utf-8"))
            logo_url = str(payload.get("logo", "")).strip() if isinstance(payload, dict) else ""
            if not logo_url:
                raise LogoProviderError("not_found", "Finnhub profile did not provide a company logo.", retryable=False)
            _validate_remote_image_url(logo_url)
            image_request = Request(
                logo_url,
                headers={"User-Agent": "RangeScout/CompanyLogo", "Accept": "image/png,image/webp,image/jpeg;q=0.9"},
            )
            with self._opener(image_request, self.timeout_seconds) as response:
                content_type = _content_type(response.headers)
                if content_type not in _ALLOWED_CONTENT_TYPES:
                    raise LogoProviderError("content_type", "Finnhub logo URL returned a non-image response.", retryable=False)
                content = response.read(MAX_LOGO_BYTES + 1)
                if not content or len(content) > MAX_LOGO_BYTES:
                    raise LogoProviderError("too_large" if content else "empty", "Finnhub logo image was empty or too large.", retryable=False)
            return LogoFetchResponse(content, content_type, logo_url, normalized)
        except LogoProviderError:
            raise
        except HTTPError as exc:
            if exc.code == 404:
                raise LogoProviderError("not_found", "Finnhub company logo was not found.", retryable=False) from None
            if exc.code == 429:
                raise LogoProviderError("rate_limited", "Finnhub company-logo limit reached.") from None
            if exc.code in {401, 403}:
                raise LogoProviderError("authentication", "Finnhub API key was rejected.", retryable=False) from None
            raise LogoProviderError("http_error", f"Finnhub returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError):
            raise LogoProviderError("network", "Finnhub company-logo lookup is temporarily unavailable.") from None
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise LogoProviderError("invalid_response", "Finnhub returned an invalid company profile.", retryable=False) from None


class TwelveDataLogoClient:
    """Official two-stage Twelve Data `/logo` adapter; image bytes are session-only."""

    provider_id = TWELVE_DATA_LOGO_PROVIDER_ID
    logo_url = "https://api.twelvedata.com/logo"

    def __init__(self, *, timeout_seconds: float = 6.0, opener: OpenFn | None = None) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._opener = opener or _default_twelve_open

    def fetch(self, symbol: str, exchange: str | None, api_key: str, *, theme: str = "dark") -> LogoFetchResponse:  # noqa: ARG002
        normalized = str(symbol).strip().upper()
        if not _SYMBOL_RE.fullmatch(normalized):
            raise ValueError("Invalid stock ticker for Twelve Data logo lookup.")
        key = str(api_key).strip()
        if not key:
            raise ValueError("Twelve Data API key is required.")
        query: dict[str, str] = {"symbol": normalized, "apikey": key}
        normalized_exchange = str(exchange or "").strip().upper()
        if normalized_exchange:
            if len(normalized_exchange) == 4 and normalized_exchange.startswith("X"):
                query["mic_code"] = normalized_exchange
            else:
                query["exchange"] = normalized_exchange
        url = self.logo_url + "?" + urlencode(query)
        request = Request(
            url,
            headers={"User-Agent": "RangeScout/CompanyLogo", "Accept": "application/json"},
        )
        try:
            with self._opener(request, self.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if status != 200:
                    _raise_twelve_http(status)
                content_type = _content_type(response.headers)
                if content_type != "application/json":
                    raise LogoProviderError("content_type", "Twelve Data returned non-JSON logo metadata.", retryable=False)
                raw = response.read(MAX_LOGO_METADATA_BYTES + 1)
                if len(raw) > MAX_LOGO_METADATA_BYTES:
                    raise LogoProviderError("too_large", "Twelve Data logo metadata exceeded the safety limit.", retryable=False)
                if not raw:
                    raise LogoProviderError("empty", "Twelve Data returned empty logo metadata.", retryable=False)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise LogoProviderError("invalid_response", "Twelve Data returned malformed logo metadata.", retryable=False) from None
            if not isinstance(payload, dict):
                raise LogoProviderError("invalid_response", "Twelve Data returned invalid logo metadata.", retryable=False)
            if str(payload.get("status", "")).strip().lower() == "error" or (
                "code" in payload and not payload.get("url")
            ):
                _raise_twelve_provider_error(payload.get("code"))
            logo_url = str(payload.get("url", "")).strip()
            if not logo_url:
                raise LogoProviderError("not_found", "Twelve Data did not provide a stock logo URL.", retryable=False)
            _validate_twelve_logo_url(logo_url)
            image_request = Request(
                logo_url,
                headers={"User-Agent": "RangeScout/CompanyLogo", "Accept": "image/png,image/webp,image/jpeg;q=0.9"},
            )
            with self._opener(image_request, self.timeout_seconds) as response:
                image_status = int(getattr(response, "status", 200))
                if image_status != 200:
                    _raise_twelve_http(image_status)
                final_url_getter = getattr(response, "geturl", None)
                final_url = str(final_url_getter()).strip() if callable(final_url_getter) else logo_url
                _validate_twelve_logo_url(final_url)
                image_type = _content_type(response.headers)
                if image_type not in _ALLOWED_CONTENT_TYPES:
                    raise LogoProviderError("content_type", "Twelve Data logo URL returned a non-image response.", retryable=False)
                content = response.read(MAX_LOGO_BYTES + 1)
                if not content:
                    raise LogoProviderError("empty", "Twelve Data returned an empty logo image.", retryable=False)
                if len(content) > MAX_LOGO_BYTES:
                    raise LogoProviderError("too_large", "Twelve Data logo image exceeded the safety limit.", retryable=False)
                if not _valid_image_signature(content, image_type):
                    raise LogoProviderError("invalid_image", "Twelve Data returned invalid logo image bytes.", retryable=False)
            lookup = normalized + (f"@{normalized_exchange}" if normalized_exchange else "")
            return LogoFetchResponse(content, image_type, final_url, lookup)
        except LogoProviderError:
            raise
        except HTTPError as exc:
            _raise_twelve_http(exc.code)
        except (URLError, TimeoutError):
            raise LogoProviderError("network", "Twelve Data company-logo lookup is temporarily unavailable.") from None


def _validate_remote_image_url(value: str) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise LogoProviderError("unsafe_url", "Provider returned an unsafe company-logo URL.", retryable=False)
    host = parsed.hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise LogoProviderError("unsafe_url", "Provider returned a local company-logo URL.", retryable=False)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise LogoProviderError("unsafe_url", "Provider returned a non-public company-logo URL.", retryable=False)


def _validate_twelve_logo_url(value: str) -> None:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    host = str(parsed.hostname or "").strip("[]").lower()
    try:
        port = parsed.port
    except ValueError:
        raise LogoProviderError("unsafe_url", "Twelve Data returned an unapproved logo URL.", retryable=False) from None
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or host not in _TWELVE_DATA_LOGO_HOSTS
    ):
        raise LogoProviderError("unsafe_url", "Twelve Data returned an unapproved logo URL.", retryable=False)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise LogoProviderError("unsafe_url", "Twelve Data returned a non-public logo URL.", retryable=False)


class _TwelveRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_twelve_logo_url(str(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_twelve_open(request: Request, timeout: float):
    return build_opener(_TwelveRedirectHandler()).open(request, timeout=timeout)


def _raise_twelve_http(status: int) -> None:
    if status == 404:
        raise LogoProviderError("not_found", "Twelve Data company logo was not found.", retryable=False)
    if status == 429:
        raise LogoProviderError("rate_limited", "Twelve Data company-logo limit reached.")
    if status in {401, 403}:
        raise LogoProviderError("authentication", "Twelve Data API key was rejected.", retryable=False)
    raise LogoProviderError("http_error", f"Twelve Data returned HTTP {status}.")


def _raise_twelve_provider_error(code: object) -> None:
    try:
        status = int(str(code).strip())
    except (TypeError, ValueError):
        status = 0
    if status in {401, 403, 429, 404}:
        _raise_twelve_http(status)
    raise LogoProviderError("provider_error", "Twelve Data rejected the company-logo request.", retryable=False)


def _valid_image_signature(content: bytes, content_type: str) -> bool:
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return len(content) >= 4 and content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9")
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False
