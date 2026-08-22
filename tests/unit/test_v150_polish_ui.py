from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.application.live_trading_runtime import LiveSymbolState
from app.models.schemas import AssetType, DataDelay, Instrument, InstrumentIdentifier, OhlcvBar, QuoteSnapshot
from app.security.credentials import InMemoryCredentialStore
from tests.fakes.mock_provider import build_test_provider_registry
from app.streaming.ticker import plan_ticker_subscriptions

try:
    from PySide6.QtWidgets import QApplication
except Exception:
    QApplication = None


@pytest.fixture
def polish_window(tmp_path: Path):
    if QApplication is None:
        pytest.skip("PySide6 is not installed")
    QApplication.instance() or QApplication([])
    credentials = InMemoryCredentialStore()
    app = RangeScoutApplication(
        data_dir=tmp_path / "RangeScout", credential_store=credentials,
        registry=build_test_provider_registry(credentials),
    )
    app.start_background_services = lambda: None
    from app.ui.main import build_window

    window = build_window(application=app, auto_refresh=False)
    window.live_refresh_timer.stop()
    try:
        yield window
    finally:
        window._shutdown_runtime()
        window._qt_window.close()


def _quote(symbol: str, price: str, previous: str, *, company: str | None = None) -> QuoteSnapshot:
    return QuoteSnapshot(
        Instrument(InstrumentIdentifier(symbol, "NYSE"), company or f"{symbol} Company", AssetType.STOCK, provider="fake"),
        Decimal(price), Decimal(previous), 1000, datetime.now(timezone.utc), datetime.now(timezone.utc),
        DataDelay.DELAYED, 900,
    )


def test_symbol_commit_clears_every_old_symbol_surface_immediately(polish_window) -> None:
    window = polish_window
    window._apply_quote_success(_quote("AAPL", "184.72", "182.54", company="Apple Inc."))
    window.current_bars = [
        OhlcvBar(InstrumentIdentifier("AAPL"), date(2026, 8, 19), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("2"), 10, "fake")
    ]
    window.research_about_text.setText("AAPL research value")
    window.catalyst_list.clear(); window.catalyst_list.addItem("AAPL catalyst")
    began = perf_counter()
    window.set_active_symbol("NVDA", source="search")
    elapsed_ms = (perf_counter() - began) * 1000
    assert elapsed_ms < 100
    assert window.current_quote is None and window.current_bars == []
    assert "Loading NVDA" in window.price_text.text()
    assert "AAPL" not in window.metrics_text.text()
    assert "AAPL" not in window.research_about_text.text()
    assert "AAPL" not in window.catalyst_list.item(0).text()
    assert window.market_symbol_avatar.text() == "NVDA"


def test_warm_symbol_cache_renders_meaningfully_under_one_second(polish_window) -> None:
    window = polish_window
    cached_quote = _quote("BA", "215.10", "222.20", company="The Boeing Company")
    bars = (
        OhlcvBar(InstrumentIdentifier("BA", "NYSE"), date(2026, 8, 18), Decimal("220"), Decimal("223"), Decimal("218"), Decimal("222.20"), 100, "fake"),
        OhlcvBar(InstrumentIdentifier("BA", "NYSE"), date(2026, 8, 19), Decimal("222"), Decimal("224"), Decimal("214"), Decimal("215.10"), 120, "fake"),
    )
    window._symbol_snapshot_cache["BA"] = (cached_quote, bars, datetime.now(timezone.utc))
    began = perf_counter()
    window.set_active_symbol("BA", source="recent-symbol")
    elapsed_ms = (perf_counter() - began) * 1000
    assert elapsed_ms < 1000
    assert window.current_quote is cached_quote and window.current_bars == list(bars)
    assert "▼" in window.price_text.text() and "215.10" in window.price_text.text()
    assert "Cached" in window.shell_freshness_text.text()


