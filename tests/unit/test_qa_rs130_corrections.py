from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep

import pytest

from app.historical_store.repository import HistoricalStore
from app.market_data.contracts import (
    AssetClass,
    Capability,
    CredentialKind,
    DelayClass,
    FabricRequest,
    FabricResult,
    ProviderDescriptor,
    ProviderTerms,
    RateLimitState,
)
from app.market_data.discovery import DiscoveryCoordinator, InstrumentDiscovery, OfficialNasdaqDirectorySource
from app.market_data.instruments import DiscoveredInstrument
from app.market_data.providers.byo_free_tier import TwelveDataAdapter
from app.market_data.providers.catalog import default_fabric_registry
from app.market_data.registry import FabricRegistry
from app.market_data.router import MarketDataRouter, NoEligibleProvider
from app.market_data.service import FabricMarketDataService
from app.market_data.service import infer_asset_class
from app.market_data.validation import ResultValidationError, validate_result
from app.models.schemas import OhlcvBar, QuoteSnapshot
from app.security.credentials import InMemoryCredentialStore
from app.security.credentials import ProviderCredentials
from scripts.handoff.verify_source_completeness import verify_changed_files, verify_input_manifest


def _descriptor(
    provider_id: str,
    *,
    capabilities: frozenset[Capability] = frozenset({Capability.QUOTE}),
    max_concurrency: int = 2,
    interval: float = 0.0,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id,
        provider_id,
        frozenset({AssetClass.EQUITY}),
        capabilities,
        False,
        CredentialKind.NONE,
        DelayClass.DELAYED,
        ProviderTerms("https://example.invalid", "2026-08-19", "test", decision="enabled"),
        enabled=True,
        max_concurrency=max_concurrency,
        minimum_request_interval_seconds=interval,
    )


class DynamicAdapter:
    def __init__(self, provider_id: str, *, delay: float = 0.0, descriptor: ProviderDescriptor | None = None):
        self.descriptor = descriptor or _descriptor(provider_id)
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.starts: list[float] = []
        self._lock = Lock()
        self.preflight = RateLimitState()

    def request(self, request: FabricRequest) -> FabricResult:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.starts.append(monotonic())
        try:
            sleep(self.delay)
            received = datetime.now(timezone.utc)
            return FabricResult(
                request.request_id,
                self.descriptor.provider_id,
                request.canonical_symbol,
                request.canonical_instrument_id,
                request.canonical_symbol,
                request.capability,
                received - timedelta(minutes=1),
                received,
                self.descriptor.delay_class,
                "USD",
                request.venue,
                {"price": "101.25"},
                self.descriptor.display_name,
                0,
            )
        finally:
            with self._lock:
                self.active -= 1

    def normalize_symbol(self, symbol: str) -> str:
        return symbol.upper()

    def provider_symbol_for(self, request: FabricRequest) -> str:
        return request.canonical_symbol

    def health_check(self) -> bool:
        return True

    def rate_limit_state(self) -> RateLimitState:
        return self.preflight

    def list_instruments(self) -> list[dict[str, object]]:
        return []


def _request(symbol: str = "AAPL", capability: Capability = Capability.QUOTE, interval: str | None = None):
    return FabricRequest(
        f"equity:{symbol}", symbol, AssetClass.EQUITY, capability, interval=interval
    )


def _result(request: FabricRequest, source: datetime, received: datetime, delay: DelayClass) -> FabricResult:
    return FabricResult(
        request.request_id,
        "provider",
        request.canonical_symbol,
        request.canonical_instrument_id,
        request.canonical_symbol,
        request.capability,
        source,
        received,
        delay,
        "USD",
        request.venue,
        {"price": "1"},
        "provider",
        0,
    )


