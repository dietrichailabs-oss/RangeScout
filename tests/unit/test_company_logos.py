from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
from urllib.error import HTTPError

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.company_logos.models import CompanyLogoAsset, CompanyLogoStatus
from app.company_logos.provider import FinnhubProfileLogoClient, LogoDevClient, LogoProviderError, TwelveDataLogoClient
from app.company_data.repository import CompanyDatabaseRepository
from app.company_logos.service import CompanyLogoService
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION
from app.historical_store.repository import HistoricalStore
from app.security.credentials import InMemoryCredentialStore, ProviderCredentials, supported_credential_fields
from tests.fakes.mock_provider import build_test_provider_registry

try:
    from PySide6.QtWidgets import QApplication
except Exception:  # pragma: no cover - exercised only without the Windows UI runtime
    QApplication = None  # type: ignore[assignment]


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Headers:
    def __init__(self, content_type: str = "image/png") -> None:
        self.content_type = content_type

    def get_content_type(self) -> str:
        return self.content_type


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200, content_type: str = "image/png", url: str | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers = _Headers(content_type)
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, amt: int = -1) -> bytes:
        return self.payload if amt < 0 else self.payload[:amt]

    def geturl(self) -> str:
        return self.url or "https://api.twelvedata.com/logo/company.example"


class _CountingClient:
    provider_id = "logo_dev"

    def __init__(self, payload: bytes = b"png-image") -> None:
        self.payload = payload
        self.calls: list[tuple[str, str | None, str, str]] = []

    def fetch(self, symbol: str, exchange: str | None, publishable_key: str, *, theme: str = "dark"):
        from app.company_logos.provider import LogoFetchResponse

        self.calls.append((symbol, exchange, publishable_key, theme))
        return LogoFetchResponse(self.payload, "image/png")


def _store_with_logo_schema(tmp_path: Path) -> Path:
    db = tmp_path / "history.sqlite"
    store = HistoricalStore(db)
    try:
        version = store._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert version == str(CURRENT_SCHEMA_VERSION)
        assert CURRENT_SCHEMA_VERSION >= 3
        columns = {row[1] for row in store._con.execute("PRAGMA table_info(rs_company_logo_state)")}
        assert "symbol" in columns
        assert "content_sha256" in columns
        assert "image_bytes" not in columns
    finally:
        store.close()
    return db


def test_logo_dev_credential_is_supported_and_redacted() -> None:
    assert supported_credential_fields("logo_dev") == ("publishable_key",)
    credentials = ProviderCredentials("logo_dev", {"publishable_key": "pk_example"})
    assert "pk_example" not in str(credentials)
    assert "pk_example" not in repr(credentials)


def test_logo_dev_ticker_url_uses_documented_ticker_route_and_exchange_suffix() -> None:
    client = LogoDevClient()
    us = client.build_url("AAPL", "NASDAQ", "pk_test", theme="dark")
    london = client.build_url("BP", "LSE", "pk_test", theme="light")
    assert us.startswith("https://img.logo.dev/ticker/AAPL?")
    assert "fallback=404" in us
    assert "format=png" in us
    assert "theme=dark" in us
    assert london.startswith("https://img.logo.dev/ticker/BP.L?")
    assert "theme=light" in london


def test_logo_client_accepts_only_bounded_images() -> None:
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, timeout, request.headers.get("User-agent")))
        return _Response(b"valid-png", content_type="image/png")

    result = LogoDevClient(opener=opener).fetch("AAPL", "NASDAQ", "pk_test", theme="dark")
    assert result.content == b"valid-png"
    assert result.content_type == "image/png"
    assert seen and "pk_test" in seen[0][0]


def test_logo_client_sanitizes_http_error_without_leaking_publishable_key() -> None:
    def opener(request, timeout):  # noqa: ARG001
        raise HTTPError(request.full_url, 401, "bad token", {}, None)

    with pytest.raises(LogoProviderError) as exc_info:
        LogoDevClient(opener=opener).fetch("AAPL", None, "pk_DO_NOT_LEAK", theme="dark")
    assert "pk_DO_NOT_LEAK" not in str(exc_info.value)
    assert exc_info.value.code == "authentication"


