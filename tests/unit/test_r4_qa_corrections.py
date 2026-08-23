from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.application.active_symbol import ActiveSymbolController
from app.application.bootstrap import RangeScoutApplication
from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import (
    AssetClass, Capability, CredentialKind, DelayClass, FabricRequest, FabricResult,
    ProviderDescriptor, ProviderTerms, RateLimitState,
)
from app.market_data.providers.byo_free_tier import TwelveDataAdapter, _twelve_asset_class
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter
from app.market_data.service import FabricMarketDataService, infer_asset_class
from app.models.schemas import AssetType, InstrumentIdentifier
from app.research.fund import FundResearchService
from app.research.models import Availability
from app.research.routing import route_snapshot
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials


NOW = datetime(2026, 8, 22, 21, tzinfo=timezone.utc)


def _database(path: Path) -> Path:
    with HistoricalStore(path):
        pass
    provision_company_master(path)
    InstrumentReferenceSeeder(path).apply()
    return path


@pytest.mark.parametrize(
    ("query", "top", "unique"),
    [
        ("Boeing", "BA", "BA"), ("Oracle", "ORCL", "ORCL"), ("Ford", "F", "F"),
        ("Intel", "INTC", "INTC"), ("JPMorgan", "JPM", "JPM"),
        ("Coca Cola", "KO", "KO"), ("Coca-Cola", "KO", "KO"),
        ("Berkshire Hathaway", "BRK.B", None), ("Apple", "AAPL", "AAPL"),
        ("Microsoft", "MSFT", "MSFT"), ("BlackRock Enhanced", "BOE", "BOE"),
        ("Gold", "XAU/USD", None), ("Gold Spot", "XAU/USD", "XAU/USD"),
        ("EUR/USD", "EUR/USD", "EUR/USD"),
        ("Dow Jones", "^DJI", "^DJI"),
        ("S&P 500", "^GSPC", "^GSPC"), ("Nasdaq", "^IXIC", "^IXIC"),
        ("Bitcoin", "BTC/USD", "BTC/USD"),
    ],
)
def test_r4_generic_issuer_ranking_and_disambiguation(tmp_path: Path, query: str, top: str, unique: str | None) -> None:
    resolver = InstrumentResolver(_database(tmp_path / "history.sqlite"))
    results = resolver.search(query, 10)
    assert results and results[0].symbol == top
    resolved = resolver.resolve_unique(query)
    assert (resolved.symbol if resolved else None) == unique
    if query in {"Boeing", "Oracle"}:
        assert results[0].instrument.asset_class == "equity"
        assert "-P" not in results[0].symbol


def test_reference_v2_classifies_cef_generically_and_is_additive(tmp_path: Path) -> None:
    path = _database(tmp_path / "history.sqlite")
    resolver = InstrumentResolver(path)
    boe = resolver.resolve_unique("BlackRock Enhanced")
    assert boe is not None
    assert boe.instrument.asset_class == "closed_end_fund"
    assert boe.instrument.subtype == "closed_end_fund"
    with HistoricalStore(path) as store:
        store._con.execute("CREATE TABLE IF NOT EXISTS r4_user_sentinel(value TEXT)")
        store._con.execute("INSERT INTO r4_user_sentinel VALUES('preserve')")
        store._con.commit()
    assert InstrumentReferenceSeeder(path).apply() == 0
    with HistoricalStore(path) as store:
        assert store._con.execute("SELECT value FROM r4_user_sentinel").fetchone()[0] == "preserve"


class _Adapter:
    def __init__(self) -> None:
        self.requests: list[FabricRequest] = []
        self.descriptor = ProviderDescriptor(
            "canonical_fake", "Canonical Fake",
            frozenset({
                AssetClass.EQUITY, AssetClass.ETF, AssetClass.INDEX, AssetClass.FX,
                AssetClass.CRYPTO_SPOT, AssetClass.COMMODITY_SPOT,
            }),
            frozenset({Capability.QUOTE, Capability.HISTORICAL}),
            False, CredentialKind.NONE, DelayClass.REALTIME,
            ProviderTerms("https://example.invalid/docs", "2026-08-22", "deterministic test fake", decision="enabled"),
            enabled=True,
        )

    def request(self, request: FabricRequest) -> FabricResult:
        self.requests.append(request)
        payload = (
            {"price": "2500.25"}
            if request.capability is Capability.QUOTE
            else {"bars": [{"datetime": "2026-08-21T00:00:00+00:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5"}]}
        )
        return FabricResult(
            request.request_id, "canonical_fake", request.canonical_symbol,
            request.canonical_instrument_id, request.canonical_symbol, request.capability,
            NOW, NOW, DelayClass.REALTIME, "USD", request.venue, payload, "deterministic fake", 1,
        )

    def health_check(self) -> bool:
        return True

    def rate_limit_state(self) -> RateLimitState:
        return RateLimitState()

    def list_instruments(self) -> list[dict[str, object]]:
        return []