def test_production_catalog_bridges_yahoo_and_finnhub_without_mock_or_alpaca() -> None:
    registry = default_fabric_registry(InMemoryCredentialStore())
    ids = {adapter.descriptor.provider_id for adapter in registry.snapshot()}
    assert {"yahoo", "finnhub"} <= ids
    assert "mock" not in ids and "alpaca" not in ids
    assert Capability.HISTORICAL in registry.get("yahoo").descriptor.capabilities
    assert registry.get("finnhub").descriptor.capabilities == frozenset({Capability.QUOTE})


def test_fabric_service_preserves_winner_provenance_and_unsupported_is_truthful() -> None:
    registry = FabricRegistry()
    registry.register(DynamicAdapter("slow", delay=0.04))
    registry.register(DynamicAdapter("fast", delay=0.001))
    with MarketDataRouter(registry) as router:
        result = FabricMarketDataService(router).fetch_quote("AAPL")
        assert isinstance(result.payload, QuoteSnapshot)
        assert result.metadata.provider_id == "fast"
        assert result.payload.instrument.provider == "fast"
        with pytest.raises(NoEligibleProvider, match="No authorized healthy provider"):
            router.fetch(_request(capability=Capability.FUNDAMENTALS))


def test_user_visible_service_routes_crypto_symbols_only_to_crypto_asset_pool() -> None:
    assert infer_asset_class("BTC-USD") == AssetClass.CRYPTO_SPOT
    assert infer_asset_class("BRK-B") == AssetClass.EQUITY
    assert infer_asset_class("EURUSD=X") == AssetClass.FX
    equity = DynamicAdapter("equity")
    crypto_descriptor = replace(
        _descriptor("crypto"), asset_classes=frozenset({AssetClass.CRYPTO_SPOT})
    )
    crypto = DynamicAdapter("crypto", descriptor=crypto_descriptor)
    registry = FabricRegistry()
    registry.register(equity)
    registry.register(crypto)
    with MarketDataRouter(registry) as router:
        result = FabricMarketDataService(router).fetch_quote("BTC-USD")
    assert result.metadata.provider_id == "crypto"
    assert equity.calls == 0 and crypto.calls == 1


def test_production_ui_and_service_sources_enter_the_router() -> None:
    root = Path(__file__).resolve().parents[2]
    service_source = (root / "app" / "market_data" / "service.py").read_text(encoding="utf-8")
    ui_source = (root / "app" / "ui" / "main.py").read_text(encoding="utf-8")
    assert "self.router.fetch(request, budget_seconds=" in service_source
    assert "class _QuoteRefreshTask(QRunnable)" in ui_source
    assert "class _HistoryRefreshTask(QRunnable)" in ui_source
    assert "refresh_symbol_report(" not in ui_source
    assert "QThreadPool.globalInstance().start(task)" in ui_source
    assert "self.provider.fetch_historical" not in ui_source


def test_execution_gate_enforces_exact_concurrency_and_one_second_starts() -> None:
    descriptor = _descriptor("paced", max_concurrency=1, interval=1.0)
    adapter = DynamicAdapter("paced", delay=0.01, descriptor=descriptor)
    registry = FabricRegistry()
    registry.register(adapter)
    with MarketDataRouter(registry, max_workers=8) as router:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda i: router.fetch(_request(f"SYM{i}")), range(8)))
    assert len(results) == 8
    assert adapter.max_active <= 1
    intervals = [right - left for left, right in zip(adapter.starts, adapter.starts[1:])]
    assert all(value >= 0.98 for value in intervals)


def test_rate_limit_preflight_skips_provider_before_dispatch() -> None:
    limited = DynamicAdapter("limited")
    limited.preflight = RateLimitState(limited=True, retry_after_seconds=60)
    healthy = DynamicAdapter("healthy")
    registry = FabricRegistry()
    registry.register(limited)
    registry.register(healthy)
    with MarketDataRouter(registry) as router:
        assert router.fetch(_request()).provider_id == "healthy"
    assert limited.calls == 0


def test_default_twelve_data_quota_has_minute_and_daily_windows() -> None:
    adapter = TwelveDataAdapter(InMemoryCredentialStore())
    assert [(window.limit, window.seconds) for window in adapter.quota._windows] == [(8, 60), (800, 86400)]
    assert adapter.descriptor.max_concurrency == 1
    assert adapter.descriptor.minimum_request_interval_seconds == 7.5


