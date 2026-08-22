from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.market_data.contracts import AssetClass, Capability, FabricProviderError, FabricRequest, FreshnessPolicy
from app.market_data.providers.catalog import default_fabric_registry
from app.market_data.providers.crypto_public import CoinbaseExchangeAdapter, CoinPaprikaAdapter, KrakenAdapter


def request(symbol="BTC-USD", capability=Capability.QUOTE, interval=None):
    return FabricRequest(
        canonical_instrument_id=f"crypto:spot:{symbol}",
        canonical_symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        capability=capability,
        venue=None,
        interval=interval,
        freshness=FreshnessPolicy(timedelta(days=1), allow_delayed=True),
        request_id="crypto-1",
    )


class Transport:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get_json(self, url, headers=None):
        self.urls.append(url)
        for marker, response in self.responses:
            if marker in url:
                return response
        raise AssertionError(f"Unexpected URL: {url}")


def test_coinbase_quote_candles_and_discovery_normalize_provenance() -> None:
    transport = Transport(
        [
            ("/ticker", {"price": "60123.45", "volume": "12.5", "trade_id": 7, "time": "2026-08-18T15:00:00Z"}),
            ("/candles", [[1787065200, 59000, 61000, 60000, 60123, 10]]),
            ("/products", [{"id": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD", "status": "online"}]),
        ]
    )
    adapter = CoinbaseExchangeAdapter(transport)
    quote = adapter.request(request())
    assert quote.payload["price"] == "60123.45" and quote.provider_symbol == "BTC-USD"
    assert quote.attribution == "Coinbase Exchange" and quote.venue == "Coinbase"
    bars = adapter.request(request(capability=Capability.CANDLES, interval="86400"))
    assert bars.payload["bars"][0]["volume"] == 10
    assert adapter.list_instruments()[0]["provider_product_id"] == "BTC-USD"


def test_kraken_alias_quote_ohlc_and_discovery() -> None:
    transport = Transport(
        [
            ("/Ticker", {"error": [], "result": {"XXBTZUSD": {"c": ["60100.0", "1"], "v": ["10", "20"]}}}),
            ("/OHLC", {"error": [], "result": {"XXBTZUSD": [[1787065200, "1", "3", "0.5", "2", "2", "100", 3]], "last": 1}}),
            ("/AssetPairs", {"error": [], "result": {"XXBTZUSD": {"base": "XXBT", "quote": "ZUSD", "pair_decimals": 1, "lot_decimals": 8, "ordermin": "0.0001", "status": "online"}}}),
        ]
    )
    adapter = KrakenAdapter(transport)
    result = adapter.request(request())
    assert result.provider_symbol == "XBTUSD" and result.payload["price"] == "60100.0"
    assert "receipt time" in result.warnings[0]
    assert adapter.request(request(capability=Capability.HISTORICAL)).payload["bars"][0]["high"] == "3"
    assert adapter.list_instruments()[0]["minimum_size"] == "0.0001"


def test_coinpaprika_free_scope_and_known_id_mapping() -> None:
    transport = Transport(
        [
            ("/tickers/", {"id": "btc-bitcoin", "last_updated": "2026-08-18T15:00:00Z", "quotes": {"USD": {"price": 60000, "volume_24h": 2, "market_cap": 3}}}),
            ("/coins", [{"id": "btc-bitcoin", "symbol": "BTC", "name": "Bitcoin", "is_active": True}]),
        ]
    )
    adapter = CoinPaprikaAdapter(transport)
    assert adapter.request(request()).payload == {"price": "60000", "volume": 2, "market_cap": 3}
    assert adapter.list_instruments()[0]["base_asset"] == "BTC"
    with pytest.raises(FabricProviderError, match="resolved through discovery"):
        adapter.request(request("NEW-USD"))
    with pytest.raises(FabricProviderError, match="quote/universe"):
        adapter.request(request(capability=Capability.HISTORICAL))


def test_default_catalog_enables_only_three_reviewed_public_crypto_adapters() -> None:
    registry = default_fabric_registry()
    enabled = {item.descriptor.provider_id for item in registry.eligible(AssetClass.CRYPTO_SPOT, Capability.QUOTE)}
    assert enabled == {"coinbase_exchange", "kraken", "coinpaprika"}
    all_ids = {item.descriptor.provider_id for item in registry.snapshot()}
    assert {"google_finance_candidate", "msn_money_candidate", "binance_us_candidate"} <= all_ids
    assert not any(item.descriptor.enabled for item in registry.snapshot() if item.descriptor.provider_id.endswith("candidate"))


def test_public_crypto_adapters_expose_no_trading_or_account_capability() -> None:
    for adapter in (CoinbaseExchangeAdapter(Transport([])), KrakenAdapter(Transport([])), CoinPaprikaAdapter(Transport([]))):
        capabilities = {item.value for item in adapter.descriptor.capabilities}
        assert "trading" not in capabilities and "account" not in capabilities and "orders" not in capabilities
        assert adapter.descriptor.requires_credentials is False
