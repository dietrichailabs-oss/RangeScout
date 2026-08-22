from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from concurrent.futures import Future

from app.application.recent_symbols import RecentSymbols
from app.company_data.repository import CompanyDatabaseRepository
from app.company_data.maintenance import CompanyMaintenanceService
from app.company_data.scheduler import CompanyUpdateSchedule, is_update_due, next_update_at
from app.company_logos.sources import LOGO_SOURCE_ORDER
from app.configuration.settings import AppSettings, export_safe_settings, import_safe_settings
from app.historical_store.migrations import CURRENT_SCHEMA_VERSION
from app.historical_store.repository import HistoricalStore
from app.ui.presentation import directional_price, freshness_label
from app.ui.theme import resolve_effective_theme


def test_schema_v5_preserves_company_identity_and_adds_logo_provenance(tmp_path: Path) -> None:
    db = tmp_path / "history.sqlite"
    store = HistoricalStore(db)
    now = "2026-08-20T12:00:00+00:00"
    store._con.execute(
        """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,primary_venue,cik,
           first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
           VALUES('BA','The Boeing Company','equity','NYSE','0000012927',?,?,?,?)""",
        (now, now, now, now),
    )
    instrument_id = store._con.execute("SELECT instrument_id FROM rs_instruments WHERE canonical_symbol='BA'").fetchone()[0]
    store._con.execute(
        "INSERT INTO rs_instrument_aliases(instrument_id,alias_symbol,venue,alias_kind,created_at_utc) VALUES(?,?,?,?,?)",
        (instrument_id, "BOE", "NYSE", "previous_symbol", now),
    )
    store._con.commit()
    assert store._con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
    columns = {row[1] for row in store._con.execute("PRAGMA table_info(rs_instruments)")}
    assert {"logo_source_id", "logo_lookup_identifier", "logo_content_sha256", "logo_license_metadata", "logo_next_refresh_utc", "logo_failure_count"} <= columns
    assert "image_bytes" not in columns
    store.close()

    repository = CompanyDatabaseRepository(db)
    record = repository.resolve("BOE")
    assert record is not None
    assert record.canonical_symbol == "BA" and record.cik == "0000012927" and "BOE" in record.aliases
    assert repository.record_logo_result(
        "BA", source_id="wikimedia_commons", lookup_identifier="Q66",
        source_url="https://commons.wikimedia.org/wiki/File:Boeing.svg", content_sha256="A" * 64,
        license_metadata="CC BY-SA 4.0; author and attribution retained", local_path="logos/BA.svg",
        success=True, next_refresh_utc=datetime.now(timezone.utc) + timedelta(days=30),
    )
    updated = repository.resolve("BA")
    assert updated is not None and updated.logo_source_id == "wikimedia_commons"
    assert updated.local_logo_path == "logos/BA.svg"
    assert repository.health() == {"healthy": True, "integrity_check": "ok", "foreign_key_violations": []}


def test_company_and_logo_update_schedules_are_explicit() -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert next_update_at(now, CompanyUpdateSchedule.WEEKLY) == now + timedelta(days=7)
    assert next_update_at(now, CompanyUpdateSchedule.MONTHLY) == now + timedelta(days=30)
    assert next_update_at(now, CompanyUpdateSchedule.OFF) is None
    assert is_update_due(now - timedelta(days=8), CompanyUpdateSchedule.WEEKLY, now)
    assert not is_update_due(now - timedelta(days=8), CompanyUpdateSchedule.OFF, now)


def test_logo_source_order_is_legitimate_and_persistence_is_fail_closed() -> None:
    assert [item.source_id for item in LOGO_SOURCE_ORDER] == [
        "local_permitted", "finnhub_profile", "twelve_data_logo", "logo_dev", "wikimedia_commons", "simple_icons", "ticker_monogram"
    ]
    assert all(item.official_url.startswith(("https://", "local://")) for item in LOGO_SOURCE_ORDER)
    assert not next(item for item in LOGO_SOURCE_ORDER if item.source_id == "finnhub_profile").persistent_image_cache_permitted
    assert not next(item for item in LOGO_SOURCE_ORDER if item.source_id == "twelve_data_logo").persistent_image_cache_permitted
    assert not any(host in item.official_url for item in LOGO_SOURCE_ORDER for host in ("google.com/search", "finance.yahoo.com", "msn.com"))