def test_capability_aware_daily_weekend_holiday_and_intraday_freshness() -> None:
    daily = _request(capability=Capability.HISTORICAL, interval="1day")
    wednesday = datetime(2026, 8, 19, 16, tzinfo=timezone.utc)
    validate_result(daily, _result(daily, datetime(2026, 8, 18, tzinfo=timezone.utc), wednesday, DelayClass.END_OF_DAY))
    sunday = datetime(2026, 8, 23, 16, tzinfo=timezone.utc)
    validate_result(daily, _result(daily, datetime(2026, 8, 21, tzinfo=timezone.utc), sunday, DelayClass.END_OF_DAY))
    labor_day = datetime(2026, 9, 7, 16, tzinfo=timezone.utc)
    validate_result(daily, _result(daily, datetime(2026, 9, 4, tzinfo=timezone.utc), labor_day, DelayClass.END_OF_DAY))
    with pytest.raises(ResultValidationError, match="stale"):
        validate_result(daily, _result(daily, datetime(2026, 9, 3, tzinfo=timezone.utc), labor_day, DelayClass.END_OF_DAY))
    intraday = _request(capability=Capability.HISTORICAL, interval="1m")
    with pytest.raises(ResultValidationError, match="stale"):
        validate_result(intraday, _result(intraday, wednesday - timedelta(minutes=20), wednesday, DelayClass.DELAYED))


def _item(symbol: str, name: str, venue: str) -> DiscoveredInstrument:
    return DiscoveredInstrument(symbol, name, AssetClass.EQUITY, "Common Stock", venue, provider_symbol=symbol)


def test_venue_change_preserves_identity_and_ambiguous_symbol_does_not_merge(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "history.sqlite")
    discovery = InstrumentDiscovery(store._con)
    discovery.import_snapshot("source", "Source", "https://example.invalid", [_item("AAA", "Alpha", "Q")], b"v1")
    instrument_id = store._con.execute("SELECT instrument_id FROM rs_instruments").fetchone()[0]
    report = discovery.import_snapshot("source", "Source", "https://example.invalid", [_item("AAA", "Alpha", "N")], b"v2")
    rows = store._con.execute("SELECT instrument_id,primary_venue,is_active FROM rs_instruments").fetchall()
    assert report.added == 0 and report.removed_inactive == 0 and report.changed == 1
    assert [tuple(row) for row in rows] == [(instrument_id, "N", 1)]
    assert store._con.execute("SELECT change_type FROM rs_discovery_changes ORDER BY discovery_change_id DESC").fetchone()[0] == "venue_changed"

    discovery.import_snapshot(
        "ambiguous",
        "Ambiguous",
        "https://example.invalid",
        [_item("DUP", "One", "Q"), _item("DUP", "Two", "A")],
        b"a1",
    )
    ambiguous = discovery.import_snapshot(
        "ambiguous", "Ambiguous", "https://example.invalid", [_item("DUP", "One", "N")], b"a2"
    )
    assert ambiguous.added == 1 and ambiguous.removed_inactive == 2
    store.close()


class FixtureSource:
    def __init__(self, nasdaq: str, other: str) -> None:
        self.nasdaq = nasdaq
        self.other = other
        self.fail = False

    def fetch(self):
        if self.fail:
            raise RuntimeError("offline")
        source = OfficialNasdaqDirectorySource(
            lambda url: self.nasdaq if url.endswith("nasdaqlisted.txt") else self.other
        )
        return source.fetch()


