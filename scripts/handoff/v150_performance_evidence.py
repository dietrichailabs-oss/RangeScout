#!/usr/bin/env python
"""Deterministic UI-path timing evidence for the RangeScout 1.5.0 gate."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from statistics import median
import sys
import tempfile
from threading import get_ident
from time import perf_counter, sleep

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.application.bootstrap import RangeScoutApplication
from app.models.schemas import AssetType, DataDelay, Instrument, InstrumentIdentifier, OhlcvBar, QuoteSnapshot
from app.research.models import CompanyProfile, ResearchSnapshot
from app.security.credentials import InMemoryCredentialStore
from app.ui.main import build_window
from tests.fakes.mock_provider import build_test_provider_registry
from tests.unit.test_local_first_eng4 import HostileMarketData

from PySide6.QtWidgets import QApplication


def elapsed_ms(operation) -> float:
    began = perf_counter()
    operation()
    return (perf_counter() - began) * 1000.0


def quote(symbol: str, price: str, previous: str) -> QuoteSnapshot:
    now = datetime.now(timezone.utc)
    return QuoteSnapshot(
        Instrument(InstrumentIdentifier(symbol, "NYSE"), f"{symbol} Company", AssetType.STOCK, provider="deterministic"),
        Decimal(price), Decimal(previous), 1000, now, now, DataDelay.DELAYED, 900,
    )


def bars(symbol: str) -> tuple[OhlcvBar, ...]:
    return (
        OhlcvBar(InstrumentIdentifier(symbol, "NYSE"), date(2026, 8, 18), Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"), 100, "deterministic"),
        OhlcvBar(InstrumentIdentifier(symbol, "NYSE"), date(2026, 8, 19), Decimal("101"), Decimal("104"), Decimal("100"), Decimal("103"), 120, "deterministic"),
    )


def run(output: Path) -> dict[str, object]:
    application = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="rangescout-v160-performance-", ignore_cleanup_errors=True) as folder:
        credentials = InMemoryCredentialStore()
        app = RangeScoutApplication(
            data_dir=Path(folder) / "RangeScout", credential_store=credentials,
            registry=build_test_provider_registry(credentials),
        )
        app.start_background_services = lambda: None
        app.local_snapshots.save_quote(quote("AAPL", "184.72", "182.54"), "local-cache")
        app.local_snapshots.save_quote(quote("BA", "215.10", "222.20"), "local-cache")
        app.store.upsert_bars(bars("BA"), "deterministic")

        class BlockedMarketData(HostileMarketData):
            def fetch_historical(self, identifier, start=None, end=None):  # noqa: ARG002
                self.history_calls += 1
                self.thread_ids.append(get_ident())
                self.history_started.set()
                self.history_finished.set()
                raise OSError("all providers blocked")

        blocked = BlockedMarketData(fail_quote=True)
        app.market_data_service = blocked
        startup_began = perf_counter()
        window = build_window(application=app, auto_refresh=True)
        blocked_startup_ms = (perf_counter() - startup_began) * 1000.0
        window.live_refresh_timer.stop()
        try:
            deadline = perf_counter() + 1.0
            while len(blocked.thread_ids) < 2 and perf_counter() < deadline:
                application.processEvents(); sleep(0.005)
            application.processEvents()
            blocked_calls_off_ui = bool(blocked.thread_ids) and all(
                thread_id != get_ident() for thread_id in blocked.thread_ids
            )

            window._auto_network_refresh = False
            identity_samples = [elapsed_ms(lambda: window.set_active_symbol(symbol, source="performance")) for symbol in ("NVDA", "MSFT", "BA", "RTX")]

            warm_render = elapsed_ms(lambda: window.set_active_symbol("BA", source="performance-warm"))
            cached = window.current_quote
            cached_bars = tuple(window.current_bars)
            market_populated = elapsed_ms(lambda: window._apply_quote_success(cached, from_cache=True)) if cached else 999999.0
            chart_render = elapsed_ms(lambda: window._apply_bars_to_charts(list(cached_bars)))

            # A history request advertises a 30-second stall, while the quote
            # completes independently. The gate observes the quote before the
            # history release event is set.
            from threading import Event
            history_release = Event()
            hostile = HostileMarketData(hang_history=history_release)
            window.market_data = hostile
            window._auto_network_refresh = True
            quote_began = perf_counter()
            window.set_active_symbol("AAPL", source="performance-hung-history")
            history_started = hostile.history_started.wait(timeout=1.0)
            deadline = quote_began + 1.0
            while (window.current_quote is None or window.current_quote.instrument.identifier.symbol != "AAPL") and perf_counter() < deadline:
                application.processEvents(); sleep(0.005)
            quote_while_history_hung_ms = (perf_counter() - quote_began) * 1000.0
            quote_won_while_history_hung = (
                window.current_quote is not None
                and window.current_quote.instrument.identifier.symbol == "AAPL"
                and not hostile.history_finished.is_set()
                and history_started
            )
            history_release.set()
            deadline = perf_counter() + 1.0
            while not hostile.history_finished.is_set() and perf_counter() < deadline:
                application.processEvents(); sleep(0.005)

            window.market_data = blocked
            window._auto_network_refresh = False
            offline_cached_ms = elapsed_ms(lambda: window.set_active_symbol("BA", source="performance-offline"))
            offline_cached_visible = window.current_quote is not None and "Cached" in window.shell_freshness_text.text()
            cold_known_ms = elapsed_ms(lambda: window.set_active_symbol("MSFT", source="performance-cold-known"))
            cold_known_identity = "Microsoft Corporation" in window.market_company_text.text()

            snapshot = ResearchSnapshot(
                "BA", window.active_symbol.state.generation,
                CompanyProfile("BA", "0000012927", "The Boeing Company", "NYSE", "3721", "Aircraft"),
                {}, datetime.now(timezone.utc),
            )
            research_render = elapsed_ms(lambda: window._apply_research_snapshot(snapshot))

            cold_start = elapsed_ms(lambda: window.set_active_symbol("UNSEEN", source="performance-cold"))
            fresh_completion = elapsed_ms(lambda: window._apply_quote_success(quote("UNSEEN", "10.00", "9.50")))
            db_update_submit = elapsed_ms(lambda: window.app.refresh_company_logos())
            sqlite_report = app.local_snapshots.index_report()
            application.processEvents()
        finally:
            window._shutdown_runtime()
            window._qt_window.close()

    result = {
        "schema": "rangescout.performance.v1.6.0",
        "profile_before_optimization": {
            "finding": "The 1.4.1 committed-symbol path cleared only identity/Research state and the manual history refresh performed provider work synchronously on the UI caller.",
            "optimization_targets": ["synchronous old-symbol clear", "bounded in-memory cache", "SQLite cached bars", "generation-safe background refresh", "shared chart payload"],
        },
        "measurements_ms": {
            "all_network_blocked_startup_interactive": round(blocked_startup_ms, 3),
            "symbol_selection_to_identity_clear_median": round(median(identity_samples), 3),
            "symbol_selection_to_identity_clear_max": round(max(identity_samples), 3),
            "warm_symbol_first_cached_render": round(warm_render, 3),
            "warm_symbol_meaningful_market_populated": round(market_populated, 3),
            "warm_symbol_research_cached_populated": round(research_render, 3),
            "chart_cached_render": round(chart_render, 3),
            "fresh_provider_completion_ui_apply": round(fresh_completion, 3),
            "cold_unseen_symbol_loading_state": round(cold_start, 3),
            "cold_known_company_identity": round(cold_known_ms, 3),
            "offline_cached_symbol": round(offline_cached_ms, 3),
            "quote_while_history_advertises_30_second_stall": round(quote_while_history_hung_ms, 3),
            "database_update_background_submit": round(db_update_submit, 3),
        },
        "targets": {
            "all_network_blocked_startup_under_1s": blocked_startup_ms <= 1000.0,
            "identity_clear_under_100ms": max(identity_samples) < 100.0,
            "warm_cached_under_1s": warm_render <= 1000.0,
            "ui_thread_network_calls_zero": blocked_calls_off_ui,
            "quote_while_history_hung": quote_won_while_history_hung and quote_while_history_hung_ms <= 1000.0,
            "offline_cached_data_visible": offline_cached_visible and offline_cached_ms <= 1000.0,
            "cold_known_identity_local": cold_known_identity and cold_known_ms <= 1000.0,
            "database_update_nonblocking_under_250ms": db_update_submit < 250.0,
        },
        "thread_evidence": {
            "qt_ui_thread": get_ident(),
            "provider_thread_ids": sorted(set(blocked.thread_ids + hostile.thread_ids)),
            "ui_thread_network_calls": 0 if blocked_calls_off_ui and all(value != get_ident() for value in hostile.thread_ids) else 1,
        },
        "sqlite": sqlite_report,
        "network_truth": "Cold unseen symbols show loading immediately; complete fresh network data within one second is not claimed.",
    }
    result["pass"] = all(result["targets"].values())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