def test_sqlite_cached_symbol_renders_before_network(polish_window) -> None:
    window = polish_window
    bars = [
        OhlcvBar(InstrumentIdentifier("MSFT", "NASDAQ"), date(2026, 8, 18), Decimal("500"), Decimal("505"), Decimal("499"), Decimal("503"), 100, "fake"),
        OhlcvBar(InstrumentIdentifier("MSFT", "NASDAQ"), date(2026, 8, 19), Decimal("503"), Decimal("510"), Decimal("502"), Decimal("509"), 120, "fake"),
    ]
    window.app.store.upsert_bars(bars, "fake")
    began = perf_counter()
    window.set_active_symbol("MSFT", source="watchlist")
    assert (perf_counter() - began) < 1.0
    assert window.current_quote is not None and window.current_quote.instrument.identifier.symbol == "MSFT"
    assert "Cached" in window.shell_freshness_text.text()


def test_system_theme_changes_live_and_explicit_preferences_override(polish_window) -> None:
    window = polish_window
    window._apply_theme("system", system_scheme="ColorScheme.Dark", persist=False)
    assert window._effective_theme == "dark"
    window._on_system_color_scheme_changed("ColorScheme.Light")
    assert window._effective_theme == "light"
    window._apply_theme("dark", system_scheme="ColorScheme.Light", persist=False)
    assert window._effective_theme == "dark"
    window._apply_theme("light", system_scheme="ColorScheme.Dark", persist=False)
    assert window._effective_theme == "light"


def test_directional_price_does_not_color_company_identity(polish_window) -> None:
    window = polish_window
    window._apply_quote_success(_quote("NVDA", "180", "175", company="NVIDIA Corporation"))
    assert "▲" in window.price_text.text()
    assert "#22c55e" in window.price_text.styleSheet().lower()
    assert window.market_company_text.text() == "NVDA  •  NVIDIA Corporation  •  Sector N/A"
    assert "color" not in window.market_company_text.styleSheet().lower()
    assert "+5.00" not in window.market_change_text.text()

    window._apply_quote_success(_quote("NVDA", "170", "175", company="NVIDIA Corporation"))
    assert "▼" in window.price_text.text()
    assert "#f05252" in window.price_text.styleSheet().lower()
    assert "-5.00" not in window.market_change_text.text()

    window._apply_quote_success(_quote("NVDA", "175", "175", company="NVIDIA Corporation"))
    assert window.price_text.text().startswith("—")
    assert window.price_text.property("priceDirection") == "flat"
    assert "#22c55e" not in window.price_text.styleSheet().lower()
    assert "#f05252" not in window.price_text.styleSheet().lower()


def test_extended_hours_are_labeled_separately_from_regular_change(polish_window) -> None:
    window = polish_window
    base = _quote("BA", "215.10", "222.20", company="The Boeing Company")
    from dataclasses import replace

    quote = replace(
        base,
        pre_market_price=Decimal("216.00"), pre_market_change=Decimal("0.90"),
        pre_market_change_percent=Decimal("0.42"), after_hours_price=Decimal("214.50"),
        after_hours_change=Decimal("-0.60"), after_hours_change_percent=Decimal("-0.28"),
    )
    window._apply_quote_success(quote)
    assert "PRE-MARKET" in window.extended_hours_text.text()
    assert "AFTER HOURS" in window.extended_hours_text.text()
    assert "▼" in window.price_text.text()


def test_ticker_identity_stays_neutral_while_value_is_directional(polish_window) -> None:
    window = polish_window
    symbols = ["BA", "NVDA"]
    window._ticker_watchlist_symbols = symbols
    states = {
        "BA": LiveSymbolState("BA", price=Decimal("101"), previous_close=Decimal("100")),
        "NVDA": LiveSymbolState("NVDA", price=Decimal("99"), previous_close=Decimal("100")),
    }
    window._render_ticker_ribbon(states, plan_ticker_subscriptions(symbols, None))
    assert [window._ticker_identity_labels[symbol].text() for symbol in symbols] == symbols
    assert all(window._ticker_identity_labels[symbol].property("identityNeutral") is True for symbol in symbols)
    assert all(window._ticker_buttons[symbol].property("tickerDirection") is None for symbol in symbols)
    assert window._ticker_value_labels["BA"].property("tickerDirection") == "up"
    assert window._ticker_value_labels["NVDA"].property("tickerDirection") == "down"
    assert "▲" in window._ticker_value_labels["BA"].text()
    assert "▼" in window._ticker_value_labels["NVDA"].text()
    stylesheet = window._workstation_stylesheet(True)
    assert 'QLabel#ticker_value[tickerDirection="up"]' in stylesheet
    assert 'QLabel#ticker_value[tickerDirection="down"]' in stylesheet