def test_production_discovery_due_manual_offline_and_search(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "history.sqlite")
    source = FixtureSource(
        "Symbol|Security Name|ETF|Exchange\nAAA|Alpha Corp|N|Q\n",
        "ACT Symbol|Security Name|Exchange|ETF\nBBB|Beta Corp|N|N\n",
    )
    coordinator = DiscoveryCoordinator(store.path, source=source)
    first = coordinator.refresh_if_due()
    assert first is not None and first.result(timeout=3).added == 2
    assert coordinator.refresh_if_due() is None
    assert coordinator.search("AA")[0]["canonical_symbol"] == "AAA"
    source.nasdaq += "CCC|Gamma Corp|N|Q\n"
    assert coordinator.refresh_manual().result(timeout=3).added == 1
    before = coordinator.status()["source_sha256"]
    source.fail = True
    with pytest.raises(RuntimeError, match="offline"):
        coordinator.refresh_manual().result(timeout=3)
    assert coordinator.status()["source_sha256"] == before
    assert {row["canonical_symbol"] for row in coordinator.search("")} == set()
    assert coordinator.search("CCC")[0]["canonical_symbol"] == "CCC"
    coordinator.shutdown()
    store.close()


def test_official_source_rejects_malformed_partial_snapshot() -> None:
    malformed = "Symbol|Security Name|ETF|Exchange\n" + "\n".join(f"BAD{i}|missing" for i in range(12))
    source = OfficialNasdaqDirectorySource(
        lambda url: malformed if url.endswith("nasdaqlisted.txt") else "ACT Symbol|Security Name|Exchange|ETF\n"
    )
    with pytest.raises(ValueError, match="parse-error threshold"):
        source.fetch()


def test_source_and_input_reconstructability_gates_fail_closed(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "present.py").write_text("ok", encoding="utf-8")
    changed = tmp_path / "CHANGED_FILES.txt"
    changed.write_text("present.py\n", encoding="utf-8")
    assert verify_changed_files(changed, source) == ["present.py"]
    changed.write_text("present.py\nmissing.py\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing.py"):
        verify_changed_files(changed, source)

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "blueprint.sql").write_text("SELECT 1;", encoding="utf-8")
    manifest = inputs / "MANIFEST.json"
    manifest.write_text(json.dumps({"entries": [{"path": "blueprint.sql"}]}), encoding="utf-8")
    assert verify_input_manifest(manifest, inputs) == ["blueprint.sql"]
    (inputs / "blueprint.sql").unlink()
    with pytest.raises(RuntimeError, match="blueprint.sql"):
        verify_input_manifest(manifest, inputs)


def test_discovery_shutdown_waits_cleanly_for_running_refresh(tmp_path) -> None:
    store = HistoricalStore(tmp_path / "history.sqlite")
    entered = Event()
    release = Event()

    class BlockingSource:
        def fetch(self):
            entered.set()
            assert release.wait(3)
            return [_item("AAA", "Alpha", "Q")], b"snapshot", 0

    coordinator = DiscoveryCoordinator(store.path, source=BlockingSource())
    future = coordinator.refresh_manual()
    assert entered.wait(1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        shutdown = executor.submit(coordinator.shutdown, wait=True)
        sleep(0.05)
        assert not shutdown.done()
        release.set()
        shutdown.result(timeout=3)
    assert future.result(timeout=1).added == 1
    store.close()


def test_byo_history_preserves_newest_source_timestamp() -> None:
    class Transport:
        def __init__(self, payload):
            self.payload = payload

        def get_json(self, _url, headers=None):  # noqa: ARG002
            return self.payload

    store = InMemoryCredentialStore()
    store.save(ProviderCredentials("twelve_data", {"api_key": "secret"}))
    twelve = TwelveDataAdapter(
        store,
        Transport(
            {
                "values": [
                    {"datetime": "2026-08-18", "open": "1", "high": "2", "low": "1", "close": "2", "volume": "3"},
                    {"datetime": "2026-08-15", "open": "1", "high": "2", "low": "1", "close": "2", "volume": "3"},
                ]
            }
        ),
    )
    result = twelve.request(_request(capability=Capability.HISTORICAL, interval="1day"))
    assert result.provider_timestamp == datetime(2026, 8, 18, tzinfo=timezone.utc)