def test_service_uses_session_memory_cache_and_does_not_persist_logo_bytes_or_key(tmp_path: Path) -> None:
    db = _store_with_logo_schema(tmp_path)
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("logo_dev", {"publishable_key": "pk_PRIVATE_TEST"}))
    client = _CountingClient(payload=b"company-logo-bytes")
    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)
    service = CompanyLogoService(db, credentials, client=client, now_fn=lambda: now)

    first = service.resolve("AAPL", "NASDAQ", theme="dark")
    second = service.resolve("AAPL", "NASDAQ", theme="dark")

    assert first.status is CompanyLogoStatus.AVAILABLE
    assert first.image_bytes == b"company-logo-bytes"
    assert second.image_bytes == first.image_bytes
    assert len(client.calls) == 1

    raw_db = db.read_bytes()
    assert b"company-logo-bytes" not in raw_db
    assert b"pk_PRIVATE_TEST" not in raw_db


def test_service_without_key_is_nonblocking_unconfigured_fallback(tmp_path: Path) -> None:
    db = _store_with_logo_schema(tmp_path)
    credentials = InMemoryCredentialStore()
    client = _CountingClient()
    service = CompanyLogoService(db, credentials, client=client)
    result = service.resolve("MSFT")
    assert result.status is CompanyLogoStatus.UNCONFIGURED
    assert result.provider_id == "ticker_monogram"
    assert result.image_bytes is None
    assert client.calls == []


