"""Credential-aware REST adapters for BYO free-provider accounts.

Streaming is implemented by the separate ``app.streaming`` subsystem. These
adapters retain the historical/snapshot request surface without polling loops.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from string import ascii_uppercase, digits
from typing import Any, Callable, Mapping

from app.domain.errors import DataQualityError, ValidationError
from app.models.schemas import (
    AdjustmentMode,
    AssetType,
    DataDelay,
    Instrument,
    InstrumentIdentifier,
    OhlcvBar,
    ProviderMetadata,
    QuoteSnapshot,
)
from app.providers.base import ProviderCapability, ProviderUnavailable, ProviderResult
from app.security.credentials import ProviderCredentials


CredentialLoader = Callable[[str], ProviderCredentials | None]
_ALLOWED_SYMBOL_CHARS = frozenset(ascii_uppercase + digits + ".-")


class _CredentialedProvider:
    provider_id = ""
    provider_name = ""

    def __init__(self, credential_loader: CredentialLoader, timeout_seconds: float = 12.0) -> None:
        self._credential_loader = credential_loader
        self.timeout_seconds = timeout_seconds
        self.quote_timeout_seconds = min(2.5, timeout_seconds)

    @classmethod
    def normalize_symbol(cls, raw: str) -> str:
        if not isinstance(raw, str):
            raise ValidationError("Symbol must be a string.")
        normalized = raw.strip().upper()
        if not normalized:
            raise ValidationError("Symbol is required.")
        if len(normalized) > 16 or not all(char in _ALLOWED_SYMBOL_CHARS for char in normalized):
            raise ValidationError("Symbol format is invalid.")
        if normalized.startswith((".", "-")) or normalized.endswith((".", "-")):
            raise ValidationError("Symbol format is invalid.")
        return normalized

    def resolve_instrument(self, symbol: str) -> Instrument:
        normalized = self.normalize_symbol(symbol)
        return Instrument(
            identifier=InstrumentIdentifier(symbol=normalized),
            name=normalized,
            asset_type=AssetType.STOCK,
            currency="USD",
            provider=self.provider_id,
        )

    def _credentials(self) -> ProviderCredentials:
        credentials = self._credential_loader(self.provider_id)
        if credentials is None:
            raise ProviderUnavailable(
                f"{self.provider_name} credentials are required. Configure them in Settings > Market Data Providers."
            )
        return credentials

    def _query_json(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "RangeScout/1.3", **dict(headers)},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"{self.provider_name} rejected the request (HTTP {exc.code}). Check credentials and provider access."
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise ProviderUnavailable(
                f"{self.provider_name} is unavailable. Check the network connection and try again."
            ) from None
        except Exception:
            raise ProviderUnavailable(f"{self.provider_name} request failed safely.") from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderUnavailable(f"{self.provider_name} returned an invalid response.") from None
        if not isinstance(payload, dict):
            raise ProviderUnavailable(f"{self.provider_name} returned an unexpected response.")
        return payload

    def _query_json_list(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "RangeScout/1.6.3", **dict(headers)},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"{self.provider_name} rejected the request (HTTP {exc.code}). Check credentials and provider access."
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise ProviderUnavailable(
                f"{self.provider_name} is unavailable. Check the network connection and try again."
            ) from None
        except Exception:
            raise ProviderUnavailable(f"{self.provider_name} request failed safely.") from None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise ProviderUnavailable(f"{self.provider_name} returned an invalid response.") from None
        if not isinstance(payload, list):
            raise ProviderUnavailable(f"{self.provider_name} returned an unexpected response.")
        return [row for row in payload if isinstance(row, dict)]

    def fetch_actions(self, identifier: InstrumentIdentifier) -> ProviderResult:  # noqa: ARG002
        raise ProviderUnavailable(f"{self.provider_name} corporate-action retrieval is not supported in M1.")


class FinnhubProvider(_CredentialedProvider):
    provider_id = "finnhub"
    provider_name = "Finnhub"
    capabilities = ProviderCapability(
        can_lookup_symbol=True,
        can_fetch_quote=True,
        can_fetch_historical=False,
        can_fetch_actions=False,
        delay=DataDelay.REALTIME,
        supports_realtime=True,
        supports_adjusted=False,
        supports_indices=False,
        supports_etf=True,
        supports_stock=True,
    )

    def fetch_quote(self, symbol: str) -> ProviderResult:
        instrument = self.resolve_instrument(symbol)
        credentials = self._credentials()
        query = urllib.parse.urlencode({"symbol": instrument.identifier.symbol})
        payload = self._query_json(
            f"https://finnhub.io/api/v1/quote?{query}",
            {"X-Finnhub-Token": credentials.values["api_key"]},
            timeout_seconds=self.quote_timeout_seconds,
        )
        current = payload.get("c")
        if current is None or Decimal(str(current)) <= 0:
            raise DataQualityError(f"Finnhub returned no valid quote for {instrument.identifier.symbol}.")
        provider_timestamp = _unix_timestamp(payload.get("t"))
        now = datetime.now(timezone.utc)
        quote = QuoteSnapshot(
            instrument=instrument,
            last=Decimal(str(current)),
            previous_close=_optional_decimal(payload.get("pc")),
            volume=None,
            timestamp=now,
            provider_timestamp=provider_timestamp,
            delay_label=DataDelay.DELAYED,
            delay_seconds=None,
            day_low=_optional_decimal(payload.get("l")),
            day_high=_optional_decimal(payload.get("h")),
        )
        return ProviderResult("quote", quote, now, self.capabilities_report())

    def fetch_historical(
        self,
        identifier: InstrumentIdentifier,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResult:
        del identifier, start, end, adjusted
        raise ProviderUnavailable(
            "Finnhub free credentials do not provide the historical-candle entitlement used by this view. "
            "Select Yahoo for historical analysis."
        )

    def fetch_company_news(self, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return official Finnhub company-news records without retaining article bodies."""
        instrument = self.resolve_instrument(symbol)
        credentials = self._credentials()
        query = urllib.parse.urlencode(
            {
                "symbol": instrument.identifier.symbol,
                "from": start.date().isoformat(),
                "to": end.date().isoformat(),
            }
        )
        return self._query_json_list(
            f"https://finnhub.io/api/v1/company-news?{query}",
            {"X-Finnhub-Token": credentials.values["api_key"]},
        )

    def capabilities_report(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            supports_real_time=True,
            supports_adjusted=False,
            delay_label=DataDelay.REALTIME,
            capabilities={
                "authentication": "BYO user API key in Windows Credential Manager",
                "quote_transport": "authenticated REST snapshot",
                "historical_free_tier": False,
                "streaming": "BYO-key WebSocket trade stream; one connection per API key",
                "network_required": True,
            },
        )


