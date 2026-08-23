"""Quota-aware user-key adapters for optional free-tier services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from urllib.parse import quote, urlencode

from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FabricProviderError,
    FabricRequest,
    FabricResult,
    ProviderDescriptor,
    ProviderTerms,
    RateLimited,
    RateLimitState,
)
from app.market_data.providers.http import JsonTransport
from app.security.credentials import CredentialStore


@dataclass
class _QuotaWindow:
    limit: int
    seconds: int
    started: float
    count: int = 0


class LocalQuota:
    """Thread-safe multi-window quota; the two-argument form remains supported."""

    def __init__(
        self,
        limit: int | None = None,
        window_seconds: int | None = None,
        *,
        windows: tuple[tuple[int, int], ...] | None = None,
    ) -> None:
        if windows is None:
            if limit is None or window_seconds is None:
                raise ValueError("A quota window is required.")
            windows = ((limit, window_seconds),)
        now = datetime.now(timezone.utc).timestamp()
        self._windows = [_QuotaWindow(value, seconds, now) for value, seconds in windows]
        if any(window.limit < 1 or window.seconds < 1 for window in self._windows):
            raise ValueError("Quota windows must be positive.")
        self.limit = self._windows[0].limit
        self.window_seconds = self._windows[0].seconds
        self._lock = RLock()

    def consume(self) -> None:
        with self._lock:
            now = datetime.now(timezone.utc).timestamp()
            retry_after = 0.0
            for window in self._windows:
                if now - window.started >= window.seconds:
                    window.started, window.count = now, 0
                if window.count >= window.limit:
                    retry_after = max(retry_after, window.seconds - (now - window.started))
            if retry_after:
                raise RateLimited(max(1.0, retry_after))
            for window in self._windows:
                window.count += 1

    def state(self) -> RateLimitState:
        with self._lock:
            now = datetime.now(timezone.utc).timestamp()
            remaining_values: list[int] = []
            retry_after = 0.0
            for window in self._windows:
                if now - window.started >= window.seconds:
                    window.started, window.count = now, 0
                remaining_values.append(max(0, window.limit - window.count))
                if window.count >= window.limit:
                    retry_after = max(retry_after, window.seconds - (now - window.started))
            limited = any(value == 0 for value in remaining_values)
            return RateLimitState(
                limited=limited,
                retry_after_seconds=max(1.0, retry_after) if limited else None,
                remaining=min(remaining_values),
                reset_at=datetime.now(timezone.utc) + timedelta(seconds=retry_after) if limited else None,
            )


class ByoFreeTierAdapter:
    descriptor: ProviderDescriptor
    credential_id: str

    def __init__(self, credential_store: CredentialStore, transport: JsonTransport | None = None, quota: LocalQuota | None = None) -> None:
        self.credential_store = credential_store
        self.transport = transport or JsonTransport()
        self.quota = quota or self.default_quota()

    def default_quota(self) -> LocalQuota:
        return LocalQuota(5, 60)

    def _key(self) -> str:
        credentials = self.credential_store.load(self.credential_id)
        if credentials is None:
            raise FabricProviderError("Provider credentials are required.")
        return credentials.values["api_key"]

    def normalize_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized or len(normalized) > 64:
            raise ValueError("Invalid provider symbol.")
        return normalized

    def provider_symbol_for(self, request: FabricRequest) -> str:
        return self.normalize_symbol(request.canonical_symbol)

    def rate_limit_state(self) -> RateLimitState:
        return self.quota.state()

    def health_check(self) -> bool:
        try:
            return bool(self._key())
        except Exception:
            return False

    def list_instruments(self) -> list[dict[str, object]]:
        return []

    def _result(self, request: FabricRequest, payload, provider_timestamp: datetime, currency="USD", ttl=60):
        received = datetime.now(timezone.utc)
        return FabricResult(
            request.request_id,
            self.descriptor.provider_id,
            self.provider_symbol_for(request),
            request.canonical_instrument_id,
            request.canonical_symbol,
            request.capability,
            provider_timestamp,
            received,
            self.descriptor.delay_class,
            currency,
            request.venue,
            payload,
            self.descriptor.terms.attribution,
            ttl,
        )


class TwelveDataAdapter(ByoFreeTierAdapter):
    credential_id = "twelve_data"
    descriptor = ProviderDescriptor(
        "twelve_data", "Twelve Data",
        frozenset({AssetClass.EQUITY, AssetClass.ETF, AssetClass.FX, AssetClass.CRYPTO_SPOT, AssetClass.COMMODITY_SPOT}),
        frozenset({Capability.QUOTE, Capability.HISTORICAL, Capability.CANDLES, Capability.SYMBOL_SEARCH}),
        True, CredentialKind.API_KEY, DelayClass.DELAYED,
        ProviderTerms("https://twelvedata.com/docs", "2026-08-22", "Official BYO-key API including symbol search and documented precious-metal spot pairs.", attribution="Twelve Data", caching="Bounded by endpoint freshness.", redistribution="User plan terms apply; Basic is personal/internal non-display use.", decision="byo_enabled", reason="Official APIs document /symbol_search and XAU/USD quote/time-series coverage; local Basic-plan quota windows are enforced."),
        enabled=True, max_concurrency=1, minimum_request_interval_seconds=7.5,
    )

    def default_quota(self) -> LocalQuota:
        # Official Basic plan reviewed 2026-08-19: 8 credits/minute and 800/day.
        return LocalQuota(windows=((8, 60), (800, 86400)))

    def search_instruments(self, query: str, limit: int = 20) -> list[dict[str, object]]:
        """Use Twelve Data's documented symbol-search endpoint with the user's key."""

        normalized = str(query or "").strip()
        if len(normalized) < 2 or len(normalized) > 120:
            return []
        self.quota.consume()
        data = self.transport.get_json(
            "https://api.twelvedata.com/symbol_search?" + urlencode({
                "symbol": normalized, "outputsize": str(max(1, min(50, int(limit)))),
                "show_plan": "true", "apikey": self._key(),
            })
        )
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise FabricProviderError("Twelve Data symbol discovery is unavailable.")
        verified = datetime.now(timezone.utc).isoformat()
        results: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            instrument_type = str(row.get("instrument_type") or "").strip()
            if not symbol:
                continue
            results.append({
                "canonical_symbol": symbol,
                "provider_symbol": symbol,
                "name": str(row.get("instrument_name") or symbol).strip(),
                "venue": str(row.get("exchange") or row.get("mic_code") or "").strip().upper(),
                "currency": str(row.get("currency") or "USD").strip().upper(),
                "instrument_type": instrument_type,
                "subtype": instrument_type.lower().replace("-", "_").replace(" ", "_"),
                "asset_class": _twelve_asset_class(instrument_type, symbol),
                "verified_at_utc": verified,
            })
        return results

    def request(self, request: FabricRequest) -> FabricResult:
        self.quota.consume()
        key = self._key()
        symbol = self.provider_symbol_for(request)
        if request.capability == Capability.QUOTE:
            url = "https://api.twelvedata.com/quote?" + urlencode({"symbol": symbol, "apikey": key})
            data = (
                self.transport.get_json(url, timeout_seconds=2.5)
                if type(self.transport) is JsonTransport
                else self.transport.get_json(url)
            )
            if not isinstance(data, dict) or data.get("status") == "error" or not data.get("close"):
                raise FabricProviderError("Twelve Data quote is unavailable for this symbol or plan.")
            timestamp = datetime.fromtimestamp(int(data.get("timestamp", datetime.now(timezone.utc).timestamp())), timezone.utc)
            return self._result(request, {"price": str(data["close"]), "volume": data.get("volume")}, timestamp, str(data.get("currency", "USD")))
        if request.capability in {Capability.HISTORICAL, Capability.CANDLES}:
            data = self.transport.get_json("https://api.twelvedata.com/time_series?" + urlencode({"symbol": symbol, "interval": request.interval or "1day", "outputsize": "500", "apikey": key}))
            values = data.get("values") if isinstance(data, dict) else None
            if not isinstance(values, list):
                raise FabricProviderError("Twelve Data history is unavailable for this symbol or plan.")
            return self._result(request, {"bars": values}, _newest_twelve_timestamp(values), ttl=300)
        raise FabricProviderError("Unsupported Twelve Data capability.")


def _twelve_asset_class(instrument_type: str, symbol: str) -> str:
    kind = instrument_type.strip().lower().replace("-", " ")
    mapping = {
        "common stock": "equity", "preferred stock": "preferred", "closed end fund": "closed_end_fund",
        "etf": "etf", "exchange traded fund": "etf", "exchange traded note": "etn",
        "physical currency": "fx", "digital currency": "crypto_spot",
        "commodity": "commodity_spot", "precious metal": "commodity_spot",
        "index": "index", "future": "future", "mutual fund": "mutual_fund",
    }
    if kind in mapping:
        return mapping[kind]
    normalized = symbol.upper().replace("-", "/")
    if normalized.startswith(("XAU/", "XAG/", "XPT/", "XPD/")):
        return "commodity_spot"
    return "unknown"


class AlphaVantageAdapter(ByoFreeTierAdapter):
    credential_id = "alpha_vantage"
    descriptor = ProviderDescriptor(
        "alpha_vantage", "Alpha Vantage",
        frozenset({AssetClass.EQUITY, AssetClass.ETF, AssetClass.FX, AssetClass.CRYPTO_SPOT}),
        frozenset({Capability.QUOTE, Capability.HISTORICAL}),
        True, CredentialKind.API_KEY, DelayClass.END_OF_DAY,
        ProviderTerms("https://www.alphavantage.co/support/", "2026-08-19", "Official BYO-key API.", attribution="Alpha Vantage", caching="Long low-frequency TTL.", redistribution="Terms and user plan apply.", decision="byo_enabled", reason="Official free allowance is 25 requests/day; never a high-frequency default."),
        enabled=True, max_concurrency=1, minimum_request_interval_seconds=12.0,
    )

    def __init__(self, credential_store: CredentialStore, transport: JsonTransport | None = None, quota: LocalQuota | None = None) -> None:
        super().__init__(credential_store, transport, quota or LocalQuota(25, 86400))

    def request(self, request: FabricRequest) -> FabricResult:
        self.quota.consume()
        key, symbol = self._key(), self.provider_symbol_for(request)
        function = "GLOBAL_QUOTE" if request.capability == Capability.QUOTE else "TIME_SERIES_DAILY"
        url = "https://www.alphavantage.co/query?" + urlencode({"function": function, "symbol": symbol, "apikey": key})
        data = (
            self.transport.get_json(url, timeout_seconds=2.5)
            if request.capability == Capability.QUOTE and type(self.transport) is JsonTransport
            else self.transport.get_json(url)
        )
        if not isinstance(data, dict) or "Note" in data or "Information" in data:
            raise RateLimited(86400)
        if request.capability == Capability.QUOTE:
            quote_data = data.get("Global Quote")
            if not isinstance(quote_data, dict) or not quote_data.get("05. price"):
                raise FabricProviderError("Alpha Vantage quote is unavailable.")
            day = quote_data.get("07. latest trading day")
            timestamp = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) if day else datetime.now(timezone.utc)
            return self._result(request, {"price": quote_data["05. price"], "volume": quote_data.get("06. volume")}, timestamp, ttl=3600)
        series = data.get("Time Series (Daily)")
        if not isinstance(series, dict):
            raise FabricProviderError("Alpha Vantage history is unavailable.")
        return self._result(request, {"bars": series}, _newest_mapping_date(series), ttl=86400)


class FredAdapter(ByoFreeTierAdapter):
    credential_id = "fred"
    descriptor = ProviderDescriptor(
        "fred", "FRED",
        frozenset({AssetClass.MACRO}),
        frozenset({Capability.MACRO_SERIES}),
        True, CredentialKind.API_KEY, DelayClass.REFERENCE,
        ProviderTerms("https://fred.stlouisfed.org/docs/api/fred/", "2026-08-19", "Official BYO-key FRED API.", attribution="Federal Reserve Bank of St. Louis (FRED)", caching="Series-appropriate bounded TTL.", redistribution="FRED Terms of Use apply.", decision="byo_enabled", reason="Macro context only; never participates in quote racing."),
        enabled=True, max_concurrency=1, minimum_request_interval_seconds=1.0,
    )

    def request(self, request: FabricRequest) -> FabricResult:
        if request.capability != Capability.MACRO_SERIES:
            raise FabricProviderError("FRED is a macro-series provider, not a market quote provider.")
        self.quota.consume()
        data = self.transport.get_json("https://api.stlouisfed.org/fred/series/observations?" + urlencode({"series_id": self.provider_symbol_for(request), "api_key": self._key(), "file_type": "json"}))
        observations = data.get("observations") if isinstance(data, dict) else None
        if not isinstance(observations, list):
            raise FabricProviderError("FRED series is unavailable.")
        return self._result(request, {"observations": observations}, _newest_observation_date(observations), ttl=86400)


def _newest_twelve_timestamp(values: list[object]) -> datetime:
    candidates: list[datetime] = []
    for row in values:
        if not isinstance(row, dict):
            continue
        raw = row.get("datetime")
        if not isinstance(raw, str):
            continue
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        candidates.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if not candidates:
        raise FabricProviderError("Twelve Data history has no source timestamps.")
    return max(candidates)


def _newest_mapping_date(series: dict[object, object]) -> datetime:
    candidates: list[datetime] = []
    for raw in series:
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        candidates.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if not candidates:
        raise FabricProviderError("Alpha Vantage history has no source timestamps.")
    return max(candidates)


def _newest_observation_date(observations: list[object]) -> datetime:
    values = [row.get("date") for row in observations if isinstance(row, dict)]
    parsed: list[datetime] = []
    for raw in values:
        try:
            value = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        parsed.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    if not parsed:
        raise FabricProviderError("FRED observations have no source timestamps.")
    return max(parsed)