def test_permitted_persisted_logo_survives_service_restart_without_any_network_key(tmp_path: Path) -> None:
    db = _store_with_logo_schema(tmp_path)
    store = HistoricalStore(db)
    stamp = "2026-08-20T12:00:00+00:00"
    store._con.execute(
        """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,primary_venue,
           first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
           VALUES('BA','The Boeing Company','equity','NYSE',?,?,?,?)""",
        (stamp, stamp, stamp, stamp),
    )
    store._con.commit()
    store.close()
    logo_dir = tmp_path / "logos"
    logo_dir.mkdir()
    logo_path = logo_dir / "BA.png"
    logo_path.write_bytes(_ONE_PIXEL_PNG)
    digest = hashlib.sha256(_ONE_PIXEL_PNG).hexdigest().upper()
    assert CompanyDatabaseRepository(db).record_logo_result(
        "BA",
        source_id="wikimedia_commons",
        lookup_identifier="Q66",
        source_url="https://commons.wikimedia.org/wiki/File:Boeing.svg",
        content_sha256=digest,
        license_metadata="CC BY-SA 4.0; attribution retained",
        local_path="logos/BA.png",
        success=True,
        next_refresh_utc=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )

    class NoNetwork:
        calls = 0

        def fetch(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.calls += 1
            raise AssertionError("a permitted local logo must prevent every network provider call")

    network = NoNetwork()
    credentials = InMemoryCredentialStore()
    first = CompanyLogoService(
        db, credentials, client=network, finnhub_client=network, twelve_data_client=network
    ).resolve("BA", "NYSE")
    second = CompanyLogoService(
        db, credentials, client=network, finnhub_client=network, twelve_data_client=network
    ).resolve("BA", "NYSE")
    assert first.has_image and second.has_image
    assert first.image_bytes == second.image_bytes == _ONE_PIXEL_PNG
    assert first.provider_id == second.provider_id == "wikimedia_commons"
    assert first.persistent_local_copy and second.persistent_local_copy
    assert "CC BY-SA" in (second.license_metadata or "")
    assert network.calls == 0


def test_local_logo_rejects_hash_mismatch_and_path_escape(tmp_path: Path) -> None:
    db = _store_with_logo_schema(tmp_path)
    store = HistoricalStore(db)
    stamp = "2026-08-20T12:00:00+00:00"
    store._con.execute(
        """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,primary_venue,
           first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
           VALUES('BA','Boeing','equity','NYSE',?,?,?,?)""",
        (stamp, stamp, stamp, stamp),
    )
    store._con.commit(); store.close()
    (tmp_path / "logos").mkdir()
    (tmp_path / "logos" / "BA.png").write_bytes(_ONE_PIXEL_PNG)
    repository = CompanyDatabaseRepository(db)
    repository.record_logo_result(
        "BA", source_id="local_permitted", lookup_identifier="BA", source_url="local://fixture",
        content_sha256="0" * 64, license_metadata="User-provided permitted asset",
        local_path="logos/BA.png", success=True, next_refresh_utc=None,
    )
    result = CompanyLogoService(db, InMemoryCredentialStore()).resolve("BA", "NYSE")
    assert not result.has_image and result.provider_id == "ticker_monogram"
    repository.record_logo_result(
        "BA", source_id="local_permitted", lookup_identifier="BA", source_url="local://fixture",
        content_sha256=hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
        license_metadata="User-provided permitted asset", local_path="../BA.png",
        success=True, next_refresh_utc=None,
    )
    result = CompanyLogoService(db, InMemoryCredentialStore()).resolve("BA", "NYSE")
    assert not result.has_image and result.provider_id == "ticker_monogram"


def test_finnhub_and_twelve_data_logo_adapters_use_official_bounded_routes() -> None:
    calls: list[str] = []

    def finnhub_open(request, timeout):  # noqa: ARG001
        calls.append(request.full_url)
        if request.full_url.startswith(FinnhubProfileLogoClient.profile_url):
            return _Response(json.dumps({"logo": "https://cdn.example.com/ba.png"}).encode(), content_type="application/json")
        return _Response(_ONE_PIXEL_PNG)

    finnhub = FinnhubProfileLogoClient(opener=finnhub_open).fetch("BA", "NYSE", "FH_TEST_KEY")
    assert finnhub.content == _ONE_PIXEL_PNG and finnhub.source_url == "https://cdn.example.com/ba.png"
    assert calls[0].startswith("https://finnhub.io/api/v1/stock/profile2?")

    twelve_calls: list[str] = []

    def twelve_open(request, timeout):  # noqa: ARG001
        twelve_calls.append(request.full_url)
        if len(twelve_calls) == 1:
            return _Response(
                json.dumps({"meta": {"symbol": "BA"}, "url": "https://api.twelvedata.com/logo/boeing.com"}).encode(),
                content_type="application/json",
            )
        return _Response(_ONE_PIXEL_PNG, url="https://api.twelvedata.com/logo/boeing.com")

    twelve = TwelveDataLogoClient(opener=twelve_open).fetch("BA", "NYSE", "TD_TEST_KEY")
    assert twelve.content == _ONE_PIXEL_PNG
    assert twelve.source_url == "https://api.twelvedata.com/logo/boeing.com"
    assert twelve.lookup_identifier == "BA@NYSE"
    assert twelve_calls[0].startswith("https://api.twelvedata.com/logo?")
    assert "symbol=BA" in twelve_calls[0] and "exchange=NYSE" in twelve_calls[0]
    assert twelve_calls[1] == "https://api.twelvedata.com/logo/boeing.com"


def test_logo_source_falls_through_finnhub_to_twelve_data_without_logo_dev(tmp_path: Path) -> None:
    db = _store_with_logo_schema(tmp_path)
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("finnhub", {"api_key": "FH_TEST"}))
    credentials.save(ProviderCredentials("twelve_data", {"api_key": "TD_TEST"}))

    class Missing:
        calls = 0

        def fetch(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.calls += 1
            raise LogoProviderError("not_found", "deterministic miss", retryable=False)

    class Available:
        calls = 0

        def fetch(self, *args, **kwargs):  # noqa: ANN002, ANN003
            from app.company_logos.provider import LogoFetchResponse

            self.calls += 1
            return LogoFetchResponse(_ONE_PIXEL_PNG, "image/png", "https://api.twelvedata.com/logo", "BA")

    missing = Missing(); available = Available(); logo_dev = _CountingClient()
    result = CompanyLogoService(
        db, credentials, client=logo_dev, finnhub_client=missing, twelve_data_client=available
    ).resolve("BA", "NYSE")
    assert result.has_image and result.provider_id == "twelve_data_logo"
    assert missing.calls == 1 and available.calls == 1 and logo_dev.calls == []
    raw_database = db.read_bytes()
    assert _ONE_PIXEL_PNG not in raw_database
    assert b"TD_TEST" not in raw_database


def test_negative_retry_metadata_blocks_repeat_network_calls(tmp_path: Path) -> None:
    from app.company_logos.provider import LogoProviderError

    db = _store_with_logo_schema(tmp_path)
    credentials = InMemoryCredentialStore()
    credentials.save(ProviderCredentials("logo_dev", {"publishable_key": "pk_test"}))
    now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)

    class MissingClient:
        provider_id = "logo_dev"

        def __init__(self):
            self.calls = 0

        def fetch(self, symbol, exchange, publishable_key, *, theme="dark"):  # noqa: ARG002
            self.calls += 1
            raise LogoProviderError("not_found", "No logo.", retryable=False)

    client = MissingClient()
    service = CompanyLogoService(db, credentials, client=client, now_fn=lambda: now)
    first = service.resolve("ZZZZ")
    second = service.resolve("ZZZZ")
    assert first.status is CompanyLogoStatus.NOT_FOUND
    assert second.status is CompanyLogoStatus.NOT_FOUND
    assert client.calls == 1
    assert second.retry_after and second.retry_after > now