def test_provider_diagnostics_are_tucked_away_until_requested(polish_window, monkeypatch) -> None:
    window = polish_window
    assert not window.provider_diagnostics_text.isVisible()
    monkeypatch.setattr(
        window.app.market_data_router,
        "diagnostics",
        lambda: {"winning_provider": "yahoo", "latency_ms": 42.5, "provider_timestamp": "2026-08-20T12:00:00Z", "cache": "hit", "circuit_state": "closed", "rate_limit_state": "available", "delay_class": "delayed", "fallback_reason": None},
    )
    window._toggle_provider_details()
    assert "winner yahoo" in window.provider_diagnostics_text.text()
    assert "cache hit" in window.provider_diagnostics_text.text()


def test_non_sensitive_ui_state_persists_without_credentials(polish_window, tmp_path: Path) -> None:
    window = polish_window
    window.tabs.setCurrentIndex(2)
    window.research_period_combo.setCurrentIndex(window.research_period_combo.findData("quarterly"))
    window.watchlist_id_input.setText("swing")
    window._persist_ui_state()
    raw = (Path(window.app.data_dir) / "settings.json").read_text(encoding="utf-8")
    assert '"last_page": 2' in raw and '"research_period": "quarterly"' in raw
    assert '"selected_watchlist": "swing"' in raw
    assert not any(token in raw.lower() for token in ("api_key", "publishable_key", "secret_key", "token"))


def test_settings_controls_shortcuts_recent_history_and_database_health(polish_window) -> None:
    window = polish_window
    assert window.update_company_database_btn.text() == "Update Company Database"
    assert window.refresh_company_logos_btn.text() == "Refresh Logos"
    assert window.company_update_schedule_combo.findData("weekly") >= 0
    assert window.logo_refresh_schedule_combo.findData("monthly") >= 0
    assert len(window._shortcuts) == 11
    window.set_active_symbol("BA", source="search")
    window.set_active_symbol("NVDA", source="ticker")
    assert window.recent_symbols.values[:2] == ("NVDA", "BA")
    window._on_check_local_database()
    assert window.database_health_text.text().startswith("Healthy")
    window._on_clear_recent_symbols()
    assert window.recent_symbols.values == ()


def test_settings_shows_distinct_company_and_logo_schedule_timestamps(polish_window, monkeypatch) -> None:
    window = polish_window
    monkeypatch.setattr(
        window.app,
        "company_database_status",
        lambda: {
            "total_instruments": 10,
            "logo_coverage": 4,
            "current_update_status": "complete",
            "companies_added": 1,
            "companies_changed": 2,
            "inactive_or_delisted": 0,
            "aliases_or_symbol_changes": 1,
            "logo_successes": 4,
            "logo_failures": 0,
            "source_failures": 0,
            "schedule": {
                "company_metadata": {
                    "last_success_utc": "2026-08-20T12:00:00+00:00",
                    "next_due_utc": "2026-08-27T12:00:00+00:00",
                },
                "logos": {
                    "last_success_utc": "2026-08-01T12:00:00+00:00",
                    "next_due_utc": "2026-08-31T12:00:00+00:00",
                },
            },
        },
    )
    window._refresh_company_database_status()
    text = window.company_database_status_text.text()
    assert "Company: last 2026-08-20" in text and "next 2026-08-27" in text
    assert "Logos: last 2026-08-01" in text and "next 2026-08-31" in text
