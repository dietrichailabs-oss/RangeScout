"""Live provider adapter backed by Yahoo Finance public chart/quote endpoints."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

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
from app.providers.base import MarketDataProvider, ProviderCapability, ProviderUnavailable, ProviderResult
from app.market_data.provider_symbols import ProviderSymbolError, normalize_yahoo_symbol



class YahooFinanceProvider:
    provider_id = "yahoo"
    provider_name = "Yahoo Finance"
    capabilities = ProviderCapability(
        can_lookup_symbol=True,
        can_fetch_quote=True,
        can_fetch_historical=True,
        can_fetch_actions=False,
        delay=DataDelay.DELAYED,
        supports_realtime=False,
        supports_adjusted=True,
        supports_indices=True,
        supports_etf=True,
        supports_stock=True,
    )

    def __init__(self, timeout_seconds: float = 12.0, user_agent: str | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.quote_timeout_seconds = min(2.5, timeout_seconds)
        self.user_agent = user_agent or "RangeScout/1.3"

    @staticmethod
    def normalize_symbol(raw: str) -> str:
        try:
            return normalize_yahoo_symbol(raw)
        except ProviderSymbolError as exc:
            raise ValidationError(str(exc)) from exc
    def resolve_instrument(self, symbol: str) -> Instrument:
        symbol_u = self.normalize_symbol(symbol)
        return Instrument(
            identifier=InstrumentIdentifier(symbol=symbol_u),
            name=f"{symbol_u}",
            asset_type=AssetType.STOCK,
            currency="USD",
            provider=self.provider_id,
        )

    def fetch_quote(self, symbol: str) -> ProviderResult:
        instrument = self.resolve_instrument(symbol)
        payload = self._query_json(
            self._quote_url(instrument.identifier.symbol), timeout_seconds=self.quote_timeout_seconds
        )
        chart = self._chart_result(payload, instrument.identifier.symbol)
        meta = chart.get("meta") or {}
        if not isinstance(meta, dict):
            raise DataQualityError(f"Malformed quote metadata for {instrument.identifier.symbol}.")
        last = meta.get("regularMarketPrice")
        previous = meta.get("previousClose", meta.get("chartPreviousClose"))
        volume = meta.get("regularMarketVolume")
        market_time = self._to_datetime(meta.get("regularMarketTime"))

        if last is None:
            raise DataQualityError(f"Quote payload is missing regularMarketPrice for {instrument.identifier.symbol}.")

        return ProviderResult(
            kind="quote",
            payload=QuoteSnapshot(
                instrument=instrument,
                last=Decimal(str(last)),
                previous_close=Decimal(str(previous)) if previous is not None else None,
                volume=int(volume) if volume is not None else None,
                timestamp=datetime.now(timezone.utc),
                provider_timestamp=market_time,
                delay_label=DataDelay.DELAYED,
                delay_seconds=900,
                currency=meta.get("currency", "USD"),
                day_low=self._optional_decimal(meta.get("regularMarketDayLow")),
                day_high=self._optional_decimal(meta.get("regularMarketDayHigh")),
                fifty_two_week_low=self._optional_decimal(meta.get("fiftyTwoWeekLow")),
                fifty_two_week_high=self._optional_decimal(meta.get("fiftyTwoWeekHigh")),
                average_volume=int(meta["averageDailyVolume3Month"]) if meta.get("averageDailyVolume3Month") is not None else None,
                market_cap=self._optional_decimal(meta.get("marketCap")),
                pre_market_price=self._optional_decimal(meta.get("preMarketPrice")),
                pre_market_change=self._optional_decimal(meta.get("preMarketChange")),
                pre_market_change_percent=self._optional_decimal(meta.get("preMarketChangePercent")),
                after_hours_price=self._optional_decimal(meta.get("postMarketPrice")),
                after_hours_change=self._optional_decimal(meta.get("postMarketChange")),
                after_hours_change_percent=self._optional_decimal(meta.get("postMarketChangePercent")),
            ),
            timestamp=datetime.now(timezone.utc),
            metadata=self.capabilities_report(),
        )

    def fetch_historical(
        self,
        identifier: InstrumentIdentifier,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: AdjustmentMode = AdjustmentMode.RAW,
    ) -> ProviderResult:
        payload = self._query_json(self._historical_url(identifier.symbol, start=start, end=end))
        chart = self._chart_result(payload, identifier.symbol)
        indicators = chart.get("indicators", {})
        quote_rows = (indicators.get("quote") or [])
        if not isinstance(quote_rows, list) or not quote_rows:
            raise DataQualityError(f"No historical bar payload for {identifier.symbol}.")
        quote = quote_rows[0] or {}

        timestamps = chart.get("timestamp") or []
        if not isinstance(timestamps, list):
            raise DataQualityError(f"Malformed historical payload for {identifier.symbol}.")

        bars = self._build_bars(
            symbol=identifier.symbol,
            timestamps=timestamps,
            quote=quote,
            start=start,
            end=end,
            adjusted=adjusted == AdjustmentMode.ADJUSTED,
        )
        if not bars:
            raise DataQualityError(f"No valid bars for {identifier.symbol} in requested window.")
        return ProviderResult(
            kind="historical",
            payload=(bars, []),
            timestamp=datetime.now(timezone.utc),
            metadata=self.capabilities_report(),
        )

    def fetch_actions(self, identifier: InstrumentIdentifier) -> ProviderResult:
        raise ProviderUnavailable("Yahoo corporate-action retrieval is not supported by RangeScout.")

    def capabilities_report(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            supports_real_time=self.capabilities.supports_realtime,
            supports_adjusted=self.capabilities.supports_adjusted,
            delay_label=self.capabilities.delay,
            capabilities={
                "exchange_coverage": ["US", "CA", "EU"],
                "supported_adjustment_modes": [AdjustmentMode.RAW.value, AdjustmentMode.ADJUSTED.value],
                "supports_realtime": self.capabilities.supports_realtime,
                "supports_actions": self.capabilities.can_fetch_actions,
                "network_required": True,
                "upstream": "Yahoo Finance chart endpoint",
            },
        )

    def _quote_url(self, symbol: str) -> str:
        symbol_u = self.normalize_symbol(symbol)
        safe_symbol = urllib.parse.quote(symbol_u, safe="._^=-")
        query = urllib.parse.urlencode(
            {
                "interval": "1m",
                "range": "1d",
                "includePrePost": "false",
                "events": "div,splits",
            }
        )
        return f"https://query1.finance.yahoo.com/v8/finance/chart/{safe_symbol}?{query}"

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def _historical_url(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> str:
        symbol_u = self.normalize_symbol(symbol)
        if start is None:
            period1 = 0
        else:
            period1 = int(start.timestamp())
        if end is None:
            period2 = int(datetime.now(timezone.utc).timestamp())
        else:
            inclusive_end = end
            if end.time() == datetime.min.time():
                inclusive_end = end + timedelta(days=1)
            period2 = int(inclusive_end.timestamp())
        safe_symbol = urllib.parse.quote(symbol_u, safe="._^=-")
        query = urllib.parse.urlencode(
            {
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
                "period1": period1,
                "period2": period2,
            }
        )
        return (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{safe_symbol}?{query}"
        )

    def _query_json(self, url: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(f"Live provider returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderUnavailable(
                "Live provider request failed. Check the network connection and try again."
            ) from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable("Live provider returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable("Live provider returned an unexpected response.")
        return payload

    @staticmethod
    def _chart_result(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
        chart = payload.get("chart")
        if not isinstance(chart, dict):
            raise DataQualityError(f"Malformed chart payload for {symbol}.")
        upstream_error = chart.get("error")
        if upstream_error:
            if isinstance(upstream_error, dict):
                detail = upstream_error.get("description") or upstream_error.get("code")
            else:
                detail = str(upstream_error)
            raise ProviderUnavailable(f"Live provider rejected {symbol}: {detail or 'unknown error'}.")
        result = chart.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise DataQualityError(f"No market data returned for {symbol}.")
        return result[0]

    def _build_bars(
        self,
        symbol: str,
        timestamps: list[Any],
        quote: dict[str, Any],
        start: datetime | None,
        end: datetime | None,
        adjusted: bool = False,
    ) -> list[OhlcvBar]:
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        if not (len(opens) == len(highs) == len(lows) == len(closes) == len(volumes) == len(timestamps)):
            raise DataQualityError(f"Malformed bar component lengths for {symbol}.")

        start_date = start.date() if isinstance(start, datetime) else None
        end_date = end.date() if isinstance(end, datetime) else None

        out: list[OhlcvBar] = []
        for idx, ts in enumerate(timestamps):
            provider_timestamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            bar_date = provider_timestamp.date()
            if start_date is not None and bar_date < start_date:
                continue
            if end_date is not None and bar_date > end_date:
                continue

            o = opens[idx]
            h = highs[idx]
            l = lows[idx]
            c = closes[idx]
            v = volumes[idx]
            if o is None or h is None or l is None or c is None or v is None:
                continue
            out.append(
                OhlcvBar(
                    instrument=InstrumentIdentifier(symbol=symbol.upper()),
                    date=bar_date,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(l)),
                    close=Decimal(str(c)),
                    volume=int(v),
                    provider=self.provider_id,
                    adjusted=adjusted,
                    source=self.provider_id,
                    provider_timestamp=provider_timestamp,
                )
            )
        return out

    @staticmethod
    def _to_datetime(raw: Any) -> datetime | None:
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)