def test_system_theme_resolution_and_live_inputs() -> None:
    assert resolve_effective_theme("system", qt_color_scheme="ColorScheme.Dark") == "dark"
    assert resolve_effective_theme("system", qt_color_scheme="ColorScheme.Light") == "light"
    assert resolve_effective_theme("system", windows_light_reader=lambda: 0) == "dark"
    assert resolve_effective_theme("system", windows_light_reader=lambda: 1) == "light"
    assert resolve_effective_theme("light", qt_color_scheme="dark") == "light"
    assert resolve_effective_theme("dark", qt_color_scheme="light") == "dark"


def test_directional_price_uses_arrow_and_keeps_identity_out_of_presentation() -> None:
    up = directional_price(Decimal("184.72"), Decimal("182.54"), "USD")
    down = directional_price(Decimal("181.03"), Decimal("182.54"), "USD")
    flat = directional_price(Decimal("10"), Decimal("10"), "USD")
    assert up.arrow == "▲" and up.direction == "up" and "+2.18" in up.text
    assert down.arrow == "▼" and down.direction == "down" and "-1.51" in down.text
    assert flat.arrow == "—" and flat.direction == "flat"
    assert all("company" not in value.text.lower() for value in (up, down, flat))


def test_freshness_labels_never_call_cached_data_live() -> None:
    now = datetime(2026, 8, 20, 12, 10, tzinfo=timezone.utc)
    assert freshness_label(freshness="cached", delay="delayed", received_at=now - timedelta(minutes=4), now=now) == "Cached 4m"
    assert freshness_label(freshness="offline", delay="offline", received_at=now, now=now) == "Offline"
    assert freshness_label(freshness="live", delay="realtime", received_at=now, now=now) == "Live"


def test_recent_symbols_are_local_bounded_deduplicated_and_newest_first() -> None:
    history = RecentSymbols(("AAPL", "MSFT"), limit=3)
    history.add("NVDA"); history.add("AAPL"); history.add("TSLA")
    assert history.values == ("TSLA", "AAPL", "NVDA")
    history.clear()
    assert history.values == ()


def test_settings_export_import_excludes_all_credentials(tmp_path: Path) -> None:
    settings = AppSettings(theme="system", recent_symbols=("BA", "NVDA"), company_update_schedule="weekly")
    export_path = tmp_path / "preferences.json"
    export_safe_settings(export_path, settings)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    text = json.dumps(payload).lower()
    assert payload["schema"] == "rangescout-safe-preferences-v1"
    assert not any(token in text for token in ("api_key", "publishable_key", "secret_key", "token"))
    payload["preferences"]["api_key"] = "must-not-import"
    export_path.write_text(json.dumps(payload), encoding="utf-8")
    imported = import_safe_settings(export_path, AppSettings(theme="dark"))
    assert imported.theme == "system" and imported.recent_symbols == ("BA", "NVDA")
    assert "must-not-import" not in repr(imported)


def test_company_maintenance_runs_incrementally_in_background(tmp_path: Path) -> None:
    db = tmp_path / "history.sqlite"
    store = HistoricalStore(db)
    now = datetime.now(timezone.utc).isoformat()
    store._con.execute(
        """INSERT INTO rs_instruments(canonical_symbol,security_name,asset_class,primary_venue,
           first_seen_utc,last_seen_utc,created_at_utc,updated_at_utc)
           VALUES('BA','Boeing','equity','NYSE',?,?,?,?)""",
        (now, now, now, now),
    )
    store._con.commit(); store.close()

    class Discovery:
        def refresh_manual(self):
            future = Future(); future.set_exception(RuntimeError("offline fixture")); return future

    class Asset:
        has_image = True

    class Logos:
        def resolve(self, symbol, venue, force=False):  # noqa: ANN001, ARG002
            return Asset()

    repository = CompanyDatabaseRepository(db)
    service = CompanyMaintenanceService(repository, Discovery(), Logos())
    try:
        began = datetime.now(timezone.utc)
        future = service.refresh_logos(limit=1)
        assert (datetime.now(timezone.utc) - began).total_seconds() < 0.25
        assert future.result(timeout=2) == {"attempted": 1, "successes": 1, "failures": 0}
        assert repository.status().logo_successes == 1
        failed = service.refresh_companies()
        assert failed.done()
        assert repository.status().source_failures == 1
    finally:
        service.shutdown()
