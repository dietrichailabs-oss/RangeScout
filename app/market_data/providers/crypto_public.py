"""Official public/no-key crypto market-data adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
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
    RateLimitState,
)
from app.market_data.providers.http import JsonTransport


def _utc(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class PublicCryptoAdapter:
    descriptor: ProviderDescriptor
    base_url: str

    def __init__(self, transport: JsonTransport | None = None) -> None:
        self.transport = transport or JsonTransport()

    def normalize_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper().replace("/", "-")
        if "-" not in normalized:
            normalized += "-USD"
        base, currency = normalized.split("-", 1)
        if not base or not currency or not base.replace(".", "").isalnum() or not currency.isalnum():
            raise ValueError("Invalid crypto product symbol.")
        return f"{base}-{currency}"

    def rate_limit_state(self) -> RateLimitState:
        return RateLimitState()

    def health_check(self) -> bool:
        try:
            return bool(self.list_instruments())
        except Exception:
            return False

    def _result(
        self,
        request: FabricRequest,
        provider_symbol: str,
        payload: dict[str, object],
        provider_timestamp: datetime,
        received_at: datetime,
        *,
        currency: str,
        venue: str,
        ttl: int = 2,
        warnings: tuple[str, ...] = (),
    ) -> FabricResult:
        return FabricResult(
            request_id=request.request_id,
            provider_id=self.descriptor.provider_id,
            provider_symbol=provider_symbol,
            canonical_instrument_id=request.canonical_instrument_id,
            canonical_symbol=request.canonical_symbol,
            capability=request.capability,
            provider_timestamp=provider_timestamp,
            received_at=received_at,
            delay_class=self.descriptor.delay_class,
            currency=currency,
            venue=venue,
            payload=payload,
            attribution=self.descriptor.terms.attribution,
            cache_ttl_seconds=ttl,
            warnings=warnings,
        )


class CoinbaseExchangeAdapter(PublicCryptoAdapter):
    base_url = "https://api.exchange.coinbase.com"
    descriptor = ProviderDescriptor(
        "coinbase_exchange",
        "Coinbase Exchange",
        frozenset({AssetClass.CRYPTO_SPOT}),
        frozenset({Capability.QUOTE, Capability.HISTORICAL, Capability.CANDLES, Capability.UNIVERSE}),
        False,
        CredentialKind.NONE,
        DelayClass.REALTIME,
        ProviderTerms(
            "https://docs.cdp.coinbase.com/exchange/introduction/welcome",
            "2026-08-18",
            "Official public Exchange market-data REST API.",
            attribution="Coinbase Exchange",
            caching="Short-lived normalized cache only.",
            redistribution="Coinbase Market Data Terms apply.",
            decision="enabled",
            reason="Official documentation explicitly classifies market-data APIs as public.",
        ),
        enabled=True,
        max_concurrency=2,
        minimum_request_interval_seconds=0.1,
    )

    def provider_symbol_for(self, request: FabricRequest) -> str:
        return self.normalize_symbol(request.canonical_symbol)

    def request(self, request: FabricRequest) -> FabricResult:
        product = self.provider_symbol_for(request)
        received = datetime.now(timezone.utc)
        if request.capability == Capability.QUOTE:
            data = self.transport.get_json(f"{self.base_url}/products/{quote(product)}/ticker")
            if not isinstance(data, dict) or "price" not in data:
                raise FabricProviderError("Coinbase quote response is incomplete.")
            timestamp = _utc(str(data.get("time") or ""), received)
            payload = {"price": str(data["price"]), "volume": data.get("volume"), "trade_id": data.get("trade_id")}
            return self._result(request, product, payload, timestamp, received, currency=product.split("-")[1], venue="Coinbase")
        if request.capability in {Capability.HISTORICAL, Capability.CANDLES}:
            granularity = request.interval or "86400"
            parameters = {"granularity": granularity}
            if request.start:
                parameters["start"] = request.start.isoformat()
            if request.end:
                parameters["end"] = request.end.isoformat()
            data = self.transport.get_json(f"{self.base_url}/products/{quote(product)}/candles?{urlencode(parameters)}")
            if not isinstance(data, list):
                raise FabricProviderError("Coinbase candle response is incomplete.")
            bars = [
                {"timestamp": int(row[0]), "low": row[1], "high": row[2], "open": row[3], "close": row[4], "volume": row[5]}
                for row in data if isinstance(row, list) and len(row) >= 6
            ]
            timestamp = datetime.fromtimestamp(max((bar["timestamp"] for bar in bars), default=received.timestamp()), timezone.utc)
            return self._result(request, product, {"bars": bars}, timestamp, received, currency=product.split("-")[1], venue="Coinbase", ttl=60)
        raise FabricProviderError("Coinbase capability is not implemented by this adapter method.")

    def list_instruments(self) -> list[dict[str, object]]:
        data = self.transport.get_json(f"{self.base_url}/products")
        if not isinstance(data, list):
            raise FabricProviderError("Coinbase products response is incomplete.")
        return [
            {"provider_product_id": row.get("id"), "base_asset": row.get("base_currency"), "quote_asset": row.get("quote_currency"), "status": row.get("status"), "venue": "Coinbase", "product_type": "spot"}
            for row in data if isinstance(row, dict) and row.get("id")
        ]


class KrakenAdapter(PublicCryptoAdapter):
    base_url = "https://api.kraken.com/0/public"
    descriptor = ProviderDescriptor(
        "kraken",
        "Kraken",
        frozenset({AssetClass.CRYPTO_SPOT}),
        frozenset({Capability.QUOTE, Capability.HISTORICAL, Capability.CANDLES, Capability.UNIVERSE}),
        False,
        CredentialKind.NONE,
        DelayClass.REALTIME,
        ProviderTerms(
            "https://docs.kraken.com/exchange/guides/rest/introduction",
            "2026-08-18",
            "Official public Spot REST market-data endpoints.",
            attribution="Kraken",
            caching="Short-lived normalized cache only.",
            redistribution="Kraken API terms apply.",
            decision="enabled",
            reason="Official structured public market-data endpoints.",
        ),
        enabled=True,
        max_concurrency=2,
        minimum_request_interval_seconds=0.2,
    )

    def provider_symbol_for(self, request: FabricRequest) -> str:
        base, currency = self.normalize_symbol(request.canonical_symbol).split("-", 1)
        return f"{'XBT' if base == 'BTC' else base}{currency}"

    def _unwrap(self, data):
        if not isinstance(data, dict) or data.get("error"):
            raise FabricProviderError("Kraken returned a provider error.")
        result = data.get("result")
        if not isinstance(result, dict):
            raise FabricProviderError("Kraken response is incomplete.")
        return result

    def request(self, request: FabricRequest) -> FabricResult:
        pair = self.provider_symbol_for(request)
        received = datetime.now(timezone.utc)
        if request.capability == Capability.QUOTE:
            result = self._unwrap(self.transport.get_json(f"{self.base_url}/Ticker?{urlencode({'pair': pair})}"))
            row = next(iter(result.values()), None)
            if not isinstance(row, dict) or not row.get("c"):
                raise FabricProviderError("Kraken ticker response is incomplete.")
            payload = {"price": str(row["c"][0]), "volume": row.get("v", [None, None])[-1]}
            return self._result(request, pair, payload, received, received, currency=self.normalize_symbol(request.canonical_symbol).split("-")[1], venue="Kraken", warnings=("Provider quote has no source timestamp; receipt time is used.",))
        if request.capability in {Capability.HISTORICAL, Capability.CANDLES}:
            interval = request.interval or "1440"
            result = self._unwrap(self.transport.get_json(f"{self.base_url}/OHLC?{urlencode({'pair': pair, 'interval': interval})}"))
            rows = next((value for key, value in result.items() if key != "last"), [])
            bars = [
                {"timestamp": int(row[0]), "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[6]}
                for row in rows if isinstance(row, list) and len(row) >= 7
            ]
            timestamp = datetime.fromtimestamp(max((bar["timestamp"] for bar in bars), default=received.timestamp()), timezone.utc)
            return self._result(request, pair, {"bars": bars}, timestamp, received, currency=self.normalize_symbol(request.canonical_symbol).split("-")[1], venue="Kraken", ttl=60)
        raise FabricProviderError("Kraken capability is not implemented by this adapter method.")

    def list_instruments(self) -> list[dict[str, object]]:
        result = self._unwrap(self.transport.get_json(f"{self.base_url}/AssetPairs"))
        return [
            {"provider_product_id": key, "base_asset": row.get("base"), "quote_asset": row.get("quote"), "status": row.get("status", "online"), "venue": "Kraken", "product_type": "spot", "price_precision": row.get("pair_decimals"), "size_precision": row.get("lot_decimals"), "minimum_size": row.get("ordermin")}
            for key, row in result.items() if isinstance(row, dict)
        ]


class CoinPaprikaAdapter(PublicCryptoAdapter):
    base_url = "https://api.coinpaprika.com/v1"
    descriptor = ProviderDescriptor(
        "coinpaprika",
        "CoinPaprika",
        frozenset({AssetClass.CRYPTO_SPOT}),
        frozenset({Capability.QUOTE, Capability.UNIVERSE}),
        False,
        CredentialKind.NONE,
        DelayClass.DELAYED,
        ProviderTerms(
            "https://docs.coinpaprika.com/api-reference/rest-api/introduction",
            "2026-08-18",
            "Official no-key free REST base URL and endpoints.",
            attribution="CoinPaprika",
            caching="Bounded five-minute quote and daily metadata cache.",
            redistribution="CoinPaprika Terms of Use apply.",
            decision="enabled",
            reason="Official documentation provides free no-key endpoints.",
        ),
        enabled=True,
        max_concurrency=1,
        minimum_request_interval_seconds=1.0,
    )

    _KNOWN = {"BTC": "btc-bitcoin", "ETH": "eth-ethereum", "SOL": "sol-solana", "XRP": "xrp-xrp", "ADA": "ada-cardano", "DOGE": "doge-dogecoin"}

    def provider_symbol_for(self, request: FabricRequest) -> str:
        base, currency = self.normalize_symbol(request.canonical_symbol).split("-", 1)
        if currency != "USD":
            raise FabricProviderError("CoinPaprika free quote adapter supports USD normalization only.")
        if base not in self._KNOWN:
            raise FabricProviderError("CoinPaprika coin ID must be resolved through discovery before quoting.")
        return self._KNOWN[base]

    def request(self, request: FabricRequest) -> FabricResult:
        if request.capability != Capability.QUOTE:
            raise FabricProviderError("CoinPaprika free adapter exposes quote/universe capabilities only.")
        coin_id = self.provider_symbol_for(request)
        received = datetime.now(timezone.utc)
        data = self.transport.get_json(f"{self.base_url}/tickers/{quote(coin_id)}")
        if not isinstance(data, dict) or not isinstance(data.get("quotes"), dict):
            raise FabricProviderError("CoinPaprika ticker response is incomplete.")
        usd = data["quotes"].get("USD")
        if not isinstance(usd, dict) or "price" not in usd:
            raise FabricProviderError("CoinPaprika USD quote is unavailable.")
        payload = {"price": str(usd["price"]), "volume": usd.get("volume_24h"), "market_cap": usd.get("market_cap")}
        return self._result(request, coin_id, payload, _utc(str(data.get("last_updated") or ""), received), received, currency="USD", venue="Aggregate", ttl=300)

    def list_instruments(self) -> list[dict[str, object]]:
        data = self.transport.get_json(f"{self.base_url}/coins")
        if not isinstance(data, list):
            raise FabricProviderError("CoinPaprika coins response is incomplete.")
        return [
            {"provider_product_id": row.get("id"), "base_asset": row.get("symbol"), "quote_asset": "USD", "status": "active" if row.get("is_active") else "inactive", "venue": "Aggregate", "product_type": "spot", "name": row.get("name")}
            for row in data if isinstance(row, dict) and row.get("id") and row.get("symbol")
        ]