class AlpacaProvider(_CredentialedProvider):
    provider_id = "alpaca"
    provider_name = "Alpaca"
    capabilities = ProviderCapability(
        can_lookup_symbol=True,
        can_fetch_quote=True,
        can_fetch_historical=True,
        can_fetch_actions=False,
        delay=DataDelay.REALTIME,
        supports_realtime=True,
        supports_adjusted=False,
        supports_indices=False,
        supports_etf=True,
        supports_stock=True,
    )

    def _headers(self) -> dict[str, str]:
        credentials = self._credentials()
        return {
            "APCA-API-KEY-ID": credentials.values["key_id"],
            "APCA-API-SECRET-KEY": credentials.values["secret_key"],
        }

    def fetch_quote(self, symbol: str) -> ProviderResult:
        instrument = self.resolve_instrument(symbol)
        safe_symbol = urllib.parse.quote(instrument.identifier.symbol, safe=".-")
        payload = self._query_json(
            f"https://data.alpaca.markets/v2/stocks/{safe_symbol}/snapshot?feed=iex",
            self._headers(),
        )
        trade = payload.get("latestTrade")
        if not isinstance(trade, dict) or trade.get("p") is None:
            raise DataQualityError(f"Alpaca returned no valid quote for {instrument.identifier.symbol}.")
        previous_bar = payload.get("prevDailyBar") if isinstance(payload.get("prevDailyBar"), dict) else {}
        daily_bar = payload.get("dailyBar") if isinstance(payload.get("dailyBar"), dict) else {}
        now = datetime.now(timezone.utc)
        quote = QuoteSnapshot(
            instrument=instrument,
            last=Decimal(str(trade["p"])),
            previous_close=_optional_decimal(previous_bar.get("c")),
            volume=int(daily_bar["v"]) if daily_bar.get("v") is not None else None,
            timestamp=now,
            provider_timestamp=_rfc3339_timestamp(trade.get("t")),
            delay_label=DataDelay.REALTIME,
            delay_seconds=None,
        )
        return ProviderResult("quote", quote, now, self.capabilities_report())

    def fetch_historical(
        self,
        identifier: InstrumentIdentifier,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResult:
        symbol = self.normalize_symbol(identifier.symbol)
        if adjusted != AdjustmentMode.RAW:
            raise ProviderUnavailable("Alpaca adjusted historical bars are not supported in M1.")
        safe_symbol = urllib.parse.quote(symbol, safe=".-")
        end_value = end or datetime.now(timezone.utc)
        start_value = start or end_value - timedelta(days=365)
        query = urllib.parse.urlencode(
            {
                "adjustment": "raw",
                "end": _as_utc_rfc3339(end_value),
                "feed": "iex",
                "limit": 10000,
                "sort": "asc",
                "start": _as_utc_rfc3339(start_value),
                "timeframe": "1Day",
            }
        )
        payload = self._query_json(
            f"https://data.alpaca.markets/v2/stocks/{safe_symbol}/bars?{query}",
            self._headers(),
        )
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise DataQualityError(f"Alpaca returned no historical bars for {symbol}.")
        bars: list[OhlcvBar] = []
        for raw in raw_bars:
            if not isinstance(raw, dict):
                continue
            try:
                timestamp = _rfc3339_timestamp(raw.get("t"))
                if timestamp is None:
                    continue
                bars.append(
                    OhlcvBar(
                        instrument=InstrumentIdentifier(symbol=symbol),
                        date=timestamp.date(),
                        open=Decimal(str(raw["o"])),
                        high=Decimal(str(raw["h"])),
                        low=Decimal(str(raw["l"])),
                        close=Decimal(str(raw["c"])),
                        volume=int(raw["v"]),
                        provider=self.provider_id,
                        adjusted=False,
                        source="alpaca-iex",
                        provider_timestamp=timestamp,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if not bars:
            raise DataQualityError(f"Alpaca returned no valid historical bars for {symbol}.")
        return ProviderResult("historical", (bars, []), datetime.now(timezone.utc), self.capabilities_report())

    def capabilities_report(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            supports_real_time=True,
            supports_adjusted=False,
            delay_label=DataDelay.REALTIME,
            capabilities={
                "authentication": "BYO user key pair in Windows Credential Manager",
                "free_equities_feed": "IEX only; not full-market SIP",
                "quote_transport": "authenticated REST snapshot",
                "streaming": "BYO-key WebSocket trade stream; Basic tier limit 30 symbols",
                "network_required": True,
            },
        )


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _unix_timestamp(value: Any) -> datetime | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(parsed, tz=timezone.utc) if parsed > 0 else None


def _rfc3339_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_utc_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
