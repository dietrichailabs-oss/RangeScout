from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.application.active_symbol import ActiveSymbolController
from app.catalysts.correlation import CatalystCorrelator
from app.catalysts.entities import CatalystEvent
from app.catalysts.symbol_mapping import SymbolCatalog
from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION
from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import (
    AssetClass, Capability, CredentialKind, DelayClass, FabricRequest, FabricResult,
    ProviderDescriptor, ProviderTerms, RateLimitState,
)
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter
from app.research.models import Availability
from app.research.routing import ResearchRoute, plan_research, route_snapshot


NOW = datetime(2026, 8, 22, 16, tzinfo=timezone.utc)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "history.sqlite"
    with HistoricalStore(path):
        pass
    report = provision_company_master(path)
    assert report.available >= 16_000
    InstrumentReferenceSeeder(path).apply()
    return path


def test_schema_v8_is_additive_idempotent_and_preserves_master(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with HistoricalStore(path) as store:
        assert store._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        before = store._con.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0]
        assert store._con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store._con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert InstrumentReferenceSeeder(path).apply() == 0
    with HistoricalStore(path) as reopened:
        assert reopened._con.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0] == before


def test_acceptance_searches_return_canonical_disambiguated_instruments(tmp_path: Path) -> None:
    resolver = InstrumentResolver(_database(tmp_path))
    expected = {
        "Apple": "AAPL", "AAPL": "AAPL", "MSFT": "MSFT",
        "BOE": "BOE", "BlackRock Enhanced": "BOE",
        "Gold": "XAU/USD", "Gold Spot": "XAU/USD", "XAU": "XAU/USD",
        "XAUUSD": "XAU/USD", "XAU/USD": "XAU/USD",
        "Dow Jones": "^DJI", "Dow": "^DJI", "Dow 30": "^DJI", "DJIA": "^DJI",
        "S&P 500": "^GSPC", "SP500": "^GSPC", "SPX": "^GSPC",
        "Nasdaq Composite": "^IXIC", "Bitcoin": "BTC/USD",
    }
    for query, symbol in expected.items():
        match = resolver.resolve_unique(query)
        assert match is not None, query
        assert match.symbol == symbol, query
        assert match.instrument.instrument_id > 0
        assert match.display_text.count("·") == 3
    assert resolver.resolve_unique("Nasdaq").symbol == "^IXIC"
    assert resolver.resolve_unique("Gold").instrument.provider_symbols == {"twelve_data": "XAU/USD"}


def test_provider_discovery_enrichment_is_generic_and_cached(tmp_path: Path) -> None:
    resolver = InstrumentResolver(_database(tmp_path))
    assert resolver.enrich_provider_results("yahoo", [{
        "symbol": "ZZQX", "name": "Zeta Quantum Index", "asset_class": "index",
        "venue": "INDEX", "provider_symbol": "^ZZQX", "verified_at_utc": NOW.isoformat(),
    }]) == 1
    match = resolver.resolve_unique("Zeta Quantum")
    assert match is not None and match.symbol == "ZZQX"
    assert match.instrument.provider_symbols["yahoo"] == "^ZZQX"


def test_active_instrument_generation_and_stale_rejection() -> None:
    controller = ActiveSymbolController("AAPL")
    gold = controller.set("XAU/USD", source="search", instrument_id=42, name="Gold Spot",
                          venue="OTC SPOT", asset_class="commodity_spot",
                          provider_symbols=(("twelve_data", "XAU/USD"),), subtype="precious_metal_spot")
    stale = controller.request(source="quote")
    latest = controller.set("^DJI", source="search", instrument_id=43, name="Dow Jones Industrial Average",
                            venue="INDEX", asset_class="index", provider_symbols=(("yahoo", "^DJI"),))
    fresh = controller.request(source="quote")
    assert gold.generation + 1 == latest.generation
    assert not controller.accepts(stale)
    assert controller.accepts(fresh)
    assert fresh.instrument_id == 43 and dict(fresh.provider_symbols)["yahoo"] == "^DJI"


class _Adapter:
    def __init__(self) -> None:
        self.seen = None
        self.descriptor = ProviderDescriptor(
            "twelve_data", "Twelve Data", frozenset({AssetClass.COMMODITY_SPOT}), frozenset({Capability.QUOTE}),
            False, CredentialKind.NONE, DelayClass.REALTIME,
            ProviderTerms("https://twelvedata.com/docs", "2026-08-22", "test", decision="enabled"), enabled=True,
        )

    def request(self, request: FabricRequest) -> FabricResult:
        self.seen = request
        return FabricResult(request.request_id, "twelve_data", request.canonical_symbol,
                            request.canonical_instrument_id, request.canonical_symbol, request.capability,
                            NOW, NOW, DelayClass.REALTIME, "USD", request.venue, {"price": "2500"},
                            "Twelve Data", 1)

    def health_check(self): return True
    def rate_limit_state(self): return RateLimitState()
    def list_instruments(self): return []


def test_provider_mapping_is_applied_only_at_adapter_boundary() -> None:
    adapter, registry = _Adapter(), FabricRegistry()
    registry.register(adapter)
    request = FabricRequest("instrument:42", "XAU/USD", AssetClass.COMMODITY_SPOT, Capability.QUOTE,
                            provider_symbol_overrides=(("twelve_data", "XAUUSD"),))
    with MarketDataRouter(registry) as router:
        result = router.fetch(request)
    assert adapter.seen.canonical_symbol == "XAUUSD"
    assert result.canonical_symbol == "XAU/USD"
    assert result.canonical_instrument_id == "instrument:42"


def test_research_routes_funds_and_noncorporate_instruments_truthfully() -> None:
    fund = plan_research("stock", "closed_end_fund")
    assert fund.route is ResearchRoute.FUND and fund.sec_applicable and not fund.analyst_applicable
    index = plan_research("index")
    assert index.state is Availability.NOT_APPLICABLE and not index.sec_applicable
    request = ActiveSymbolController("AAPL")
    request.set("^GSPC", source="search", instrument_id=7, asset_class="index", subtype="broad_market_index")
    snapshot = route_snapshot(object(), request.request(source="research"), "annual")
    assert "NOT APPLICABLE" in snapshot.warnings[0].upper() or "do not apply" in snapshot.warnings[0]
    assert snapshot.sections["Overview"]["Data state"].availability is Availability.NOT_APPLICABLE


def test_catalyst_relevance_tiers_are_bounded_and_urgent_first() -> None:
    catalog = SymbolCatalog(); catalog.register("AAPL", "Apple Inc", "Technology", "Apple")
    events = [CatalystEvent(str(i), "Official", f"https://example.gov/{i}", NOW + timedelta(seconds=i), NOW,
                            f"Apple event {i}", symbols=("AAPL",), urgency="critical" if i == 0 else "normal")
              for i in range(20)]
    result = CatalystCorrelator(catalog).correlate(events, "AAPL", set(), set())
    assert len(result) == 12
    assert result[0].event.urgency == "critical"