def test_populated_v2_database_upgrade_preserves_history_discovery_fundamentals_and_external_state(
    tmp_path: Path,
) -> None:
    db = tmp_path / "history.sqlite"
    external = {
        "watchlists.json": b'{"My Watchlist":["AAPL","NVDA"]}\n',
        "settings.json": b'{"theme":"dark","default_provider":"yahoo"}\n',
    }
    for name, payload in external.items():
        (tmp_path / name).write_bytes(payload)

    store = HistoricalStore(db)
    con = store._con
    now = "2026-08-19T14:00:00+00:00"
    con.execute(
        "INSERT INTO instruments(symbol, exchange, provider, currency) VALUES(?,?,?,?)",
        ("AAPL", "NASDAQ", "yahoo", "USD"),
    )
    con.execute(
        """INSERT INTO ohlcv_bars(
               symbol, provider, bar_date, open, high, low, close, volume,
               adjusted, exchange, source, provider_timestamp, source_timezone
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("AAPL", "yahoo", "2026-08-18", 1.0, 2.0, 0.5, 1.5, 100, 0, "NASDAQ", "fixture", now, "UTC"),
    )
    instrument_id = con.execute(
        """INSERT INTO rs_instruments(
               canonical_symbol, security_name, asset_class, primary_venue,
               first_seen_utc, last_seen_utc, created_at_utc, updated_at_utc
           ) VALUES(?,?,?,?,?,?,?,?)""",
        ("AAPL", "Apple Inc.", "equity", "NASDAQ", now, now, now, now),
    ).lastrowid
    con.execute(
        """INSERT INTO rs_discovery_sources(
               source_id, display_name, source_kind, enabled, created_at_utc, updated_at_utc
           ) VALUES(?,?,?,?,?,?)""",
        ("nasdaq-listed", "Nasdaq Listed", "official_directory", 1, now, now),
    )
    con.execute(
        """INSERT INTO rs_discovery_runs(
               source_id, started_at_utc, completed_at_utc, status, rows_seen
           ) VALUES(?,?,?,?,?)""",
        ("nasdaq-listed", now, now, "success", 1),
    )
    con.execute(
        """INSERT INTO rs_fundamental_facts(
               instrument_id, taxonomy, concept, unit, value_text,
               source_id, created_at_utc
           ) VALUES(?,?,?,?,?,?,?)""",
        (instrument_id, "us-gaap", "Assets", "USD", "100", "sec", now),
    )
    con.execute("DROP INDEX IF EXISTS idx_rs_company_logo_retry")
    con.execute("DROP TABLE IF EXISTS rs_company_logo_state")
    con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    con.commit()
    store.close()

    upgraded = HistoricalStore(db)
    try:
        assert upgraded._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        assert upgraded.database_checks() == {"integrity_check": "ok", "foreign_key_violations": []}
        assert upgraded._con.execute("SELECT COUNT(*) FROM ohlcv_bars").fetchone()[0] == 1
        assert upgraded._con.execute("SELECT COUNT(*) FROM rs_instruments").fetchone()[0] == 1
        assert upgraded._con.execute("SELECT COUNT(*) FROM rs_discovery_runs").fetchone()[0] == 1
        assert upgraded._con.execute("SELECT COUNT(*) FROM rs_fundamental_facts").fetchone()[0] == 1
        columns = {row[1].lower() for row in upgraded._con.execute("PRAGMA table_info(rs_company_logo_state)")}
        assert not any(marker in column for column in columns for marker in ("image", "blob", "key", "token"))
        assert upgraded._con.execute("SELECT COUNT(*) FROM rs_company_logo_state").fetchone()[0] == 0
    finally:
        upgraded.close()

    for name, payload in external.items():
        assert (tmp_path / name).read_bytes() == payload


def test_ui_source_wires_logo_to_market_research_and_settings() -> None:
    source = (Path(__file__).parents[2] / "app" / "ui" / "main.py").read_text(encoding="utf-8")
    assert "self.market_symbol_avatar" in source
    assert "self.research_symbol_avatar" in source
    assert "_request_company_logo" in source
    assert "Logo.dev publishable key" in source
    assert 'provider_configuration.save_credentials("logo_dev"' in source
    assert "RangeScout 1.6.4" in source
    assert "RangeScout 1.3.0" not in source


@pytest.fixture
def logo_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    if QApplication is None:
        pytest.skip("PySide6 is not installed")

    class Adapter:
        def __init__(self, root: Path) -> None:
            self.app_name = "RangeScout"
            self.app_data_dir = self.config_dir = self.temp_dir = str(root)
            self.allow_user_install_paths = []

    root = tmp_path / "RangeScout"
    root.mkdir()
    adapter = Adapter(root)
    module = importlib.import_module("app.ui.main")
    monkeypatch.setattr(module, "platform_adapter", lambda: adapter)
    monkeypatch.setattr(module, "QThreadPool", None)
    credentials = InMemoryCredentialStore()
    application = RangeScoutApplication(
        data_dir=root,
        credential_store=credentials,
        registry=build_test_provider_registry(credentials),
    )
    qt = QApplication.instance() or QApplication([])
    window = module.build_window(application=application, auto_refresh=False)
    window.live_refresh_timer.stop()
    try:
        yield window, credentials, module
    finally:
        window.app.store.close()
        window._qt_window.close()
        if qt is not None:
            qt.processEvents()


def _available_asset(symbol: str, provider_id: str = "logo_dev") -> CompanyLogoAsset:
    return CompanyLogoAsset(
        symbol=symbol,
        exchange="NASDAQ",
        provider_id=provider_id,
        status=CompanyLogoStatus.AVAILABLE,
        image_bytes=_ONE_PIXEL_PNG,
        content_type="image/png",
    )


def test_market_and_research_begin_with_monogram_and_accept_same_pixmap(logo_window) -> None:
    window, _credentials, _module = logo_window
    window._set_company_logo_placeholder("AAPL")
    assert window.market_symbol_avatar.text() == "AAPL"
    assert window.research_symbol_avatar.text() == "AAPL"
    assert window.market_symbol_avatar.pixmap().isNull()
    assert window.research_symbol_avatar.pixmap().isNull()

    window._apply_company_logo_asset(_available_asset("AAPL"))
    assert window.market_symbol_avatar.text() == ""
    assert window.research_symbol_avatar.text() == ""
    assert not window.market_symbol_avatar.pixmap().isNull()
    assert not window.research_symbol_avatar.pixmap().isNull()


def test_twelve_data_logo_has_compact_visible_attribution_on_both_surfaces(logo_window) -> None:
    window, _credentials, _module = logo_window
    window._qt_window.show()
    window._apply_company_logo_asset(_available_asset("AAPL", "twelve_data_logo"))
    assert window.market_logo_attribution.text() == "Logo: Twelve Data"
    assert window.research_logo_attribution.text() == "Logo: Twelve Data"
    assert window.market_logo_attribution.isVisible()
    window.tabs.setCurrentIndex(2)
    assert window.research_logo_attribution.isVisible()

    window._apply_company_logo_asset(_available_asset("AAPL", "logo_dev"))
    assert window.market_logo_attribution.isHidden()
    assert window.research_logo_attribution.isHidden()


def test_stale_logo_result_cannot_overwrite_new_active_symbol(logo_window) -> None:
    window, _credentials, _module = logo_window
    old_request = window.active_symbol.request(source="company-logo")
    window.set_active_symbol("NVDA", source="ticker")
    assert window.market_symbol_avatar.text() == "NVDA"
    assert window.research_symbol_avatar.text() == "NVDA"

    window._on_company_logo_finished(old_request, _available_asset("AAPL"), None)
    assert window.current_symbol == "NVDA"
    assert window.market_symbol_avatar.text() == "NVDA"
    assert window.research_symbol_avatar.text() == "NVDA"


def test_logo_key_save_delete_updates_secure_status_without_settings_leak(logo_window) -> None:
    window, credentials, _module = logo_window
    secret = "pk_UI_PRIVATE_TEST_123456789"
    window.logo_dev_publishable_key_input.setText(secret)
    window._on_save_company_logo_key()
    assert credentials.load("logo_dev").values["publishable_key"] == secret
    assert window.logo_dev_publishable_key_input.text() == ""
    assert window.company_logo_status_text.text().startswith("Configured")
    assert secret not in (Path(window.app.data_dir) / "settings.json").read_text(encoding="utf-8")

    window._on_delete_company_logo_key()
    assert credentials.load("logo_dev") is None
    assert window.company_logo_status_text.text().startswith("Optional")
    assert window.market_symbol_avatar.text() == window.current_symbol


def test_theme_changes_schedule_at_most_one_logo_request_per_symbol_theme(logo_window, monkeypatch: pytest.MonkeyPatch) -> None:
    window, _credentials, module = logo_window
    scheduled = []

    class HoldingPool:
        def start(self, task):
            scheduled.append(task)

    class HoldingThreadPool:
        @staticmethod
        def globalInstance():
            return HoldingPool()

    monkeypatch.setattr(module, "QThreadPool", HoldingThreadPool)
    window.theme_combo.setCurrentText("dark")
    window._apply_theme("dark")
    window.theme_combo.setCurrentText("light")
    window._apply_theme("light")
    assert len(scheduled) == 2
    assert {record[1][2] for record in window._company_logo_tasks.values()} == {"dark", "light"}