@pytest.mark.parametrize(
    ("symbol", "asset_class", "model_type"),
    [
        ("AAPL", AssetClass.EQUITY, AssetType.STOCK),
        ("SPY", AssetClass.ETF, AssetType.ETF),
        ("^DJI", AssetClass.INDEX, AssetType.INDEX),
        ("^GSPC", AssetClass.INDEX, AssetType.INDEX),
        ("BTC/USD", AssetClass.CRYPTO_SPOT, AssetType.CRYPTO),
        ("EUR/USD", AssetClass.FX, AssetType.FOREX),
        ("XAU/USD", AssetClass.COMMODITY_SPOT, AssetType.COMMODITY_SPOT),
    ],
)
def test_quote_and_history_preserve_canonical_asset_and_provider_symbol(
    symbol: str, asset_class: AssetClass, model_type: AssetType,
) -> None:
    adapter = _Adapter()
    registry = FabricRegistry()
    registry.register(adapter)
    with MarketDataRouter(registry) as router:
        service = FabricMarketDataService(router)
        provider_symbol = symbol.replace("/", "")
        quote = service.fetch_quote(
            symbol, asset_class=asset_class, canonical_instrument_id="instrument:77",
            provider_symbols=(("canonical_fake", provider_symbol),),
        )
        history = service.fetch_historical(
            InstrumentIdentifier(symbol, "CANONICAL"), asset_class=asset_class,
            canonical_instrument_id="instrument:77",
            provider_symbols=(("canonical_fake", provider_symbol),),
        )
    assert quote.payload.instrument.asset_type is model_type
    assert len(history.payload[0]) == 1
    assert [request.asset_class for request in adapter.requests] == [asset_class, asset_class]
    assert [request.canonical_symbol for request in adapter.requests] == [provider_symbol, provider_symbol]
    assert [request.canonical_instrument_id for request in adapter.requests] == ["instrument:77", "instrument:77"]


def test_fallback_inference_distinguishes_fx_crypto_metals_and_indexes() -> None:
    assert infer_asset_class("EUR/USD") is AssetClass.FX
    assert infer_asset_class("EURUSD=X") is AssetClass.FX
    assert infer_asset_class("BTC/USD") is AssetClass.CRYPTO_SPOT
    assert infer_asset_class("XAU/USD") is AssetClass.COMMODITY_SPOT
    assert infer_asset_class("^DJI") is AssetClass.INDEX


class _DiscoveryAdapter:
    descriptor = ProviderDescriptor(
        "discovery_fake", "Discovery Fake", frozenset({AssetClass.EQUITY}),
        frozenset({Capability.SYMBOL_SEARCH}), False, CredentialKind.NONE, DelayClass.REFERENCE,
        ProviderTerms("https://example.invalid/docs", "2026-08-22", "deterministic test fake", decision="enabled"),
        enabled=True,
    )

    def health_check(self) -> bool:
        return True

    def rate_limit_state(self) -> RateLimitState:
        return RateLimitState()

    def list_instruments(self) -> list[dict[str, object]]:
        return []

    def search_instruments(self, query: str, limit: int) -> list[dict[str, object]]:
        assert query == "Zeta Quantum"
        return [{
            "symbol": "ZZQX", "name": "Zeta Quantum Systems Common Stock",
            "asset_class": "equity", "instrument_type": "Common Stock", "subtype": "common_stock",
            "venue": "NASDAQ", "provider_symbol": "ZZQX.O", "currency": "USD",
            "verified_at_utc": NOW.isoformat(),
        }]


def test_production_discovery_orchestration_caches_verified_mapping(tmp_path: Path) -> None:
    registry = FabricRegistry()
    registry.register(_DiscoveryAdapter())
    app = RangeScoutApplication(
        data_dir=tmp_path, credential_store=InMemoryCredentialStore(), fabric_registry=registry,
    )
    try:
        assert InstrumentResolver(app.store.path).search("Zeta Quantum") == []
        results = app.discover_instruments("Zeta Quantum")
        assert results and results[0].symbol == "ZZQX"
        assert results[0].instrument.provider_symbols["discovery_fake"] == "ZZQX.O"
        restarted = InstrumentResolver(app.store.path).resolve_unique("Zeta Quantum")
        assert restarted is not None and restarted.symbol == "ZZQX"
    finally:
        app.shutdown()


