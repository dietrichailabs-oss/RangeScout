"""Packaged live-network smoke for production provider and local-first plumbing."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from threading import Event, Lock, get_ident
from time import monotonic, sleep
from typing import Any

from app import PRODUCT
from app.application.bootstrap import RangeScoutApplication
from app.application.services import default_range_window
from app.market_calendar.us_equities import market_session_status

try:
    from PySide6.QtCore import QRunnable, QThreadPool
except Exception:
    QRunnable = QThreadPool = None  # type: ignore[assignment]


class _NetworkTask(QRunnable if QRunnable is not None else object):
    def __init__(self, kind: str, symbol: str, application: RangeScoutApplication, result: dict[str, Any], lock: Lock) -> None:
        if QRunnable is not None:
            super().__init__()
        self.kind = kind
        self.symbol = symbol
        self.application = application
        self.result = result
        self.lock = lock

    def run(self) -> None:
        began = monotonic()
        record: dict[str, Any] = {
            "kind": self.kind,
            "symbol": self.symbol,
            "worker_thread_id": get_ident(),
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if self.kind == "quote":
                response = self.application.market_data_service.fetch_quote(self.symbol)
                quote = response.payload
                record.update(
                    success=True,
                    winning_provider=response.metadata.provider_id,
                    request_id=response.metadata.capabilities.get("fabric_request_id"),
                    price=str(quote.last),
                    received_at_utc=response.timestamp.isoformat(),
                    quote_timestamp_utc=quote.timestamp.isoformat(),
                    provider_timestamp_utc=(quote.provider_timestamp.isoformat() if quote.provider_timestamp else None),
                    delay_class=quote.delay_label.value,
                    returned_symbol=quote.instrument.identifier.symbol,
                    _response=response,
                )
            else:
                instrument = self.application.market_data_service.resolve_instrument(self.symbol)
                start, end = default_range_window(30)
                response = self.application.market_data_service.fetch_historical(
                    instrument.identifier, start=start, end=end
                )
                bars, _actions = response.payload
                record.update(
                    success=True,
                    winning_provider=response.metadata.provider_id,
                    request_id=response.metadata.capabilities.get("fabric_request_id"),
                    bar_count=len(bars),
                    received_at_utc=response.timestamp.isoformat(),
                    _response=response,
                )
        except Exception as exc:
            record.update(success=False, error_type=type(exc).__name__)
        record["finished_utc"] = datetime.now(timezone.utc).isoformat()
        record["elapsed_ms"] = round((monotonic() - began) * 1000.0, 3)
        with self.lock:
            self.result.update(record)
            self.result["done"].set()


def run_live_network_diagnostic(
    qt_application: Any,
    *,
    output_path: Path,
    data_dir: Path,
    symbols: tuple[str, ...] = ("AAPL", "BA", "NVDA"),
    timeout_seconds: float = 25.0,
) -> int:
    if QThreadPool is None:
        raise RuntimeError("PySide6 QThreadPool is required for the live-network diagnostic.")
    application = RangeScoutApplication(data_dir=data_dir)
    application.start_background_services = lambda: None
    from app.ui.main import build_window

    window = build_window(application=application, auto_refresh=False)
    window.live_refresh_timer.stop()
    window._auto_network_refresh = False
    window.show()
    qt_application.processEvents()
    ui_thread_id = get_ident()
    session = market_session_status()
    report: dict[str, Any] = {
        "schema": "rangescout.live-network-diagnostic.v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "version": PRODUCT.version,
        "build_identity": PRODUCT.build_identity,
        "execution": "production application composition root",
        "packaged_runtime": bool(getattr(sys, "frozen", False)),
        "executable_name": Path(sys.executable).name,
        "deterministic_quote_substitution_used": False,
        "credentials_in_report": False,
        "request_urls_in_report": False,
        "ui_thread_id": ui_thread_id,
        "market_session": {
            "state": "OPEN" if session.is_open else "CLOSED",
            "label": session.label,
            "checked_at_et": session.checked_at_et.isoformat(),
            "next_transition_et": session.next_transition_et.isoformat(),
        },
        "provider_inventory": [
            {
                "provider_id": adapter.descriptor.provider_id,
                "adapter_type": f"{type(adapter).__module__}.{type(adapter).__name__}",
                "requires_credentials": adapter.descriptor.requires_credentials,
            }
            for adapter in application.fabric_registry.snapshot()
        ],
        "failover_test": {
            "status": "NOT_APPLICABLE",
            "reason": "Yahoo is the only enabled no-key equity quote provider in this credential-free smoke environment.",
        },
        "symbols": [],
    }
    lock = Lock()
    pool = QThreadPool.globalInstance()
    try:
        for symbol in symbols:
            application.market_data_router.cache.clear()
            window.set_active_symbol(symbol, source="live-network-diagnostic")
            qt_application.processEvents()
            local_began = monotonic()
            local_snapshot = application.local_snapshots.load(symbol)
            local_elapsed_ms = round((monotonic() - local_began) * 1000.0, 3)
            quote: dict[str, Any] = {"done": Event()}
            history: dict[str, Any] = {"done": Event()}
            dispatched = datetime.now(timezone.utc).isoformat()
            pool.start(_NetworkTask("history", symbol, application, history, lock))
            pool.start(_NetworkTask("quote", symbol, application, quote, lock))
            deadline = monotonic() + timeout_seconds
            history_pending_at_quote: bool | None = None
            while (not quote["done"].is_set() or not history["done"].is_set()) and monotonic() < deadline:
                qt_application.processEvents()
                if quote["done"].is_set() and history_pending_at_quote is None:
                    history_pending_at_quote = not history["done"].is_set()
                sleep(0.01)
            quote_finished_first = quote["done"].is_set() and (
                not history["done"].is_set() or str(quote.get("finished_utc", "")) <= str(history.get("finished_utc", ""))
            )
            quote_response = quote.get("_response")
            if quote_response is not None:
                window._last_quote_provider_id = quote_response.metadata.provider_id
                window._apply_quote_success(quote_response.payload, refresh_collections=False)
                qt_application.processEvents()
            quote_record = {key: value for key, value in quote.items() if key != "done" and not key.startswith("_")}
            history_record = {key: value for key, value in history.items() if key != "done" and not key.startswith("_")}
            request_id = quote_record.get("request_id")
            quote_record["router_diagnostic"] = application.market_data_router.diagnostics(
                str(request_id) if request_id else None
            )
            history_request_id = history_record.get("request_id")
            history_record["router_diagnostic"] = application.market_data_router.diagnostics(
                str(history_request_id) if history_request_id else None
            )
            distinct_worker_threads = bool(
                quote_record.get("worker_thread_id")
                and history_record.get("worker_thread_id")
                and quote_record["worker_thread_id"] != history_record["worker_thread_id"]
            )
            report["symbols"].append(
                {
                    "symbol": symbol,
                    "dispatched_utc": dispatched,
                    "local_identity": {
                        "name": local_snapshot.identity.security_name,
                        "venue": local_snapshot.identity.exchange,
                        "elapsed_ms": local_elapsed_ms,
                        "query_count": local_snapshot.query_count,
                        "loaded_before_network_dispatch": True,
                    },
                    "quote": quote_record,
                    "history": history_record,
                    "history_pending_when_quote_completed": bool(history_pending_at_quote),
                    "quote_completed_before_or_with_history": quote_finished_first,
                    "distinct_worker_threads": distinct_worker_threads,
                    "off_ui_thread": quote_record.get("worker_thread_id") != ui_thread_id,
                    "history_independence_pass": distinct_worker_threads and quote_record.get("worker_thread_id") != ui_thread_id,
                    "market_ui": {
                        "window_visible": window._qt_window.isVisible(),
                        "active_symbol": window.current_symbol,
                        "price_text": window.price_text.text(),
                        "provider_id": window._last_quote_provider_id,
                        "matches_accepted_quote": bool(
                            quote_record.get("success")
                            and window.current_quote is quote_response.payload
                            and window.current_symbol == symbol
                            and window._last_quote_provider_id == quote_record.get("winning_provider")
                        ),
                    },
                }
            )
        report["pass"] = all(
            item["quote"].get("success") is True
            and item["quote"].get("winning_provider")
            and item["quote"].get("returned_symbol") == item["symbol"]
            and _positive_price(item["quote"].get("price"))
            and item["quote"].get("provider_timestamp_utc")
            and item["quote"]["router_diagnostic"].get("attempts")
            and item["off_ui_thread"]
            and item["history_independence_pass"]
            and item["market_ui"]["matches_accepted_quote"]
            for item in report["symbols"]
        )
    finally:
        pool.waitForDone(int(timeout_seconds * 1000))
        window._shutdown_runtime()
        window._qt_window.close()
        application.shutdown()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return 0 if report.get("pass") else 2


def _positive_price(value: object) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False