def test_twelve_data_declares_real_gold_and_discovery_capabilities_without_network() -> None:
    descriptor = TwelveDataAdapter.descriptor
    assert AssetClass.COMMODITY_SPOT in descriptor.asset_classes
    assert Capability.SYMBOL_SEARCH in descriptor.capabilities
    assert _twelve_asset_class("Commodity", "XAU/USD") == "commodity_spot"
    assert _twelve_asset_class("Physical Currency", "EUR/USD") == "fx"
    assert _twelve_asset_class("Digital Currency", "BTC/USD") == "crypto_spot"

    class Transport:
        def get_json(self, url: str):
            assert "https://api.twelvedata.com/symbol_search?" in url
            return {"data": [{
                "symbol": "XAU/USD", "instrument_name": "Gold Spot / US Dollar",
                "exchange": "Commodity Aggregate", "instrument_type": "Commodity", "currency": "USD",
            }]}

    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("twelve_data", {"api_key": "deterministic_test_key"}))
    rows = TwelveDataAdapter(credentials, transport=Transport()).search_instruments("Gold", 5)
    assert rows == [{
        "canonical_symbol": "XAU/USD", "provider_symbol": "XAU/USD",
        "name": "Gold Spot / US Dollar", "venue": "COMMODITY AGGREGATE", "currency": "USD",
        "instrument_type": "Commodity", "subtype": "commodity",
        "asset_class": "commodity_spot", "verified_at_utc": rows[0]["verified_at_utc"],
    }]


class _FundClient:
    def __init__(self) -> None:
        self.companyfacts_called = False

    def company_map(self):
        return {"BOE": {"cik": "0001320375", "name": "BlackRock Enhanced Global Dividend Trust"}}

    def companyfacts(self, _cik):
        self.companyfacts_called = True
        raise AssertionError("Corporate companyfacts must not be called for CEF Research")

    def submissions(self, _cik):
        return {
            "name": "BlackRock Enhanced Global Dividend Trust", "exchanges": ["NYSE"],
            "sic": "", "sicDescription": "Investment company",
            "filings": {"recent": {
                "form": ["N-CSR", "8-K"], "filingDate": ["2026-08-20", "2026-08-19"],
                "accessionNumber": ["0001", "0002"],
            }},
        }


def test_fund_research_uses_sec_fund_filings_not_corporate_companyfacts() -> None:
    client = _FundClient()
    controller = ActiveSymbolController("AAPL")
    controller.set(
        "BOE", source="search", instrument_id=9, asset_class="closed_end_fund",
        subtype="closed_end_fund", venue="NYSE",
    )
    service = type("Service", (), {"client": client})()
    snapshot = route_snapshot(service, controller.request(source="research"), "annual")
    assert not client.companyfacts_called
    assert snapshot.sections["Overview"]["Latest fund filing"].value == "N-CSR"
    assert snapshot.sections["Financials"]["NAV / premium-discount"].availability is Availability.PROVIDER_NOT_SUPPORTED
    assert "Earnings" not in snapshot.sections and "Valuation" not in snapshot.sections


def test_primary_research_statuses_do_not_interpolate_raw_backend_errors() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "main.py").read_text(encoding="utf-8")
    assert 'SEC: unavailable for {request.symbol}: {error}' not in source
    assert 'Analyst Outlook: unavailable for {request.symbol}: {error}' not in source
    assert "Research data unavailable for {request.symbol}" in source
    assert "not self.research_tabs.isTabEnabled(current_index)" in source


def test_required_rapid_switch_sequence_keeps_final_canonical_identity(tmp_path: Path) -> None:
    resolver = InstrumentResolver(_database(tmp_path / "history.sqlite"))
    controller = ActiveSymbolController("AAPL")
    stale = []
    for query in ("AAPL", "Gold Spot", "BOE", "Dow Jones", "EUR/USD", "MSFT"):
        match = resolver.resolve_unique(query)
        assert match is not None, query
        item = match.instrument
        controller.set(
            item.symbol, source="instrument-search", instrument_id=item.instrument_id,
            name=item.name, venue=item.venue, asset_class=item.asset_class,
            provider_symbols=tuple(sorted(item.provider_symbols.items())), subtype=item.subtype,
        )
        stale.append(controller.request(source="in-flight"))
    final = controller.state
    assert final.symbol == "MSFT"
    assert final.asset_class == "equity"
    assert final.instrument_id == resolver.resolve_unique("MSFT").instrument.instrument_id
    assert all(not controller.accepts(request) for request in stale[:-1])
    assert controller.accepts(stale[-1])


def test_packaged_startup_symbol_uses_canonical_search_path() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "ui" / "runner.py").read_text(encoding="utf-8")
    assert 'window.set_active_symbol(symbol, source="global-search")' in source
    assert 'window.set_active_symbol(symbol, source="automation-startup")' not in source

