from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
import inspect
import logging
from threading import Event
from time import perf_counter
from typing import Any

from app.application.bootstrap import RangeScoutApplication
from app.application.active_symbol import ActiveSymbolController, ActiveSymbolState, SymbolRequest, normalize_symbol
from app.application.local_snapshot import LocalCompanyIdentity, LocalSymbolSnapshot
from app.historical_store.repository import HistoricalStore
from app.application.local_data import delete_local_data, LocalDataDeletionReport
from app.analytics.trading_indicators import calculate_risk
from app.catalysts.correlation import CorrelatedEvent, DIRECTION_DISCLOSURE
from app.catalysts.entities import CatalystEvent
from app.catalysts.presentation import human_duration, human_event_title, safe_source_url, source_link_label
from app.alerts.rules import AlertRule, evaluate_alerts
from app.alerts.dispatcher import AlertNotification, AlertPreferences, AlertType
from app.alerts.presentation import humanize_event_code, humanize_status_text
from app.application.catalyst_runtime import CatalystSource
from app.application.live_trading_runtime import LiveSymbolState
from app.application.runtime_coordinator import RuntimeCoordinator
from app.charts.prepare import prepare_chart_payload
from app.comparisons.compare import compare_symbols
from app.configuration.settings import ALLOWED_LIVE_REFRESH_INTERVALS_MS
from app.configuration.settings import export_safe_settings, import_safe_settings
from app.company_data.instrument_intelligence import InstrumentMatch, InstrumentResolver
from app.application.recent_symbols import RecentSymbols
from app.ui.presentation import directional_price, freshness_label
from app.ui.formatting import format_financial_value
from app.market_data.fusion import previous_regular_close
from app.ui.theme import resolve_effective_theme
from app.exports.csv_writer import export_bars_csv
from app.models.schemas import AlertEvent, AssetType, DataDelay, DataFreshnessState, Instrument, InstrumentIdentifier, OhlcvBar, QuoteSnapshot
from app.market_data.contracts import AssetClass
from app.market_calendar.us_equities import NEW_YORK, market_session_status
from app.market_data.execution import RequestCancelled
from app.streaming.ticker import plan_ticker_subscriptions
from app.streaming.events import StreamStatus
from app.streaming.providers import finnhub_url
from app.streaming.qt_transport import QtWebSocketTransport
from app.streaming.ticker import TickerSubscriptionPlan
from app.scanner.engine import ScannerRow, aggregate_scanner_rows, filter_scanner_rows
from app.notes.store import NoteStore
from app.platform import platform_adapter
from app.watchlists.manager import WatchlistStore
from app.research.caching import ResearchCache
from app.research.fundamentals import ResearchService, SecCompanyFactsClient
from app.research.models import ResearchSnapshot, ResearchValue
from app.research.routing import ResearchRoute, plan_research, route_snapshot
from app.research.analyst import AnalystResult, AnalystService, AnalystState
from app.security.credentials import ProviderCredentials
from app.ui.branding import load_application_icon
from app.ui.system_tray import SystemTrayController
from app.ui.provider_dialog import DataProvidersDialog


_LOG = logging.getLogger(__name__)


try:
    from PySide6.QtCore import QObject, QPointF, QRunnable, QThreadPool, QTimer, Qt, Signal, QUrl, QStringListModel
    from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHeaderView,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QStyle,
        QStyleOptionTab,
        QStylePainter,
        QTabBar,
        QTabWidget,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
        QInputDialog,
        QCompleter,
        QFileDialog,
    )
except Exception:
    Qt = QApplication = QLineEdit = QLabel = QWidget = QMainWindow = QPushButton = QVBoxLayout = None  # type: ignore[assignment]
    QObject = QRunnable = QThreadPool = QTimer = Signal = None  # type: ignore[assignment]
    QComboBox = QSpinBox = QDoubleSpinBox = QFormLayout = QGridLayout = QGroupBox = QHBoxLayout = QListWidget = QListWidgetItem = None  # type: ignore[assignment]
    QTabWidget = QTableWidget = QTableWidgetItem = QTextEdit = QFrame = QMessageBox = None  # type: ignore[assignment]
    QHeaderView = QScrollArea = QSizePolicy = None  # type: ignore[assignment]
    QPainter = QPen = QBrush = QColor = QFont = QDesktopServices = QUrl = None  # type: ignore[assignment]
    QIcon = QPixmap = QPainterPath = QPointF = QTabBar = QStyle = QStyleOptionTab = QStylePainter = None  # type: ignore[assignment]
    QInputDialog = None  # type: ignore[assignment]
    QCompleter = QFileDialog = QStringListModel = QKeySequence = QShortcut = None  # type: ignore[assignment]


_widget_base = QWidget if QWidget is not None else object


class NoGuiRuntimeError(RuntimeError):
    pass


class Theme:
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class LiteralAmpersandTabBar(QTabBar if QTabBar is not None else object):
    """Paint literal ampersands while preserving stable, single-& tab text."""

    def tabSizeHint(self, index: int):
        size = super().tabSizeHint(index)
        if "&" in self.tabText(index):
            size.setWidth(size.width() + 14)
        return size

    def paintEvent(self, event: Any) -> None:  # noqa: ARG002
        if QStylePainter is None or QStyleOptionTab is None or QStyle is None:
            return
        painter = QStylePainter(self)
        for index in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            option.text = option.text.replace("&", "&&")
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabLabel, option)


PROVIDER_SETTINGS_LABELS = {
    "yahoo": "Yahoo",
    "finnhub": "Finnhub",
}

PROVIDER_SIGNUP_URLS = {
    "finnhub": "https://finnhub.io/register",
    "alpha_vantage": "https://www.alphavantage.co/support/#api-key",
    "twelve_data": "https://twelvedata.com/",
    "fred": "https://fred.stlouisfed.org/docs/api/api_key.html",
    "logo_dev": "https://www.logo.dev/",
    "congress": "https://api.congress.gov/sign-up/",
}

ACTIVE_QUOTE_WALL_CLOCK_BUDGET_MS = 4000
ACTIVE_QUOTE_TIMEOUT_TIMER_MS = 3500


class MiniLineChart(_widget_base):
    LINE_MODE = "line"
    CANDLESTICK_MODE = "candlestick"

    def __init__(self) -> None:
        if QWidget is None or QPainter is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        super().__init__()
        self.setMinimumHeight(240)
        self.setObjectName("rangescout_chart")
        self._opens: list[float] = []
        self._closes: list[float] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._volumes: list[float | int] = []
        self._markers: dict[str, dict[str, Any]] = {}
        self._theme = Theme.LIGHT
        self._display_mode = self.LINE_MODE
        self._last_rendered_candle_directions: tuple[str, ...] = ()
        self._empty_state_text = "Loading price history…"

    @property
    def display_mode(self) -> str:
        return self._display_mode

    @property
    def last_rendered_candle_directions(self) -> tuple[str, ...]:
        return self._last_rendered_candle_directions

    def set_display_mode(self, mode: str) -> None:
        if mode not in {self.LINE_MODE, self.CANDLESTICK_MODE}:
            raise ValueError("Unsupported chart display mode.")
        self._display_mode = mode
        self.update()

    def set_series(
        self,
        closes: list[float],
        *,
        opens: list[float] | None = None,
        highs: list[float] | None = None,
        lows: list[float] | None = None,
        volumes: list[float | int] | None = None,
        markers: dict[str, Any] | None = None,
    ) -> None:
        self._opens = list(opens or [])
        self._closes = list(closes)
        self._highs = list(highs or [])
        self._lows = list(lows or [])
        self._volumes = list(volumes or [])
        self._markers = markers or {}
        self._last_rendered_candle_directions = ()
        self.update()

    def set_empty_state(self, message: str) -> None:
        self._empty_state_text = str(message or "Price history unavailable.")
        self.update()

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: ARG002
        if QPainter is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = self._theme == Theme.DARK
        background = QColor(7, 20, 34) if dark else QColor(248, 250, 252)
        text = QColor(230, 230, 230) if dark else QColor(15, 23, 42)
        positive = self._closes and self._closes[-1] >= self._closes[0]
        line = QColor(44, 196, 118) if positive else QColor(255, 76, 76)
        grid = QColor(30, 52, 73) if dark else QColor(209, 213, 219)

        painter.fillRect(self.rect(), QBrush(background))
        painter.setPen(QPen(text))
        painter.setFont(QFont("Segoe UI", 10))
        title = "Live OHLCV candlestick chart" if self._display_mode == self.CANDLESTICK_MODE else "Market price chart"
        painter.drawText(12, 18, title)

        area = self.rect().adjusted(12, 28, -12, -28)
        if not self._closes:
            painter.drawText(area, Qt.AlignCenter, self._empty_state_text)
            return

        all_values = list(self._closes)
        if self._opens:
            all_values.extend(self._opens)
        if self._highs:
            all_values.extend(self._highs)
        if self._lows:
            all_values.extend(self._lows)
        if not all_values:
            painter.drawText(area, Qt.AlignCenter, "Invalid price data. Try Refresh again.")
            return

        price_area_height = int(area.height() * 0.72)
        price_area = area.adjusted(12, 4, -12, -(area.height() - price_area_height))
        volume_area = area.adjusted(12, price_area.height() + 12, -12, -4)
        painter.drawRect(price_area)
        painter.drawRect(volume_area)
        for step in range(1, 5):
            y = price_area.top() + int(price_area.height() * step / 5)
            painter.drawLine(price_area.left(), y, price_area.right(), y)
        for step in range(1, 8):
            x = price_area.left() + int(price_area.width() * step / 8)
            painter.drawLine(x, price_area.top(), x, price_area.bottom())

        mn = min(all_values)
        mx = max(all_values)
        if mx == mn:
            mn -= 1
            mx += 1

        def x_at(index: int) -> float:
            if self._display_mode == self.CANDLESTICK_MODE:
                return price_area.left() + ((index + 0.5) / len(self._closes)) * price_area.width()
            if len(self._closes) == 1:
                return price_area.left()
            return price_area.left() + (index / (len(self._closes) - 1)) * price_area.width()

        def y_at(value: float) -> float:
            ratio = (value - mn) / (mx - mn)
            return price_area.bottom() - ratio * price_area.height()

        candle_count = min(len(self._opens), len(self._highs), len(self._lows), len(self._closes))
        candlestick_ready = self._display_mode == self.CANDLESTICK_MODE and candle_count == len(self._closes)
        if candlestick_ready:
            directions: list[str] = []
            slot_width = price_area.width() / max(1, candle_count)
            body_width = max(3, min(12, int(slot_width * 0.62)))
            for idx in range(candle_count):
                opening = self._opens[idx]
                closing = self._closes[idx]
                direction = "up" if closing > opening else "down" if closing < opening else "flat"
                directions.append(direction)
                color = QColor(34, 197, 94) if direction == "up" else QColor(239, 68, 68) if direction == "down" else QColor(245, 158, 11)
                x = int(x_at(idx))
                painter.setPen(QPen(color, 1.5))
                painter.drawLine(x, int(y_at(self._lows[idx])), x, int(y_at(self._highs[idx])))
                open_y = int(y_at(opening))
                close_y = int(y_at(closing))
                top = min(open_y, close_y)
                body_height = abs(close_y - open_y)
                painter.setBrush(QBrush(color))
                if body_height == 0:
                    painter.drawLine(x - body_width // 2, open_y, x + body_width // 2, open_y)
                else:
                    painter.drawRect(x - body_width // 2, top, body_width, max(1, body_height))
            self._last_rendered_candle_directions = tuple(directions)
        else:
            self._last_rendered_candle_directions = ()
            if QPainterPath is not None and QPointF is not None:
                trace = QPainterPath(QPointF(x_at(0), y_at(self._closes[0])))
                for idx in range(1, len(self._closes)):
                    trace.lineTo(QPointF(x_at(idx), y_at(self._closes[idx])))
                fill = QPainterPath(trace)
                fill.lineTo(QPointF(x_at(len(self._closes) - 1), price_area.bottom()))
                fill.lineTo(QPointF(x_at(0), price_area.bottom()))
                fill.closeSubpath()
                painter.fillPath(fill, QBrush(QColor(line.red(), line.green(), line.blue(), 26)))
                painter.setPen(QPen(line, 2))
                painter.drawPath(trace)
            else:
                painter.setPen(QPen(line, 2))
                for idx in range(1, len(self._closes)):
                    painter.drawLine(x_at(idx - 1), y_at(self._closes[idx - 1]), x_at(idx), y_at(self._closes[idx]))
            painter.setPen(QPen(QColor(16, 185, 129), 1.5))
            for idx in range(min(len(self._closes), len(self._highs), len(self._lows))):
                if len(self._closes) == 1:
                    break
                painter.drawLine(
                    x_at(idx),
                    y_at(self._lows[idx]),
                    x_at(idx),
                    y_at(self._highs[idx]),
                )

        if self._volumes:
            max_volume = max(self._volumes) or 1
            volume_width = max(2, int(price_area.width() / max(1, len(self._volumes))))
            painter.setPen(QPen(text))
            for idx, volume in enumerate(self._volumes):
                if idx >= len(self._closes):
                    break
                volume_color = line
                if candlestick_ready:
                    volume_color = QColor(34, 197, 94) if self._closes[idx] > self._opens[idx] else QColor(239, 68, 68) if self._closes[idx] < self._opens[idx] else QColor(245, 158, 11)
                painter.setBrush(QBrush(QColor(volume_color.red(), volume_color.green(), volume_color.blue(), 145)))
                ratio = volume / max_volume if max_volume else 0
                if ratio < 0:
                    ratio = 0
                bar_height = max(2, int((ratio) * max(volume_area.height() - 2, 2)))
                x = int(x_at(idx) - (volume_width / 2))
                y = volume_area.bottom() - bar_height
                painter.drawRect(x, y, volume_width - 2, bar_height)

        painter.setPen(QPen(QColor(244, 114, 182)))
        for marker in self._markers.values():
            marker_text = marker.get("date", "n/a") if isinstance(marker, dict) else "n/a"
            try:
                marker_value = float(marker.get("value", 0))
            except Exception:
                continue
            marker_y = y_at(marker_value)
            painter.drawLine(price_area.left(), int(marker_y), price_area.right(), int(marker_y))
            painter.drawText(price_area.left() + 4, int(marker_y) - 4, f"{marker_text}: {marker_value:.2f}")

        painter.setPen(QPen(text))
        painter.drawText(price_area.left(), price_area.top() + 12, f"max {mx:.2f}")
        painter.drawText(price_area.left(), price_area.bottom() + 12, f"min {mn:.2f}")


@dataclass
class SurfaceEvidence:
    name: str
    filename: str


if QObject is not None and QRunnable is not None and Signal is not None:
    class _QuoteRefreshSignals(QObject):
        finished = Signal(object, str, object, object)


    class _QuoteRefreshTask(QRunnable):
        def __init__(self, market_data: Any, local_snapshots: Any, request: SymbolRequest) -> None:
            super().__init__()
            self.market_data = market_data
            self.local_snapshots = local_snapshots
            self.request = request
            self.signals = _QuoteRefreshSignals()
            self.cancellation_event = Event()

        def cancel(self) -> None:
            self.cancellation_event.set()

        def run(self) -> None:
            try:
                if self.cancellation_event.is_set():
                    raise RequestCancelled("Quote request was superseded.")
                fetch_quote = self.market_data.fetch_quote
                parameters = inspect.signature(fetch_quote).parameters
                variable = any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values())
                kwargs: dict[str, object] = {}
                if variable or "cancellation_event" in parameters:
                    kwargs["cancellation_event"] = self.cancellation_event
                if variable or "canonical_instrument_id" in parameters:
                    kwargs["canonical_instrument_id"] = (
                        f"instrument:{self.request.instrument_id}" if self.request.instrument_id is not None
                        else None
                    )
                if variable or "provider_symbols" in parameters:
                    kwargs["provider_symbols"] = self.request.provider_symbols
                if variable or "asset_class" in parameters:
                    try:
                        kwargs["asset_class"] = AssetClass(self.request.asset_class)
                    except ValueError:
                        pass
                result = fetch_quote(self.request.symbol, **kwargs)
                if self.cancellation_event.is_set():
                    raise RequestCancelled("Quote request was superseded.")
                self.local_snapshots.save_quote(result.payload, result.metadata.provider_id)
            except Exception as exc:
                self.signals.finished.emit(self.request, self.market_data.provider_id, None, exc)
                return
            self.signals.finished.emit(self.request, result.metadata.provider_id, result.payload, None)


    class _InstrumentDiscoverySignals(QObject):
        finished = Signal(int, str, object, object)


    class _InstrumentDiscoveryTask(QRunnable):
        def __init__(self, application: RangeScoutApplication, generation: int, query: str) -> None:
            super().__init__()
            self.application = application
            self.generation = generation
            self.query = query
            self.signals = _InstrumentDiscoverySignals()

        def run(self) -> None:
            try:
                results = self.application.discover_instruments(self.query)
            except Exception as exc:
                self.signals.finished.emit(self.generation, self.query, None, exc)
                return
            self.signals.finished.emit(self.generation, self.query, results, None)


    class _HistoryRefreshSignals(QObject):
        finished = Signal(object, str, str, object, object)


    class _HistoryRefreshTask(QRunnable):
        def __init__(self, market_data: Any, database_path: Path, request: SymbolRequest, range_days: int) -> None:
            super().__init__()
            self.market_data = market_data
            self.database_path = Path(database_path)
            self.request = request
            self.range_days = range_days
            self.signals = _HistoryRefreshSignals()

        def run(self) -> None:
            store = None
            try:
                instrument = self.market_data.resolve_instrument(self.request.symbol)
                from app.application.services import default_range_window

                start, end = default_range_window(self.range_days)
                fetch_historical = self.market_data.fetch_historical
                parameters = inspect.signature(fetch_historical).parameters
                variable = any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values())
                kwargs: dict[str, object] = {}
                if variable or "canonical_instrument_id" in parameters:
                    kwargs["canonical_instrument_id"] = (
                        f"instrument:{self.request.instrument_id}" if self.request.instrument_id is not None else None
                    )
                if variable or "provider_symbols" in parameters:
                    kwargs["provider_symbols"] = self.request.provider_symbols
                if variable or "asset_class" in parameters:
                    try:
                        kwargs["asset_class"] = AssetClass(self.request.asset_class)
                    except ValueError:
                        pass
                result = fetch_historical(instrument.identifier, start=start, end=end, **kwargs)
                bars, _actions = result.payload
                store = HistoricalStore(self.database_path)
                store.upsert_bars(bars, result.metadata.provider_id)
                cached = store.get_bars(
                    instrument.identifier, result.metadata.provider_id, start=start.date(), end=end.date()
                )
            except Exception as exc:
                self.signals.finished.emit(self.request, "", "", None, exc)
                return
            finally:
                if store is not None:
                    store.close()
            self.signals.finished.emit(
                self.request, result.metadata.provider_id, result.metadata.provider_name, cached, None
            )


    class _ComparisonSignals(QObject):
        finished = Signal(object, object)


    class _ComparisonTask(QRunnable):
        def __init__(self, market_data: Any, symbol: str, benchmark: str) -> None:
            super().__init__()
            self.market_data = market_data
            self.symbol = symbol
            self.benchmark = benchmark
            self.signals = _ComparisonSignals()

        def run(self) -> None:
            try:
                symbol_inst = self.market_data.resolve_instrument(self.symbol)
                benchmark_inst = self.market_data.resolve_instrument(self.benchmark)
                symbol_bars = self.market_data.fetch_historical(symbol_inst.identifier).payload[0]
                benchmark_bars = self.market_data.fetch_historical(benchmark_inst.identifier).payload[0]
                if not symbol_bars or not benchmark_bars:
                    raise ValueError("No bars to compare.")
                result = compare_symbols(symbol_bars, benchmark_bars, self.symbol, self.benchmark)
            except Exception as exc:
                self.signals.finished.emit(None, exc)
                return
            self.signals.finished.emit(result, None)


    class _RuntimeBridge(QObject):
        invoke = Signal(object)


    class _ResearchSignals(QObject):
        finished = Signal(object, object, object)


    class _ResearchTask(QRunnable):
        def __init__(self, service: ResearchService, request: SymbolRequest, period_mode: str) -> None:
            super().__init__()
            self.service = service
            self.request = request
            self.period_mode = period_mode
            self.signals = _ResearchSignals()

        def run(self) -> None:
            try:
                snapshot = route_snapshot(self.service, self.request, self.period_mode)
            except Exception as exc:
                self.signals.finished.emit(self.request, None, exc)
                return
            self.signals.finished.emit(self.request, snapshot, None)


    class _AnalystSignals(QObject):
        finished = Signal(object, object, object)


    class _AnalystTask(QRunnable):
        def __init__(self, service: AnalystService, request: SymbolRequest, force: bool) -> None:
            super().__init__()
            self.service = service
            self.request = request
            self.force = force
            self.signals = _AnalystSignals()

        def run(self) -> None:
            try:
                result = self.service.load(self.request.symbol, self.request.generation, force=self.force)
            except Exception as exc:
                self.signals.finished.emit(self.request, None, exc)
                return
            self.signals.finished.emit(self.request, result, None)


    class _CompanyLogoSignals(QObject):
        finished = Signal(object, object, object)


    class _CompanyLogoTask(QRunnable):
        def __init__(self, service: Any, request: SymbolRequest, exchange: str | None, theme: str) -> None:
            super().__init__()
            self.service = service
            self.request = request
            self.exchange = exchange
            self.theme = theme
            self.signals = _CompanyLogoSignals()

        def run(self) -> None:
            try:
                asset = self.service.resolve(
                    self.request.symbol,
                    self.exchange,
                    theme=self.theme,
                )
            except Exception as exc:
                self.signals.finished.emit(self.request, None, exc)
                return
            self.signals.finished.emit(self.request, asset, None)


    class _NewsSignals(QObject):
        finished = Signal(object, object, object)


    class _NewsTask(QRunnable):
        def __init__(self, service: Any, request: SymbolRequest) -> None:
            super().__init__()
            self.service = service
            self.request = request
            self.signals = _NewsSignals()

        def run(self) -> None:
            try:
                result = self.service.fetch_news(self.request.symbol)
            except Exception as exc:
                self.signals.finished.emit(self.request, None, exc)
                return
            self.signals.finished.emit(self.request, result, None)


    class _RuntimeMainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_close_callback: Any | None = None
            self.runtime_close_interceptor: Any | None = None

        def closeEvent(self, event: Any) -> None:
            if self.runtime_close_interceptor is not None and bool(self.runtime_close_interceptor()):
                event.ignore()
                return
            if self.runtime_close_callback is not None:
                self.runtime_close_callback()
            super().closeEvent(event)


class RangeScoutWindow:
    def __init__(
        self,
        *,
        credential_store: Any | None = None,
        runtime_transport_factory: Any | None = None,
        catalyst_sources: list[CatalystSource] | None = None,
        runtime_executor: Any | None = None,
        runtime_schedule: Any | None = None,
        runtime_post: Any | None = None,
        application: RangeScoutApplication | None = None,
        research_service: ResearchService | None = None,
        analyst_service: AnalystService | None = None,
        auto_refresh: bool = True,
    ) -> None:
        if QMainWindow is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        self._qt_window = _RuntimeMainWindow()
        self._qt_window.setWindowTitle("RangeScout")
        self._qt_window.setMinimumSize(1120, 700)
        self._shutdown_complete = False
        self._tray_controller: SystemTrayController | None = None
        self._qt_application = QApplication.instance() if QApplication is not None else None
        application_icon = load_application_icon()
        if application_icon is not None:
            is_null = getattr(application_icon, "isNull", None)
            if not callable(is_null) or not bool(is_null()):
                self._qt_window.setWindowIcon(application_icon)
                if self._qt_application is not None:
                    setter = getattr(self._qt_application, "setWindowIcon", None)
                    if callable(setter):
                        setter(application_icon)

        adapter = platform_adapter()
        self.app = application or RangeScoutApplication(data_dir=Path(adapter.app_data_dir), credential_store=credential_store)
        self._credential_unsubscribe = self.app.provider_configuration.subscribe(self._on_credential_state_changed)
        self._qt_window.resize(self.app.settings.window_width, self.app.settings.window_height)
        self.provider = self.app.get_provider()
        self.market_data = self.app.market_data_service
        self._last_quote_provider_id = self.provider.provider_id
        self._effective_theme = Theme.DARK
        self.active_symbol = ActiveSymbolController("AAPL")
        self.active_symbol.subscribe(self._on_active_symbol_changed)
        self.current_bars: list[OhlcvBar] = []
        self.current_quote: QuoteSnapshot | None = None
        self._ticker_watchlist_title = "My Watchlist"
        self._ticker_watchlist_symbols: list[str] = []
        self.alert_rules: list[AlertRule] = []
        self._market_alert_records: list[tuple[str, str, str]] = []
        self._quote_refresh_in_flight = False
        self._active_quote_task: Any | None = None
        self._quote_thread_pool: Any | None = None
        self._quote_pool_is_global = False
        self._quote_tasks: dict[int, Any] = {}
        self._quote_dispatch_started: dict[int, float] = {}
        self._quote_selection_started: dict[int, float] = {}
        self._quote_timeout_timers: dict[int, Any] = {}
        self._quote_timed_out_requests: set[int] = set()
        self._pending_quote_selection_at: float | None = None
        self._history_tasks: dict[int, Any] = {}
        self._history_dispatch_started: dict[int, float] = {}
        self._instrument_discovery_generation = 0
        self._instrument_discovery_tasks: dict[int, Any] = {}
        self._market_range_revision = 0
        self._comparison_tasks: set[Any] = set()
        self._auto_network_refresh = bool(auto_refresh)
        self._performance_timings: dict[str, Any] = {
            "ui_thread_network_calls": 0,
            "startup_began": perf_counter(),
            "sec_latency_ms": None,
            "analyst_latency_ms": None,
            "logo_latency_ms": None,
            "catalyst_latency_ms": None,
        }
        self._research_dispatch_started: dict[int, float] = {}
        self._analyst_dispatch_started: dict[int, float] = {}
        self._logo_dispatch_started: dict[int, float] = {}
        self._catalyst_dispatch_started: float | None = None
        self._fresh_cycle_began: float | None = None
        self._symbol_snapshot_cache: dict[str, tuple[QuoteSnapshot, tuple[OhlcvBar, ...], datetime]] = {}
        self.recent_symbols = RecentSymbols(self.app.settings.recent_symbols)
        self._research_tasks: dict[int, Any] = {}
        self._research_request_context: dict[int, tuple[str, int, str]] = {}
        self._analyst_tasks: dict[int, Any] = {}
        self._analyst_request_context: dict[int, tuple[str, int, str]] = {}
        self._research_pending_contexts: set[tuple[str, int, str]] = set()
        self._analyst_pending_contexts: set[tuple[str, int, str]] = set()
        self._research_dirty = True
        self._research_loaded_context: tuple[str, int, str] | None = None
        self._scheduled_research_force = False
        self._sec_status_message = "SEC research waiting for the Research surface."
        self._analyst_status_message = "Analyst data waiting for the Research surface."
        self._company_logo_tasks: dict[int, Any] = {}
        self._company_logo_inflight: set[tuple[str, str, str]] = set()
        self._news_tasks: dict[int, Any] = {}
        self._official_catalyst_events: list[CorrelatedEvent] = []
        self._provider_news_events: list[CatalystEvent] = []
        self._news_status_message = "Provider news has not been checked yet."

        self.watchlist_store = WatchlistStore.from_path(self.app.data_dir / "watchlists.json")
        self.note_store = NoteStore(self.app.data_dir / "notes.json")
        self.local_instrument_search = InstrumentResolver(self.app.store.path)
        self._selected_note_id: str | None = None
        self._active_note_category = "Research Notes"
        self._note_editor_dirty = False
        self._loading_note_editor = False
        self.research_service = research_service or ResearchService(
            SecCompanyFactsClient(ResearchCache(Path(self.app.data_dir) / "research_cache"))
        )
        self.analyst_service = analyst_service or AnalystService(self.app.store.path, self.app.credential_store)
        self._research_debounce_timer = QTimer()
        self._research_debounce_timer.setSingleShot(True)
        self._research_debounce_timer.setInterval(300)
        self._research_debounce_timer.timeout.connect(self._start_research_load)
        self._quote_coalesce_timer = QTimer(self._qt_window)
        self._quote_coalesce_timer.setSingleShot(True)
        self._quote_coalesce_timer.setInterval(100)
        self._quote_coalesce_timer.timeout.connect(self._dispatch_coalesced_quote)

        self.provider_combo = self._build_provider_combo()
        self.provider_settings_selector = self._build_provider_settings_selector()
        self.active_provider_combo = self._build_active_provider_combo()
        self.provider_configuration_text = QLabel("")
        self.provider_connection_text = QLabel("")
        self.finnhub_api_key_input = self._password_input("Enter your Finnhub API key")
        self.get_finnhub_api_key_btn = self._signup_button("Get API Key")
        self.fabric_provider_selector = QComboBox()
        for status in self.app.fabric_provider_statuses():
            self.fabric_provider_selector.addItem(str(status["display_name"]), str(status["provider_id"]))
        self.fabric_provider_status_text = QLabel("")
        self.fabric_provider_status_text.setWordWrap(True)
        self.fabric_api_key_input = self._password_input("Enter your own free-tier API key")
        self.save_fabric_credentials_btn = QPushButton("Save Fabric Key Securely")
        self.delete_fabric_credentials_btn = QPushButton("Delete Fabric Key")
        self.get_fabric_api_key_btn = self._signup_button("Get API Key")
        self.discovery_status_text = QLabel("Official listing discovery status pending")
        self.discovery_status_text.setWordWrap(True)
        self.refresh_discovery_btn = QPushButton("Refresh Official Listings Now")
        self.company_logo_status_text = QLabel("")
        self.company_logo_status_text.setWordWrap(True)
        self.logo_dev_publishable_key_input = self._password_input("Enter free Logo.dev publishable key (pk_…)")
        self.save_company_logo_key_btn = QPushButton("Save Logo Key Securely")
        self.delete_company_logo_key_btn = QPushButton("Delete Logo Key")
        self.get_logo_dev_publishable_key_btn = self._signup_button("Get Publishable Key")
        self.congress_api_key_input = self._password_input("Enter your free Congress.gov API key")
        self.congress_configuration_text = QLabel("")
        self.get_congress_api_key_btn = self._signup_button("Get API Key")
        self.data_providers_btn = QPushButton("Open Data Providers && API Keys")
        self.data_providers_btn.setObjectName("open_data_providers_button")
        self._data_providers_dialog: DataProvidersDialog | None = None
        self.market_symbol_input = QLineEdit("AAPL")
        self.market_symbol_input.setPlaceholderText("AAPL")
        self.market_days_input = QSpinBox()
        self.market_days_input.setRange(30, 3650)
        self.market_days_input.setValue(365)
        self.chart_symbol_input = QLineEdit("AAPL")
        self.chart_symbol_input.setPlaceholderText("AAPL")
        self.chart_days_input = QSpinBox()
        self.chart_days_input.setRange(30, 3650)
        self.chart_days_input.setValue(365)
        self.result_text = QLabel("Ready. Enter a symbol and click Refresh.")
        self.price_text = QLabel("Price: --")
        self.metrics_text = QLabel("Metrics: --")
        self.status_text = QLabel("Provider: --")
        self.provider_diagnostics_text = QLabel("No provider request details yet.")
        self.provider_diagnostics_text.setWordWrap(True)
        self.provider_diagnostics_text.setVisible(False)
        self.provider_details_btn = QPushButton("Details")
        self.market_status_text = QLabel("Market: CLOSED")
        self.live_market_status_text = QLabel("MARKET CLOSED")
        self.last_updated_text = QLabel("Last Updated: -- ET")
        self.live_symbol_text = QLabel("AAPL")
        self.live_price_text = QLabel("--")
        self.live_change_text = QLabel("-- / --")
        self.live_bid_text = QLabel("--")
        self.live_ask_text = QLabel("--")
        self.live_spread_text = QLabel("--")
        self.live_trade_time_text = QLabel("--")
        self.live_provider_text = QLabel(self.provider.provider_name)
        self.live_stream_status_text = QLabel("DISCONNECTED")
        self.live_last_update_text = QLabel("--")
        self.live_indicators_text = QLabel("Indicators: N/A — awaiting sufficient live candle history")
        self.live_indicators_text.setWordWrap(True)
        self.live_candle_interval = QComboBox()
        for label, seconds in (("1s", 1), ("5s", 5), ("15s", 15), ("30s", 30), ("1m", 60), ("5m", 300)):
            self.live_candle_interval.addItem(label, seconds)
        self.risk_entry_input = QDoubleSpinBox()
        self.risk_stop_input = QDoubleSpinBox()
        self.risk_max_input = QDoubleSpinBox()
        for control in (self.risk_entry_input, self.risk_stop_input, self.risk_max_input):
            control.setRange(0.0, 1000000.0)
            control.setDecimals(4)
        self.risk_entry_input.setValue(10.0)
        self.risk_stop_input.setValue(9.5)
        self.risk_max_input.setValue(100.0)
        self.risk_result_text = QLabel("Shares: -- | Actual risk: -- | Stop distance: --")
        self.chart = MiniLineChart()
        self.bars_table = QTableWidget(0, 7)
        self.bars_table.setHorizontalHeaderLabels(["Date", "Open", "High", "Low", "Close", "Volume", "Provider"])
        self.insight_list = QListWidget()
        self.theme_combo = QComboBox()
        self.theme_combo.addItems([Theme.SYSTEM, Theme.LIGHT, Theme.DARK])
        self.theme_combo.setCurrentText(self.app.settings.theme)
        self.company_update_schedule_combo = QComboBox()
        self.logo_refresh_schedule_combo = QComboBox()
        for combo in (self.company_update_schedule_combo, self.logo_refresh_schedule_combo):
            combo.addItem("Off", "off"); combo.addItem("Weekly", "weekly"); combo.addItem("Monthly", "monthly")
        self.company_update_schedule_combo.setCurrentIndex(max(0, self.company_update_schedule_combo.findData(self.app.settings.company_update_schedule)))
        self.logo_refresh_schedule_combo.setCurrentIndex(max(0, self.logo_refresh_schedule_combo.findData(self.app.settings.logo_refresh_schedule)))
        self.update_company_database_btn = QPushButton("Update Company Database")
        self.refresh_company_logos_btn = QPushButton("Refresh Logos")
        self.check_local_database_btn = QPushButton("Check Local Database")
        self.export_preferences_btn = QPushButton("Export Preferences")
        self.import_preferences_btn = QPushButton("Import Preferences")
        self.clear_recent_symbols_btn = QPushButton("Clear Recent Symbols")
        self.company_database_status_text = QLabel("Company database status pending")
        self.company_database_status_text.setWordWrap(True)
        self.database_health_text = QLabel("Local database not checked")
        self.database_health_text.setWordWrap(True)
        self.refresh_interval_combo = self._build_refresh_interval_combo()
        self.ticker_position_combo = QComboBox()
        for label, value in (("Top", "top"), ("Bottom", "bottom"), ("Hidden", "hidden")):
            self.ticker_position_combo.addItem(label, value)
        self.ticker_position_combo.setCurrentIndex(
            max(0, self.ticker_position_combo.findData(self.app.settings.ticker_position))
        )
        self.alert_list = QListWidget()
        self.comparison_result = QLabel("No comparison yet. Enter two symbols and click Compare.")
        self.chart_tab_chart = MiniLineChart()
        self.export_result = QLabel("No exports yet. Refresh a symbol first.")
        self.chart_error_text = QLabel("")
        self.active_symbol_input = QLineEdit("AAPL")
        self.active_symbol_input.setPlaceholderText("Search symbol")
        self.active_symbol_title = QLabel("AAPL")
        self.active_symbol_title.setObjectName("active_symbol_title")
        self.active_symbol_title.setStyleSheet("font-size: 22px; font-weight: 700;")
        self.active_symbol_context = QLabel("ACTIVE SYMBOL • startup")
        self.shell_company_text = QLabel("Market intelligence workstation")
        self.shell_freshness_text = QLabel("DATA READY • provider state pending")
        self.market_company_text = QLabel("AAPL  •  Company profile pending")
        self.market_company_text.setObjectName("company_identity")
        self.market_change_text = QLabel("Change N/A")
        self.extended_hours_text = QLabel("Extended hours N/A")
        self.market_range_text = QLabel("Day range N/A\n52-week range N/A")
        self.market_volume_text = QLabel("Volume N/A\nAverage volume N/A")
        self.market_cap_text = QLabel("Market cap N/A\nShares N/A")
        self.market_performance_text = QLabel("Period performance N/A\nDrawdown N/A")
        self.market_overview_text = QLabel("Market context is provider-dependent.\nUnavailable values remain N/A.")
        self.market_overview_text.setWordWrap(True)
        self.research_status_text = QLabel("Research loads automatically when this surface is opened.")
        self.research_status_text.setWordWrap(True)
        self.research_symbol_avatar = QLabel("AAPL")
        self.research_symbol_avatar.setObjectName("symbol_avatar")
        self.research_symbol_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.research_symbol_avatar.setFixedSize(56, 56)
        self.research_company_text = QLabel("Company profile • N/A")
        self.research_company_text.setStyleSheet("font-size: 17px; font-weight: 700;")
        self.research_quote_text = QLabel("Price N/A • Change N/A")
        self.research_market_status_text = QLabel("MARKET CLOSED")
        self.research_profile_text = QLabel("Sector / industry • N/A")
        self.research_profile_detail_text = QLabel("Sector / industry • N/A")
        self.research_about_text = QLabel("Load official SEC Research to view a traceable company profile. No values are fabricated.")
        self.research_about_text.setWordWrap(True)
        self.research_key_metrics_text = QLabel("Revenue N/A\nNet income N/A\nAssets N/A\nEquity N/A")
        self.research_key_metrics_text.setWordWrap(True)
        self.research_market_metrics_text = QLabel("Day range N/A\n52-week range N/A\nMarket cap N/A\nAverage volume N/A")
        self.research_market_metrics_text.setWordWrap(True)
        self.research_tables: dict[str, QTableWidget] = {}
        self.current_research_snapshot: ResearchSnapshot | None = None
        self.current_analyst_result: AnalystResult | None = None

        ui_build_began = perf_counter()
        self._qt_window.setCentralWidget(self._build_root_ui())
        self._performance_timings["ui_composition_ms"] = (perf_counter() - ui_build_began) * 1000.0
        period_index = self.research_period_combo.findData(self.app.settings.research_period)
        if period_index >= 0:
            self.research_period_combo.setCurrentIndex(period_index)
        if self.app.settings.window_x is not None and self.app.settings.window_y is not None:
            self._qt_window.move(self.app.settings.window_x, self.app.settings.window_y)
        if hasattr(self, "watchlist_id_input") and self.app.settings.selected_watchlist:
            self.watchlist_id_input.setText(self.app.settings.selected_watchlist)
        self._configure_recent_symbol_search()
        self._refresh_peer_symbols()
        self._apply_theme(self.app.settings.theme)
        self._wire_events()
        self._configure_shortcuts()
        self._configure_system_theme_updates()
        self._configure_system_tray()
        self._refresh_watchlists_widget()
        self._on_reload_notes()
        self._update_market_status()
        self._runtime_bridge = _RuntimeBridge()
        self._runtime_bridge.invoke.connect(lambda callback: callback())
        discovery_future = self.app.start_background_services()
        if discovery_future is not None:
            discovery_future.add_done_callback(
                lambda _future: self._runtime_bridge.invoke.emit(self._refresh_discovery_status)
            )
        self._refresh_discovery_status()
        self._refresh_company_logo_status()
        self._refresh_company_database_status()
        self._refresh_analyst_availability()
        self._load_local_symbol_snapshot(self.current_symbol)
        self._request_company_logo(self.current_symbol)
        transport_factory = runtime_transport_factory or self._production_transport
        schedule = runtime_schedule or (lambda delay, callback: QTimer.singleShot(max(0, int(delay * 1000)), callback))
        post_to_owner = runtime_post or (lambda callback: self._runtime_bridge.invoke.emit(callback))
        self.runtime = RuntimeCoordinator(
            self,
            self.app.credential_store,
            self.app.data_dir,
            transport_factory,
            schedule,
            post_to_owner,
            catalyst_sources=catalyst_sources,
            executor=runtime_executor,
        )
        self._qt_window.runtime_close_interceptor = self._intercept_window_close
        # A non-intercepted close (tray unavailable, explicit automation close,
        # or application shutdown) must terminate the Qt event loop.  Normal
        # user X / Alt+F4 remains intercepted above and continues to hide to
        # the tray.
        self._qt_window.runtime_close_callback = self._exit_application
        self.runtime.start(self.provider.provider_id, self.current_symbol, self._watchlist_symbols())
        if auto_refresh:
            self._on_refresh()
        self._performance_timings["startup_interactive_ms"] = (
            perf_counter() - self._performance_timings["startup_began"]
        ) * 1000.0
        self._configure_live_refresh_timer()

    @property
    def current_symbol(self) -> str:
        return self.active_symbol.symbol

    @current_symbol.setter
    def current_symbol(self, value: str) -> None:
        self.active_symbol.set(value, source="legacy-control")

    def build_window_surfaces(self) -> list[SurfaceEvidence]:
        return [
            SurfaceEvidence("market", "surface-market.png"),
            SurfaceEvidence("live-trader", "surface-live-trader.png"),
            SurfaceEvidence("research", "surface-research.png"),
            SurfaceEvidence("watchlists", "surface-watchlists.png"),
            SurfaceEvidence("scanner", "surface-scanner.png"),
            SurfaceEvidence("alerts", "surface-alerts.png"),
            SurfaceEvidence("notes", "surface-notes.png"),
            SurfaceEvidence("exports", "surface-exports.png"),
            SurfaceEvidence("settings", "surface-settings.png"),
        ]

    def _card(self, title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setProperty("dashboardCard", True)
        card_layout = QVBoxLayout(frame)
        card_layout.setContentsMargins(11, 9, 11, 9)
        card_layout.setSpacing(6)
        title_label = QLabel(title.upper())
        title_label.setObjectName("card_title")
        card_layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("card_subtitle")
            subtitle_label.setWordWrap(True)
            card_layout.addWidget(subtitle_label)
        return frame, card_layout

    @staticmethod
    def _navigation_icon(letter: str) -> QIcon:
        """Create a project-owned Qt-drawn rail icon without external icon assets."""
        if QPixmap is None or QPainter is None or QIcon is None:
            return QIcon()
        pixmap = QPixmap(28, 28)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#29445f"), 1))
        painter.setBrush(QBrush(QColor("#10243a")))
        painter.drawRoundedRect(1, 1, 26, 26, 6, 6)
        painter.setPen(QPen(QColor("#9ec5ff"), 1.5))
        painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, letter)
        painter.end()
        return QIcon(pixmap)

    def _surface_heading(self, title: str, subtitle: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("surface_heading")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(2, 2, 2, 5)
        heading = QLabel(title)
        heading.setObjectName("surface_title")
        detail = QLabel(subtitle)
        detail.setObjectName("surface_subtitle")
        detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(detail)
        return frame

    @staticmethod
    def _configure_table(table: QTableWidget, *, rows: int = 8) -> None:
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(max(150, rows * 28))
        if QHeaderView is not None:
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("surface_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    def _build_root_ui(self) -> QWidget:
        if QWidget is None or QVBoxLayout is None or QTabWidget is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        root = QWidget()
        root.setObjectName("workstation_root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # The reference shell is deliberately composed as three stable bands: a
        # full-width product/search header, a rail + workspace body, and a
        # full-width market-status footer. Existing application controls remain
        # the same objects so functional behavior is not forked by the redesign.
        active_header = QFrame()
        active_header.setObjectName("active_symbol_header")
        active_header.setFixedHeight(56)
        active_layout = QHBoxLayout(active_header)
        active_layout.setContentsMargins(16, 8, 16, 8)
        active_layout.setSpacing(12)
        brand = QLabel("◉  RangeScout")
        brand.setObjectName("brand_label")
        brand.setFixedWidth(300)
        brand.setToolTip("RangeScout by Dietrich AI Labs")
        active_layout.addWidget(brand)
        search_mark = QLabel("⌕")
        search_mark.setObjectName("search_mark")
        active_layout.addWidget(search_mark)
        self.active_symbol_input.setPlaceholderText("Search symbols or companies...")
        self.active_symbol_input.setObjectName("global_symbol_search")
        self.active_symbol_input.setMaximumWidth(720)
        active_layout.addWidget(self.active_symbol_input, 1)
        active_submit = QPushButton("Open")
        active_submit.setObjectName("global_search_action")
        active_submit.setProperty("primary", True)
        active_submit.clicked.connect(self._on_global_symbol_submitted)
        self.active_symbol_input.returnPressed.connect(self._on_global_symbol_submitted)
        active_layout.addWidget(active_submit)
        active_layout.addStretch(1)
        for text_value, tooltip in (("○", "Notifications"), ("?", "Help"), ("⚙", "Settings")):
            utility = QPushButton(text_value)
            utility.setObjectName("header_utility")
            utility.setToolTip(tooltip)
            utility.setFixedSize(34, 34)
            if tooltip == "Settings":
                utility.clicked.connect(lambda _checked=False: self.tabs.setCurrentIndex(8))
            active_layout.addWidget(utility)
        self.active_symbol_title.setVisible(False)
        self.active_symbol_context.setVisible(False)
        root_layout.addWidget(active_header)

        shell = QHBoxLayout()
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        rail = QFrame()
        rail.setObjectName("navigation_rail")
        rail.setFixedWidth(140)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(8, 10, 8, 10)
        rail_layout.setSpacing(6)
        self.navigation = QListWidget()
        self.navigation.setObjectName("primary_navigation")
        rail_layout.addWidget(self.navigation, 1)
        collapse = QLabel("«  Collapse")
        collapse.setObjectName("rail_footer")
        rail_layout.addWidget(collapse)
        shell.addWidget(rail)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        shell.addWidget(content, 1)
        self.root_layout = layout

        self.ticker_ribbon = QWidget()
        self.ticker_ribbon.setObjectName("ticker_ribbon")
        self.ticker_ribbon_layout = QHBoxLayout(self.ticker_ribbon)
        self.ticker_ribbon.setFixedHeight(44)
        self.ticker_ribbon_layout.setContentsMargins(10, 3, 10, 3)
        self.ticker_ribbon_layout.setSpacing(2)
        self.tabs = QTabWidget()
        self.market_tab = self._build_market_tab_r4()
        self.live_trader_tab = self._build_live_trader_tab_r4()
        self.research_tab = self._build_research_tab()
        self.watchlists_tab = self._build_watchlist_tab_r4()
        self.scanner_tab = self._build_scanner_tab()
        self.alerts_tab = self._build_alert_tab_r4()
        self.notes_tab = self._build_notes_tab_r4()
        self.charts_tab = self._build_charts_tab()
        self.exports_tab = self._build_exports_tab_r4()
        self.settings_tab = self._build_settings_tab_r4()

        self.tabs.addTab(self.market_tab, "Market")
        self.tabs.addTab(self.live_trader_tab, "Live Trader")
        self.tabs.addTab(self.research_tab, "Research")
        self.tabs.addTab(self.watchlists_tab, "Watchlists")
        self.tabs.addTab(self.scanner_tab, "Scanner")
        self.tabs.addTab(self.alerts_tab, "Alerts")
        self.tabs.addTab(self.notes_tab, "Notes")
        self.tabs.addTab(self.exports_tab, "Exports")
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.tabBar().hide()
        for letter, label in (
            ("M", "Market"), ("L", "Live Trader"), ("R", "Research"), ("W", "Watchlists"),
            ("S", "Scanner"), ("A", "Alerts"), ("N", "Notes"), ("E", "Exports"), ("⚙", "Settings"),
        ):
            item = QListWidgetItem(self._navigation_icon(letter), label)
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.navigation.setCurrentRow)
        self.navigation.setCurrentRow(max(0, min(8, int(self.app.settings.last_page))))
        if self.app.settings.ticker_position == "top":
            layout.addWidget(self.ticker_ribbon)
        layout.addWidget(self.tabs, 1)
        if self.app.settings.ticker_position == "bottom":
            layout.addWidget(self.ticker_ribbon)
        root_layout.addLayout(shell, 1)
        footer = QFrame()
        footer.setObjectName("status_footer")
        footer.setFixedHeight(58)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(22, 5, 22, 5)
        self.shell_market_state_text = QLabel("MARKET CLOSED")
        self._style_market_status(self.shell_market_state_text, "CLOSED")
        footer_layout.addWidget(self.shell_market_state_text)
        footer_layout.addWidget(QLabel("•"))
        self.offline_banner = QLabel("")
        self.offline_banner.setObjectName("offline_banner")
        self.offline_banner.setVisible(False)
        footer_layout.addWidget(self.offline_banner)
        footer_layout.addWidget(self.shell_freshness_text, 1)
        footer_layout.addWidget(self.shell_company_text)
        root_layout.addWidget(footer)
        self.ticker_ribbon.setVisible(self.app.settings.ticker_position != "hidden")
        return root

    def _build_research_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)
        heading_row = QHBoxLayout()
        heading_row.addWidget(self._surface_heading("Research", "Official SEC fundamentals, provider market data, and traceable calculations"), 1)
        heading_row.addStretch(1)
        self.research_period_combo = QComboBox()
        self.research_period_combo.addItem("Annual", "annual")
        self.research_period_combo.addItem("Quarterly", "quarterly")
        heading_row.addWidget(self.research_period_combo)
        refresh = QPushButton("Refresh Research")
        refresh.setProperty("primary", True)
        refresh.clicked.connect(self._on_research_refresh)
        heading_row.addWidget(refresh)
        export = QPushButton("Export Research CSV")
        export.clicked.connect(self._on_research_export)
        heading_row.addWidget(export)
        layout.addLayout(heading_row)
        research_header, header_layout = self._card("Research Active Symbol", "Unified company, quote, market-state, and SEC retrieval context")
        research_header.setObjectName("research_header")
        research_header_layout = QGridLayout()
        self.research_company_text.setObjectName("company_identity")
        self.research_quote_text.setObjectName("hero_price")
        research_logo_block = QWidget()
        research_logo_layout = QVBoxLayout(research_logo_block)
        research_logo_layout.setContentsMargins(0, 0, 0, 0)
        research_logo_layout.setSpacing(2)
        research_logo_layout.addWidget(self.research_symbol_avatar, 0, Qt.AlignmentFlag.AlignCenter)
        self.research_logo_attribution = QLabel("")
        self.research_logo_attribution.setObjectName("logo_attribution")
        self.research_logo_attribution.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.research_logo_attribution.setVisible(False)
        research_logo_layout.addWidget(self.research_logo_attribution)
        research_header_layout.addWidget(research_logo_block, 0, 0, 2, 1)
        research_header_layout.addWidget(self.research_company_text, 0, 1)
        research_header_layout.addWidget(self.research_quote_text, 1, 1)
        research_header_layout.addWidget(self.research_market_status_text, 0, 2)
        research_header_layout.addWidget(self.research_profile_text, 1, 2)
        header_layout.addLayout(research_header_layout)
        header_layout.addWidget(self.research_status_text)
        self.research_tabs = QTabWidget()
        self.research_tabs.setTabBar(LiteralAmpersandTabBar())
        sections = (
            "Overview", "Valuation", "Earnings", "Growth", "Financials", "Financial Health",
            "Performance", "Peers", "Analyst Outlook", "Catalysts & News",
        )
        for section in sections:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(8, 10, 8, 8)
            if section == "Overview":
                overview_content = QWidget()
                overview_layout = QVBoxLayout(overview_content)
                overview_layout.setContentsMargins(0, 0, 0, 0)
                overview_layout.setSpacing(8)
                research_header.setMaximumHeight(145)
                overview_layout.addWidget(research_header)
                dashboard = QGridLayout()
                dashboard.setSpacing(8)
                about_card, about_layout = self._card("Company Profile", "Official SEC identity and classification")
                self.research_profile_card_title = about_layout.itemAt(0).widget()
                self.research_profile_card_subtitle = about_layout.itemAt(1).widget()
                about_card.setMinimumHeight(155)
                about_layout.addWidget(self.research_about_text)
                about_layout.addWidget(self.research_profile_detail_text)
                dashboard.addWidget(about_card, 0, 0, 1, 2)
                market_card, market_layout = self._card("Key Metrics & Fundamentals", "Provider quote metadata and selected official SEC Company Facts")
                self.research_metrics_card_title = market_layout.itemAt(0).widget()
                self.research_metrics_card_subtitle = market_layout.itemAt(1).widget()
                market_card.setMinimumHeight(155)
                metric_columns = QHBoxLayout()
                metric_columns.addWidget(self.research_market_metrics_text, 1)
                metric_columns.addWidget(self.research_key_metrics_text, 1)
                market_layout.addLayout(metric_columns)
                dashboard.addWidget(market_card, 0, 2, 1, 3)
                chart_card, chart_layout = self._card("Price Chart", "Historical market-provider context")
                chart_card.setMinimumHeight(330)
                self.research_chart = MiniLineChart()
                self.research_chart.setMinimumHeight(270)
                chart_layout.addWidget(self.research_chart)
                dashboard.addWidget(chart_card, 1, 0, 1, 2)
                peers_card, peers_layout = self._card("Peers", "Curated comparables; click to change Active Symbol")
                peers_card.setMinimumHeight(330)
                self.research_overview_peers = QListWidget()
                self.research_overview_peers.itemDoubleClicked.connect(self._on_peer_activate)
                peers_layout.addWidget(self.research_overview_peers)
                dashboard.addWidget(peers_card, 1, 2)
                catalysts_card, catalysts_layout = self._card("Catalysts & News", "Official-source events only")
                catalysts_card.setMinimumHeight(330)
                self.research_overview_catalysts = QListWidget()
                self.research_overview_catalysts.setWordWrap(True)
                self.research_overview_catalysts.addItem("No official catalyst events received yet.")
                self.research_overview_catalysts.itemDoubleClicked.connect(self._open_catalyst_item)
                catalysts_layout.addWidget(self.research_overview_catalysts)
                dashboard.addWidget(catalysts_card, 1, 3, 1, 2)
                for column in range(5):
                    dashboard.setColumnStretch(column, 1)
                overview_layout.addLayout(dashboard)
                detail_card, detail_layout = self._card("Traceable Source Detail", "Provenance is compact by default and available on demand")
                self.research_provenance_card = detail_card
                self.research_provenance_toggle = QPushButton("Show traceable source detail")
                self.research_provenance_toggle.setCheckable(True)
                self.research_provenance_toggle.toggled.connect(self._set_research_provenance_expanded)
                detail_layout.addWidget(self.research_provenance_toggle)
                table = QTableWidget(0, 5)
                table.setHorizontalHeaderLabels(["Metric", "Value", "Period / Units", "Source / Filed", "Availability / Selection"])
                table.setObjectName("research_overview")
                self._configure_table(table, rows=4)
                table.setMaximumHeight(220)
                table.setVisible(False)
                self.research_provenance_table = table
                self.research_tables[section] = table
                detail_layout.addWidget(table)
                detail_card.setMaximumHeight(96)
                overview_layout.addWidget(detail_card)
                overview_scroll = self._scrollable(overview_content)
                overview_scroll.setObjectName("research_overview_scroll")
                page_layout.addWidget(overview_scroll)
            elif section == "Peers":
                self.comparison_tab = self._build_compare_tab()
                page_layout.addWidget(self.comparison_tab)
                peers_card, peers_layout = self._card("Curated Comparable Symbols", "Double-click to make Active Symbol")
                self.peer_list = QListWidget()
                self.peer_list.itemDoubleClicked.connect(self._on_peer_activate)
                peers_layout.addWidget(self.peer_list)
                page_layout.addWidget(peers_card)
            elif section == "Catalysts & News":
                catalyst_card, catalyst_layout = self._card("Official Catalysts & News", "Feeds follow the Active Symbol; no stories are fabricated")
                self.research_catalyst_list = QListWidget()
                self.research_catalyst_list.addItem("No official catalyst events received yet.")
                self.research_catalyst_list.itemDoubleClicked.connect(self._open_catalyst_item)
                catalyst_layout.addWidget(self.research_catalyst_list)
                page_layout.addWidget(catalyst_card)
            else:
                section_card, section_layout = self._card(section, "Traceable values, periods, units, sources, and selection reasons")
                table = QTableWidget(0, 5)
                table.setHorizontalHeaderLabels(["Metric", "Value", "Period / Units", "Source / Filed", "Availability / Selection"])
                table.setObjectName(f"research_{section.lower().replace(' ', '_')}")
                self._configure_table(table, rows=12)
                self.research_tables[section] = table
                if section == "Analyst Outlook":
                    self.research_analyst_empty_state = QLabel(
                        "Analyst data has not loaded yet. Configure an optional provider key to enable supported datasets."
                    )
                    self.research_analyst_empty_state.setObjectName("research_analyst_empty_state")
                    self.research_analyst_empty_state.setWordWrap(True)
                    self.research_analyst_empty_state.setMinimumHeight(92)
                    section_layout.addWidget(self.research_analyst_empty_state)
                    table.setVisible(False)
                    section_card.setMaximumHeight(260)
                section_layout.addWidget(table)
                if section == "Analyst Outlook":
                    page_layout.addWidget(section_card, 0, Qt.AlignmentFlag.AlignTop)
                    page_layout.addStretch(1)
                else:
                    page_layout.addWidget(section_card)
            self.research_tabs.addTab(page, section)
        layout.addWidget(self.research_tabs)
        return tab

    def _build_provider_combo(self) -> QComboBox:
        if QComboBox is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        combo = QComboBox()
        combo.setObjectName("market_provider_mode_selector")
        combo.addItem("Smart Search (Recommended)", "smart")
        for status in self.app.fabric_provider_statuses():
            provider_id = str(status["provider_id"])
            if not status["enabled"] or "quote" not in status["capabilities"]:
                continue
            label = str(status["display_name"])
            if status["requires_credentials"] and not status["configured"]:
                label += " — Missing API key"
            combo.addItem(label, provider_id)
            index = combo.count() - 1
            combo.model().item(index).setEnabled(not status["requires_credentials"] or bool(status["configured"]))
        current = combo.findData(self.app.settings.provider_mode)
        combo.setCurrentIndex(max(0, current))
        return combo

    def _build_provider_settings_selector(self) -> QComboBox:
        if QComboBox is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        combo = QComboBox()
        if self.app.registry is not None:
            for provider_id in self.app.registry.list_available():
                combo.addItem(PROVIDER_SETTINGS_LABELS.get(provider_id, provider_id), provider_id)
        return combo

    def _build_active_provider_combo(self) -> QComboBox:
        if QComboBox is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        combo = QComboBox()
        if self.app.registry is not None:
            for provider_id in self.app.registry.list_available():
                combo.addItem(PROVIDER_SETTINGS_LABELS.get(provider_id, provider_id), provider_id)
        current_index = combo.findData(self.provider.provider_id)
        combo.setCurrentIndex(current_index if current_index >= 0 else 0)
        return combo

    @staticmethod
    def _password_input(placeholder: str) -> QLineEdit:
        if QLineEdit is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        line_edit = QLineEdit()
        line_edit.setEchoMode(QLineEdit.EchoMode.Password)
        line_edit.setPlaceholderText(placeholder)
        return line_edit

    @staticmethod
    def _signup_button(label: str) -> QPushButton:
        button = QPushButton(label)
        button.setToolTip("Opens the provider's official signup page in your browser.")
        return button

    def _build_refresh_interval_combo(self) -> QComboBox:
        if QComboBox is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        combo = QComboBox()
        labels = {
            500: "0.5 seconds",
            1000: "1 second",
            10000: "10 seconds",
            30000: "30 seconds",
        }
        for interval_ms in ALLOWED_LIVE_REFRESH_INTERVALS_MS:
            combo.addItem(labels[interval_ms], interval_ms)
        current_index = combo.findData(self.app.settings.live_refresh_interval_ms)
        combo.setCurrentIndex(current_index if current_index >= 0 else combo.findData(10000))
        return combo

    def _build_market_tab(self) -> QWidget:
        if QWidget is None or QFormLayout is None or QHBoxLayout is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("primary", True)
        self.market_days_input.setSuffix(" days")

        hero, hero_layout = self._card("Active Symbol", "Unified quote, market state, ranges, and provider freshness")
        hero_top = QHBoxLayout()
        identity = QVBoxLayout()
        identity.addWidget(self.market_company_text)
        price_row = QHBoxLayout()
        self.price_text.setObjectName("hero_price")
        self.market_change_text.setObjectName("hero_change")
        price_row.addWidget(self.price_text)
        price_row.addWidget(self.market_change_text)
        price_row.addStretch(1)
        identity.addLayout(price_row)
        identity.addWidget(self.extended_hours_text)
        state_row = QHBoxLayout()
        state_row.addWidget(self.market_status_text)
        state_row.addWidget(self.last_updated_text)
        state_row.addStretch(1)
        identity.addLayout(state_row)
        hero_top.addLayout(identity, 3)
        hero_top.addWidget(self.market_range_text, 2)
        hero_top.addWidget(self.market_volume_text, 2)
        hero_top.addWidget(self.market_cap_text, 2)
        actions = QVBoxLayout()
        actions.addWidget(refresh_btn)
        actions.addWidget(self.provider_combo)
        hero_top.addLayout(actions, 1)
        hero_layout.addLayout(hero_top)
        hero_layout.addWidget(self.status_text)
        layout.addWidget(hero)

        body = QGridLayout()
        body.setHorizontalSpacing(10)
        body.setVerticalSpacing(10)
        chart_card, chart_layout = self._card("Price Chart", "Historical provider data • Active Symbol")
        chart_controls = QHBoxLayout()
        for days, label in ((30, "1M"), (90, "3M"), (180, "6M"), (365, "1Y"), (1095, "3Y")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=days: self.market_days_input.setValue(value))
            chart_controls.addWidget(button)
        chart_controls.addStretch(1)
        chart_controls.addWidget(QLabel("Range"))
        chart_controls.addWidget(self.market_days_input)
        chart_layout.addLayout(chart_controls)
        self.chart.setMinimumHeight(330)
        chart_layout.addWidget(self.chart, 1)
        chart_layout.addWidget(self.result_text)
        body.addWidget(chart_card, 0, 0, 2, 3)

        key_card, key_layout = self._card("Key Metrics", "Provider and SEC-derived values only")
        self.metrics_text.setWordWrap(True)
        key_layout.addWidget(self.metrics_text)
        key_layout.addStretch(1)
        body.addWidget(key_card, 0, 3)
        performance_card, performance_layout = self._card("Performance", "Selected historical window")
        performance_layout.addWidget(self.market_performance_text)
        performance_layout.addStretch(1)
        body.addWidget(performance_card, 1, 3)
        body.setColumnStretch(0, 2)
        body.setColumnStretch(1, 2)
        body.setColumnStretch(2, 2)
        body.setColumnStretch(3, 2)
        layout.addLayout(body, 1)

        lower = QHBoxLayout()
        related_card, related_layout = self._card("Related & Watched", "Click a ticker ribbon symbol to change Active Symbol")
        self.market_related_list = QListWidget()
        self.market_related_list.setMaximumHeight(150)
        self.market_related_list.addItem("Add symbols to a watchlist for related context.")
        related_layout.addWidget(self.market_related_list)
        lower.addWidget(related_card, 2)
        catalyst_card, catalyst_layout = self._card("Catalysts & Insights", "Official sources and calculated observations")
        self.insight_list.setMaximumHeight(150)
        catalyst_layout.addWidget(self.insight_list)
        lower.addWidget(catalyst_card, 3)
        overview_card, overview_layout = self._card("Market Overview", "No full-market coverage claim")
        overview_layout.addWidget(self.market_overview_text)
        overview_layout.addStretch(1)
        lower.addWidget(overview_card, 2)
        layout.addLayout(lower)

        history_card, history_layout = self._card("Recent Price History", "Traceable provider bars")
        self._configure_table(self.bars_table, rows=5)
        self.bars_table.setMaximumHeight(170)
        history_layout.addWidget(self.bars_table)
        layout.addWidget(history_card)

        refresh_btn.clicked.connect(self._on_refresh)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_market_tab_r4(self) -> QWidget:
        """Compose the Market surface to the approved R4 dashboard geometry."""
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.market_days_input.setSuffix(" days")
        self.market_days_input.setValue(30)

        hero, hero_layout = self._card("", "")
        hero.setObjectName("market_symbol_hero")
        hero.setMaximumHeight(170)
        hero_top = QHBoxLayout()
        hero_top.setSpacing(14)
        self.market_symbol_avatar = QLabel("AAPL")
        self.market_symbol_avatar.setObjectName("symbol_avatar")
        self.market_symbol_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.market_symbol_avatar.setFixedSize(82, 82)
        market_logo_block = QWidget()
        market_logo_layout = QVBoxLayout(market_logo_block)
        market_logo_layout.setContentsMargins(0, 0, 0, 0)
        market_logo_layout.setSpacing(2)
        market_logo_layout.addWidget(self.market_symbol_avatar, 0, Qt.AlignmentFlag.AlignCenter)
        self.market_logo_attribution = QLabel("")
        self.market_logo_attribution.setObjectName("logo_attribution")
        self.market_logo_attribution.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.market_logo_attribution.setVisible(False)
        market_logo_layout.addWidget(self.market_logo_attribution)
        hero_top.addWidget(market_logo_block)
        identity = QVBoxLayout()
        identity.addWidget(self.market_company_text)
        price_row = QHBoxLayout()
        self.price_text.setObjectName("hero_price")
        self.market_change_text.setObjectName("hero_change")
        price_row.addWidget(self.price_text)
        price_row.addWidget(self.market_change_text)
        price_row.addStretch(1)
        identity.addLayout(price_row)
        state_row = QHBoxLayout()
        state_row.addWidget(self.market_status_text)
        state_row.addWidget(self.last_updated_text)
        state_row.addStretch(1)
        identity.addLayout(state_row)
        hero_top.addLayout(identity, 4)
        hero_top.addWidget(self.market_range_text, 2)
        hero_top.addWidget(self.market_volume_text, 2)
        hero_top.addWidget(self.market_cap_text, 2)
        actions = QVBoxLayout()
        quick_actions = QHBoxLayout()
        for label, index in (("Add to Watchlist", 3), ("Compare", 2), ("Alerts", 5), ("Notes", 6)):
            action = QPushButton(label)
            if label == "Add to Watchlist":
                self.market_watchlist_button = action
                action.clicked.connect(self._on_add_active_symbol_to_watchlist)
            else:
                action.clicked.connect(lambda _checked=False, destination=index: self.tabs.setCurrentIndex(destination))
            quick_actions.addWidget(action)
        actions.addLayout(quick_actions)
        provider_actions = QHBoxLayout()
        provider_actions.addWidget(self.provider_combo, 1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("primary", True)
        refresh_btn.clicked.connect(self._on_refresh)
        provider_actions.addWidget(refresh_btn)
        provider_actions.addWidget(self.provider_details_btn)
        actions.addLayout(provider_actions)
        hero_top.addLayout(actions, 3)
        hero_layout.addLayout(hero_top)
        layout.addWidget(hero)

        body = QHBoxLayout()
        body.setSpacing(8)
        chart_card, chart_layout = self._card("Price Chart", "Closest supported provider window • Active Symbol")
        chart_controls = QHBoxLayout()
        self.market_range_buttons: dict[int, QPushButton] = {}
        for days, label in ((30, "1M"), (90, "3M"), (180, "6M"), (365, "1Y"), (1095, "3Y")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(days == 30)
            button.clicked.connect(lambda _checked=False, value=days: self._on_market_range_selected(value))
            chart_controls.addWidget(button)
            self.market_range_buttons[days] = button
        chart_controls.addWidget(QPushButton("Indicators"))
        chart_controls.addWidget(QPushButton("Studies"))
        chart_controls.addStretch(1)
        chart_controls.addWidget(QLabel("Range"))
        chart_controls.addWidget(self.market_days_input)
        chart_layout.addLayout(chart_controls)
        self.chart.setMinimumHeight(345)
        chart_layout.addWidget(self.chart, 1)
        chart_layout.addWidget(self.result_text)
        chart_layout.addWidget(self.provider_diagnostics_text)
        body.addWidget(chart_card, 68)

        right_context = QVBoxLayout()
        right_context.setSpacing(8)
        key_card, key_layout = self._card("Key Metrics", "Provider and SEC-derived values only")
        self.metrics_text.setWordWrap(True)
        key_layout.addWidget(self.metrics_text)
        right_context.addWidget(key_card, 3)
        performance_card, performance_layout = self._card("Performance", "Selected historical window")
        performance_layout.addWidget(self.market_performance_text)
        right_context.addWidget(performance_card, 2)
        analyst_card, analyst_layout = self._card("Analyst Availability", "No unsupported analyst consensus is inferred")
        self.market_analyst_text = QLabel("Unavailable from configured provider")
        self.market_analyst_text.setWordWrap(True)
        analyst_layout.addWidget(self.market_analyst_text)
        right_context.addWidget(analyst_card, 2)
        body.addLayout(right_context, 32)
        layout.addLayout(body, 1)

        lower = QHBoxLayout()
        lower.setSpacing(8)
        related_card, related_layout = self._card("Related & Watched", "Local watchlist context")
        self.market_related_list = QListWidget()
        self.market_related_list.setMaximumHeight(135)
        self.market_related_list.addItem("Add symbols to a watchlist for related context.")
        related_layout.addWidget(self.market_related_list)
        lower.addWidget(related_card, 32)
        catalyst_card, catalyst_layout = self._card("Catalysts & News", "Official sources and calculated observations")
        self.insight_list.setMaximumHeight(135)
        catalyst_layout.addWidget(self.insight_list)
        lower.addWidget(catalyst_card, 40)
        overview_card, overview_layout = self._card("Market Overview", "No full-market coverage claim")
        overview_layout.addWidget(self.market_overview_text)
        overview_layout.addStretch(1)
        lower.addWidget(overview_card, 28)
        layout.addLayout(lower)

        history_card, history_layout = self._card("Recent Price History", "Traceable provider bars")
        self._configure_table(self.bars_table, rows=4)
        self.bars_table.setMaximumHeight(150)
        history_layout.addWidget(self.bars_table)
        layout.addWidget(history_card)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_live_trader_tab_r4(self) -> QWidget:
        """Compose the analysis-only Live Trader footprint from the R4 blueprint."""
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 7, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._surface_heading("Live Trader", "Streaming analysis workstation • no brokerage connectivity or order execution"))

        top = QHBoxLayout()
        top.setSpacing(8)
        hero, hero_layout = self._card("Live Active Symbol", "Quote, stream health, ranges, and provider truth")
        hero.setMaximumHeight(145)
        hero_row = QHBoxLayout()
        self.live_symbol_text.setObjectName("company_identity")
        self.live_price_text.setObjectName("hero_price")
        self.live_change_text.setObjectName("hero_change")
        identity = QVBoxLayout()
        identity.addWidget(self.live_symbol_text)
        price = QHBoxLayout(); price.addWidget(self.live_price_text); price.addWidget(self.live_change_text); price.addStretch(1)
        identity.addLayout(price)
        identity.addWidget(self.live_market_status_text)
        hero_row.addLayout(identity, 3)
        for label, widget in (
            ("Stream", self.live_stream_status_text), ("Trade Time", self.live_trade_time_text),
            ("Provider", self.live_provider_text), ("Updated", self.live_last_update_text),
        ):
            block = QVBoxLayout()
            caption = QLabel(label.upper()); caption.setObjectName("metric_caption")
            block.addWidget(caption); block.addWidget(widget); block.addStretch(1)
            hero_row.addLayout(block, 1)
        market_detail = QVBoxLayout()
        market_detail.addWidget(QLabel("BID / ASK")); market_detail.addWidget(self.live_bid_text)
        market_detail.addWidget(self.live_ask_text); market_detail.addWidget(self.live_spread_text)
        hero_row.addLayout(market_detail, 1)
        hero_layout.addLayout(hero_row)
        top.addWidget(hero, 74)
        scanner_card, scanner_layout = self._card("Scanner Hits", "Permitted live universe only")
        self.live_scanner_context = QListWidget()
        self.live_scanner_context.addItem("No current scanner hits in the permitted live universe.")
        scanner_layout.addWidget(self.live_scanner_context)
        top.addWidget(scanner_card, 26)
        layout.addLayout(top)

        workspace = QHBoxLayout()
        workspace.setSpacing(8)
        left = QVBoxLayout(); left.setSpacing(8)
        chart_card, chart_layout = self._card("Live Chart", "Tick-driven candles and locally calculated indicators")
        interval_row = QHBoxLayout(); interval_row.addWidget(QLabel("INTERVAL"))
        for index, label in enumerate(("1s", "5s", "15s", "30s", "1m", "5m")):
            button = QPushButton(label); button.setCheckable(True); button.setChecked(index == 0)
            button.clicked.connect(lambda _checked=False, value=index: self.live_candle_interval.setCurrentIndex(value))
            interval_row.addWidget(button)
        for label in ("VWAP", "EMA 9", "EMA 20", "RSI 14", "Volume"):
            indicator = QPushButton(label); indicator.setCheckable(True); indicator.setChecked(True)
            interval_row.addWidget(indicator)
        interval_row.addStretch(1)
        chart_layout.addLayout(interval_row)
        self.live_chart = MiniLineChart(); self.live_chart.set_display_mode(MiniLineChart.CANDLESTICK_MODE); self.live_chart.setMinimumHeight(350)
        chart_layout.addWidget(self.live_chart, 1)
        self.live_indicators_text.setObjectName("indicator_strip")
        chart_layout.addWidget(self.live_indicators_text)
        left.addWidget(chart_card, 3)
        lower = QHBoxLayout(); lower.setSpacing(8)
        for title, message in (
            ("Order Flow", "Unavailable from configured provider"),
            ("Time & Sales", "No legitimate trade tape available in snapshot mode"),
            ("Ticker Strip", "Shared subscription state remains bounded"),
        ):
            card, card_layout = self._card(title, "Truthful analysis state")
            label = QLabel(message); label.setWordWrap(True); card_layout.addWidget(label); card_layout.addStretch(1)
            lower.addWidget(card, 1)
        left.addLayout(lower, 1)
        workspace.addLayout(left, 59)

        analysis = QVBoxLayout(); analysis.setSpacing(8)
        risk_card, risk_layout = self._card("Position Risk", "Calculation only • never sends an order")
        risk_form = QFormLayout()
        risk_form.addRow("Entry", self.risk_entry_input); risk_form.addRow("Stop", self.risk_stop_input)
        risk_form.addRow("Max risk", self.risk_max_input)
        risk_button = QPushButton("Calculate Risk"); risk_button.clicked.connect(self._on_calculate_risk)
        risk_form.addRow("", risk_button); risk_layout.addLayout(risk_form); risk_layout.addWidget(self.risk_result_text)
        analysis.addWidget(risk_card, 1)
        plan_card, plan_layout = self._card("Trade-Plan Analysis", "Setup, trigger, invalidation, target, and risk notes")
        self.trade_plan_text = QTextEdit(); self.trade_plan_text.setPlaceholderText("Record a non-executable analysis plan for the Active Symbol.")
        plan_layout.addWidget(self.trade_plan_text)
        analysis.addWidget(plan_card, 1)
        workspace.addLayout(analysis, 16)

        right = QVBoxLayout(); right.setSpacing(8)
        catalyst_group, catalyst_layout = self._card("Actionable Catalysts", "Official-source context; no prediction is fabricated")
        self.catalyst_list = QListWidget(); self.catalyst_list.addItem("No official catalyst events received yet.")
        self.catalyst_disclosure = QLabel(DIRECTION_DISCLOSURE); self.catalyst_disclosure.setWordWrap(True)
        catalyst_layout.addWidget(self.catalyst_list); catalyst_layout.addWidget(self.catalyst_disclosure)
        self.catalyst_list.itemDoubleClicked.connect(self._open_catalyst_item)
        right.addWidget(catalyst_group, 2)
        alerts_card, alerts_layout = self._card("Recent Alerts", "Runtime notifications for the current analysis universe")
        self.live_recent_alerts = QListWidget(); self.live_recent_alerts.addItem("No alerts triggered in this session.")
        alerts_layout.addWidget(self.live_recent_alerts)
        right.addWidget(alerts_card, 1)
        workspace.addLayout(right, 25)
        layout.addLayout(workspace, 1)
        wrapper = QVBoxLayout(tab); wrapper.setContentsMargins(0, 0, 0, 0); wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_live_trader_tab(self) -> QWidget:
        if QWidget is None or QGridLayout is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Live Trader", "Streaming analysis workstation • no brokerage connectivity or order execution"))

        hero, hero_layout = self._card("Live Active Symbol", "Stream health, price state, trade time, and provider truth")
        hero_row = QHBoxLayout()
        self.live_symbol_text.setObjectName("company_identity")
        self.live_price_text.setObjectName("hero_price")
        self.live_change_text.setObjectName("hero_change")
        for label, widget in (
            ("Symbol", self.live_symbol_text), ("Last", self.live_price_text), ("Change", self.live_change_text),
            ("Stream", self.live_stream_status_text), ("Market", self.live_market_status_text),
            ("Provider", self.live_provider_text), ("Updated", self.live_last_update_text),
        ):
            block = QVBoxLayout()
            caption = QLabel(label.upper())
            caption.setObjectName("metric_caption")
            block.addWidget(caption)
            block.addWidget(widget)
            hero_row.addLayout(block, 1)
        hero_layout.addLayout(hero_row)
        layout.addWidget(hero)

        body = QGridLayout()
        body.setSpacing(10)
        chart_card, chart_layout = self._card("Live Chart", "Tick-driven candles and locally calculated indicators")
        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("INTERVAL"))
        for index, label in enumerate(("1s", "5s", "15s", "30s", "1m", "5m")):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=index: self.live_candle_interval.setCurrentIndex(value))
            interval_row.addWidget(button)
        interval_row.addWidget(self.live_candle_interval)
        interval_row.addStretch(1)
        chart_layout.addLayout(interval_row)
        self.live_chart = MiniLineChart()
        self.live_chart.set_display_mode(MiniLineChart.CANDLESTICK_MODE)
        self.live_chart.setMinimumHeight(350)
        chart_layout.addWidget(self.live_chart, 1)
        self.live_indicators_text.setObjectName("indicator_strip")
        chart_layout.addWidget(self.live_indicators_text)
        body.addWidget(chart_card, 0, 0, 2, 3)

        scanner_card, scanner_layout = self._card("Scanner Hits", "Scoped to Active Symbol, subscriptions, and watchlists")
        self.live_scanner_context = QListWidget()
        self.live_scanner_context.addItem("No current scanner hits in the permitted live universe.")
        scanner_layout.addWidget(self.live_scanner_context)
        body.addWidget(scanner_card, 0, 3)

        live_metrics_card, live_metrics_layout = self._card("Live Market Detail", "Unavailable bid/ask fields remain explicit")
        live_grid = QGridLayout()
        for row, (label, widget) in enumerate((
            ("Trade time", self.live_trade_time_text), ("Bid", self.live_bid_text), ("Ask", self.live_ask_text),
            ("Spread", self.live_spread_text),
        )):
            live_grid.addWidget(QLabel(label), row, 0)
            live_grid.addWidget(widget, row, 1)
        live_metrics_layout.addLayout(live_grid)
        body.addWidget(live_metrics_card, 1, 3)

        risk_group = QGroupBox("Position Risk Calculator (calculation only — no order execution)")
        risk_layout = QFormLayout(risk_group)
        risk_layout.addRow("Entry", self.risk_entry_input)
        risk_layout.addRow("Stop", self.risk_stop_input)
        risk_layout.addRow("Maximum Dollar Risk", self.risk_max_input)
        risk_button = QPushButton("Calculate Risk")
        risk_layout.addRow("Action", risk_button)
        risk_layout.addRow("Result", self.risk_result_text)
        risk_button.clicked.connect(self._on_calculate_risk)
        body.addWidget(risk_group, 2, 0)

        plan_card, plan_layout = self._card("Trade-Plan Analysis", "Planning notes only • never sends an order")
        self.trade_plan_text = QTextEdit()
        self.trade_plan_text.setPlaceholderText("Record setup, trigger, invalidation, target, and risk notes for the Active Symbol.")
        self.trade_plan_text.setMaximumHeight(170)
        plan_layout.addWidget(self.trade_plan_text)
        body.addWidget(plan_card, 2, 1)

        catalyst_group, catalyst_layout = self._card("Actionable Catalysts", "Official-source context; direction labels are not predictions")
        self.catalyst_list = QListWidget()
        self.catalyst_list.addItem("No catalyst events received yet.")
        self.catalyst_disclosure = QLabel(DIRECTION_DISCLOSURE)
        self.catalyst_disclosure.setWordWrap(True)
        catalyst_layout.addWidget(self.catalyst_list)
        catalyst_layout.addWidget(self.catalyst_disclosure)
        self.catalyst_list.itemDoubleClicked.connect(self._open_catalyst_item)
        body.addWidget(catalyst_group, 2, 2)

        alerts_card, alerts_layout = self._card("Recent Alerts", "Runtime notifications for the current analysis universe")
        self.live_recent_alerts = QListWidget()
        self.live_recent_alerts.addItem("No recent alerts.")
        alerts_layout.addWidget(self.live_recent_alerts)
        body.addWidget(alerts_card, 2, 3)
        body.setColumnStretch(0, 2)
        body.setColumnStretch(1, 2)
        body.setColumnStretch(2, 2)
        body.setColumnStretch(3, 2)
        layout.addLayout(body, 1)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def set_catalyst_events(self, events: list[CorrelatedEvent]) -> None:
        if self._catalyst_dispatch_started is not None:
            self._performance_timings["catalyst_latency_ms"] = (
                perf_counter() - self._catalyst_dispatch_started
            ) * 1000.0
            self._catalyst_dispatch_started = None
        active_symbol = self.current_symbol.strip().upper()
        self._official_catalyst_events = [
            correlated
            for correlated in events
            if not correlated.event.symbols
            or active_symbol in {symbol.strip().upper() for symbol in correlated.event.symbols}
        ]
        self._render_catalyst_news()

    def _render_catalyst_news(self) -> None:
        active_symbol = self.current_symbol.strip().upper()
        official = list(self._official_catalyst_events)
        news = [
            event for event in self._provider_news_events
            if active_symbol in {value.strip().upper() for value in event.symbols}
        ]
        combined: list[tuple[CatalystEvent, str]] = [
            (item.event, item.event.relevance.value.title()) for item in official
        ] + [(event, "News") for event in news]
        deduped: list[tuple[CatalystEvent, str]] = []
        seen: set[tuple[str, str]] = set()
        for event, prefix in sorted(combined, key=lambda item: item[0].published_at, reverse=True):
            key = (event.title.casefold(), event.source_url)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((event, prefix))
        self.catalyst_list.clear()
        if hasattr(self, "research_catalyst_list"):
            self.research_catalyst_list.clear()
        if hasattr(self, "research_overview_catalysts"):
            self.research_overview_catalysts.clear()
        if not deduped:
            empty = f"No matching events or stories. {self._news_status_message}"
            self.catalyst_list.addItem(empty)
            if hasattr(self, "research_catalyst_list"):
                self.research_catalyst_list.addItem(empty)
            if hasattr(self, "research_overview_catalysts"):
                self.research_overview_catalysts.addItem(empty)
            return
        for event, prefix in deduped:
            age = human_duration(event.published_at)
            symbols = " · ".join(event.symbols) if event.symbols else "Broad Market"
            source_url = safe_source_url(event.source_url, official_only=event.source.lower() in {"sec", "nasdaq", "congress", "white house"})
            link_text = source_link_label(event.source) if source_url else "Source link unavailable"
            item = QListWidgetItem(
                f"{prefix} — {symbols}\n{human_event_title(event.title)}\n"
                f"{age} · {event.source} · {link_text}\n"
                f"{event.category.replace('_', ' ').title()} · Direction {event.direction.replace('_', ' ').title()}"
            )
            item.setData(Qt.ItemDataRole.UserRole, source_url)
            routed_symbol = active_symbol if active_symbol in {symbol.strip().upper() for symbol in event.symbols} else ""
            item.setData(int(Qt.ItemDataRole.UserRole) + 1, routed_symbol)
            self.catalyst_list.addItem(item)
            if hasattr(self, "research_catalyst_list"):
                research_item = item.clone()
                self.research_catalyst_list.addItem(research_item)
            if hasattr(self, "research_overview_catalysts"):
                self.research_overview_catalysts.addItem(item.clone())

    def _open_catalyst_item(self, item: QListWidgetItem) -> None:
        symbol = item.data(int(Qt.ItemDataRole.UserRole) + 1)
        if isinstance(symbol, str) and symbol:
            self.set_active_symbol(symbol, source="catalyst")
        url = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(url, str) and safe_source_url(url) and QDesktopServices is not None and QUrl is not None:
            QDesktopServices.openUrl(QUrl(url))

    def _on_calculate_risk(self) -> None:
        try:
            result = calculate_risk(
                Decimal(str(self.risk_entry_input.value())),
                Decimal(str(self.risk_stop_input.value())),
                Decimal(str(self.risk_max_input.value())),
            )
        except Exception as exc:
            self.risk_result_text.setText(str(exc))
            return
        self.risk_result_text.setText(
            f"Shares: {result.share_count} | Actual risk: ${result.actual_risk:.2f} | "
            f"Stop distance: ${result.distance_to_stop:.4f}"
        )

    def _build_watchlist_tab_r4(self) -> QWidget:
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 7, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._surface_heading("Watchlists", "Populated local watchlists synchronized through the global Active Symbol"))
        self.watchlist_id_input = QLineEdit(""); self.watchlist_id_input.setPlaceholderText("watchlist-id")
        self.watchlist_title_input = QLineEdit(""); self.watchlist_title_input.setPlaceholderText("Watchlist title")
        self.watchlist_symbol_input = QLineEdit(""); self.watchlist_symbol_input.setPlaceholderText("Symbol")
        self.watchlist_widget = QListWidget()
        create_btn = QPushButton("New / Save"); delete_btn = QPushButton("Delete")
        add_btn = QPushButton("Add Symbol"); remove_btn = QPushButton("Remove Symbol")

        body = QHBoxLayout(); body.setSpacing(8)
        manager_card, manager_layout = self._card("Watchlists", "Choose and manage a local list")
        manager_layout.addWidget(self.watchlist_widget, 1)
        manager_layout.addWidget(QLabel("WATCHLIST ID")); manager_layout.addWidget(self.watchlist_id_input)
        manager_layout.addWidget(QLabel("TITLE")); manager_layout.addWidget(self.watchlist_title_input)
        manager_layout.addWidget(self._hbox([create_btn, delete_btn]))
        manager_layout.addWidget(QLabel("WATCHLIST ACTIONS")); manager_layout.addWidget(self.watchlist_symbol_input)
        manager_layout.addWidget(add_btn); manager_layout.addWidget(remove_btn)
        body.addWidget(manager_card, 19)

        symbols_card, symbols_layout = self._card("My Watchlist", "Double-click any symbol row to change the global Active Symbol")
        self.watchlist_symbol_table = QTableWidget(0, 6)
        self.watchlist_symbol_table.setHorizontalHeaderLabels(["Symbol", "Price", "Change", "Volume", "Status", "Alerts"])
        self._configure_table(self.watchlist_symbol_table, rows=14)
        self.watchlist_symbol_table.cellDoubleClicked.connect(self._on_watchlist_symbol_activate)
        symbols_layout.addWidget(self.watchlist_symbol_table)
        body.addWidget(symbols_card, 49)

        detail_column = QVBoxLayout(); detail_column.setSpacing(8)
        detail_card, detail_layout = self._card("Active Symbol Detail", "Watchlist selection synchronized across RangeScout")
        self.watchlist_detail_symbol = QLabel("AAPL"); self.watchlist_detail_symbol.setObjectName("hero_price")
        self.watchlist_detail_price = QLabel("Price N/A • Change N/A")
        self.watchlist_detail_metrics = QLabel("Day range N/A\n52-week range N/A\nVolume N/A\nMarket cap N/A")
        self.watchlist_detail_metrics.setWordWrap(True)
        detail_layout.addWidget(self.watchlist_detail_symbol); detail_layout.addWidget(self.watchlist_detail_price)
        detail_layout.addWidget(self.watchlist_detail_metrics); detail_layout.addStretch(1)
        detail_column.addWidget(detail_card, 2)
        context_card, context_layout = self._card("Quick Notes & Context", "Local notes plus official/runtime availability")
        self.watchlist_context_list = QListWidget(); self.watchlist_context_list.addItem("No current context for the Active Symbol.")
        context_layout.addWidget(self.watchlist_context_list)
        detail_column.addWidget(context_card, 2)
        actions_card, actions_layout = self._card("Context Actions", "Open the Active Symbol without changing data")
        for label, index in (("Open in Market", 0), ("Open in Live Trader", 1), ("Open in Research", 2)):
            button = QPushButton(label); button.clicked.connect(lambda _checked=False, destination=index: self.tabs.setCurrentIndex(destination))
            actions_layout.addWidget(button)
        detail_column.addWidget(actions_card, 1)
        body.addLayout(detail_column, 32)
        layout.addLayout(body, 1)

        create_btn.clicked.connect(self._on_watchlist_create_or_update); delete_btn.clicked.connect(self._on_watchlist_delete)
        add_btn.clicked.connect(self._on_watchlist_add_symbol); remove_btn.clicked.connect(self._on_watchlist_remove_symbol)
        self.watchlist_widget.currentRowChanged.connect(lambda _: self._on_watchlist_select())
        self.watchlist_widget.itemDoubleClicked.connect(self._on_watchlist_activate)
        wrapper = QVBoxLayout(tab); wrapper.setContentsMargins(0, 0, 0, 0); wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_watchlist_tab(self) -> QWidget:
        if QWidget is None or QFormLayout is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Watchlists", "Organize symbols, inspect live context, and route selections through Active Symbol"))
        self.watchlist_id_input = QLineEdit("")
        self.watchlist_id_input.setPlaceholderText("watchlist-id")
        self.watchlist_title_input = QLineEdit("")
        self.watchlist_title_input.setPlaceholderText("Watchlist title")
        self.watchlist_symbol_input = QLineEdit("")
        self.watchlist_symbol_input.setPlaceholderText("AAPL")
        self.watchlist_widget = QListWidget()
        create_btn = QPushButton("Create")
        delete_btn = QPushButton("Delete")
        add_btn = QPushButton("Add Symbol")
        remove_btn = QPushButton("Remove Symbol")

        form = QFormLayout()
        form.addRow("Watchlist ID", self.watchlist_id_input)
        form.addRow("Title", self.watchlist_title_input)
        form.addRow("Symbol", self.watchlist_symbol_input)
        form.addRow("Actions", self._hbox([create_btn, delete_btn, add_btn, remove_btn]))

        body = QGridLayout()
        body.setSpacing(10)
        manager_card, manager_layout = self._card("Watchlist Manager", "Create lists and manage symbols locally")
        manager_layout.addLayout(form)
        manager_layout.addWidget(self.watchlist_widget, 1)
        body.addWidget(manager_card, 0, 0, 2, 1)

        symbols_card, symbols_layout = self._card("Symbols", "Double-click a row to update the global Active Symbol")
        self.watchlist_symbol_table = QTableWidget(0, 6)
        self.watchlist_symbol_table.setHorizontalHeaderLabels(["Symbol", "Price", "Change", "Volume", "Status", "Alerts"])
        self._configure_table(self.watchlist_symbol_table, rows=12)
        self.watchlist_symbol_table.cellDoubleClicked.connect(self._on_watchlist_symbol_activate)
        symbols_layout.addWidget(self.watchlist_symbol_table)
        body.addWidget(symbols_card, 0, 1, 2, 2)

        detail_card, detail_layout = self._card("Active Symbol Detail", "Watchlist selection remains synchronized across RangeScout")
        self.watchlist_detail_symbol = QLabel("AAPL")
        self.watchlist_detail_symbol.setObjectName("hero_price")
        self.watchlist_detail_price = QLabel("Price N/A • Change N/A")
        self.watchlist_detail_metrics = QLabel("Day range N/A\n52-week range N/A\nVolume N/A\nMarket cap N/A")
        self.watchlist_detail_metrics.setWordWrap(True)
        detail_layout.addWidget(self.watchlist_detail_symbol)
        detail_layout.addWidget(self.watchlist_detail_price)
        detail_layout.addWidget(self.watchlist_detail_metrics)
        detail_layout.addStretch(1)
        body.addWidget(detail_card, 0, 3)

        context_card, context_layout = self._card("Notes, Catalysts & Alerts", "Local notes and official/runtime context")
        self.watchlist_context_list = QListWidget()
        self.watchlist_context_list.addItem("No current context for the Active Symbol.")
        context_layout.addWidget(self.watchlist_context_list)
        body.addWidget(context_card, 1, 3)
        body.setColumnStretch(0, 2)
        body.setColumnStretch(1, 2)
        body.setColumnStretch(2, 2)
        body.setColumnStretch(3, 2)
        layout.addLayout(body, 1)

        create_btn.clicked.connect(self._on_watchlist_create_or_update)
        delete_btn.clicked.connect(self._on_watchlist_delete)
        add_btn.clicked.connect(self._on_watchlist_add_symbol)
        remove_btn.clicked.connect(self._on_watchlist_remove_symbol)
        self.watchlist_widget.currentRowChanged.connect(lambda _: self._on_watchlist_select())
        self.watchlist_widget.itemDoubleClicked.connect(self._on_watchlist_activate)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _on_watchlist_symbol_activate(self, row: int, _column: int) -> None:
        item = self.watchlist_symbol_table.item(row, 0)
        if item is not None and item.text().strip():
            self.set_active_symbol(item.text().strip(), source="watchlist-table")

    def _build_notes_tab_r4(self) -> QWidget:
        tab = QWidget(); page = QWidget()
        layout = QVBoxLayout(page); layout.setContentsMargins(8, 7, 8, 8); layout.setSpacing(8)
        layout.addWidget(self._surface_heading("Notes", "Local research notes and trade-journal organization linked to Active Symbol"))
        hero, hero_layout = self._card("Active Symbol Notes", "Local-only content; provider values remain separately sourced")
        hero.setMaximumHeight(115)
        hero_row = QHBoxLayout()
        self.notes_hero_symbol = QLabel("AAPL"); self.notes_hero_symbol.setObjectName("company_identity")
        self.notes_hero_price = QLabel("Price N/A • Change N/A"); self.notes_hero_price.setObjectName("hero_price")
        self.notes_hero_market = QLabel("MARKET STATUS PENDING")
        hero_row.addWidget(self.notes_hero_symbol, 2); hero_row.addWidget(self.notes_hero_price, 3)
        hero_row.addWidget(self.notes_hero_market, 2); hero_row.addStretch(2)
        hero_layout.addLayout(hero_row); layout.addWidget(hero)

        self.notes_symbol_input = QLineEdit("AAPL"); self.notes_symbol_input.setPlaceholderText("AAPL")
        self.notes_text = QTextEdit(); self.notes_text.setPlaceholderText("Write a note to capture your idea, thesis, or reminder.")
        self.notes_list = QListWidget(); save_btn = QPushButton("Save Note"); new_btn = QPushButton("New Note"); delete_btn = QPushButton("Delete Note"); refresh_btn = QPushButton("Reload Notes")
        body = QHBoxLayout(); body.setSpacing(8)
        categories_card, categories_layout = self._card("Note Categories", "Local organization")
        self.note_categories = QListWidget()
        for category in ("Trade Journal", "Research Notes", "Earnings Notes", "Catalyst Notes", "General"):
            self.note_categories.addItem(category)
        categories_layout.addWidget(self.note_categories); categories_layout.addStretch(1)
        body.addWidget(categories_card, 16)
        list_card, list_layout = self._card("BA Notes", "Newest local notes first")
        symbol_row = QHBoxLayout(); symbol_row.addWidget(QLabel("Linked symbol")); symbol_row.addWidget(self.notes_symbol_input)
        list_layout.addLayout(symbol_row); list_layout.addWidget(self.notes_list)
        body.addWidget(list_card, 22)
        editor_card, editor_layout = self._card("Note Editor", "Capture thesis, levels, catalysts, and reminders")
        self.note_editor_title = QLabel("Research note for AAPL"); self.note_editor_title.setObjectName("company_identity")
        editor_layout.addWidget(self.note_editor_title); editor_layout.addWidget(self.notes_text, 1)
        self.note_editor_mode = QLabel("New Note")
        self.note_editor_mode.setObjectName("note_editor_mode")
        editor_layout.addWidget(self.note_editor_mode)
        editor_layout.addWidget(self._hbox([new_btn, save_btn, delete_btn, refresh_btn]))
        body.addWidget(editor_card, 62)
        layout.addLayout(body, 1)
        self.note_categories.setCurrentRow(1)
        self.note_categories.currentTextChanged.connect(self._on_note_category_changed)
        self.notes_list.itemClicked.connect(self._on_note_selected)
        self.notes_text.textChanged.connect(self._on_note_text_changed)
        self.notes_symbol_input.textEdited.connect(lambda _text: self._on_note_text_changed())
        new_btn.clicked.connect(self._on_new_note)
        save_btn.clicked.connect(self._on_save_note)
        delete_btn.clicked.connect(self._on_delete_note)
        refresh_btn.clicked.connect(self._on_reload_notes)
        wrapper = QVBoxLayout(tab); wrapper.setContentsMargins(0, 0, 0, 0); wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_notes_tab(self) -> QWidget:
        if QWidget is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Notes", "Local research notes and trade-journal organization linked to Active Symbol"))
        self.notes_symbol_input = QLineEdit("AAPL")
        self.notes_symbol_input.setPlaceholderText("AAPL")
        self.notes_text = QTextEdit()
        self.notes_text.setPlaceholderText("Write a note to capture your idea, thesis, or reminder.")
        self.notes_list = QListWidget()
        add_btn = QPushButton("Add Note")
        refresh_btn = QPushButton("Reload Notes")
        btn_row = self._hbox([add_btn, refresh_btn])
        body = QHBoxLayout()
        categories_card, categories_layout = self._card("Note Categories", "Local organization")
        self.note_categories = QListWidget()
        for category in ("Trade Journal", "Research Notes", "Earnings Notes", "Catalyst Notes", "General"):
            self.note_categories.addItem(category)
        categories_layout.addWidget(self.note_categories)
        body.addWidget(categories_card, 2)

        list_card, list_layout = self._card("Active Symbol Notes", "Newest local notes first")
        symbol_row = QHBoxLayout()
        symbol_row.addWidget(QLabel("Linked symbol"))
        symbol_row.addWidget(self.notes_symbol_input)
        list_layout.addLayout(symbol_row)
        list_layout.addWidget(self.notes_list)
        body.addWidget(list_card, 3)

        editor_card, editor_layout = self._card("Note Editor", "Capture thesis, levels, catalysts, and reminders")
        note_title = QLabel("Research note for AAPL")
        note_title.setObjectName("company_identity")
        self.note_editor_title = note_title
        editor_layout.addWidget(note_title)
        editor_layout.addWidget(self.notes_text, 1)
        editor_layout.addWidget(btn_row)
        body.addWidget(editor_card, 5)
        layout.addLayout(body, 1)
        add_btn.clicked.connect(self._on_add_note)
        refresh_btn.clicked.connect(self._on_reload_notes)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_charts_tab(self) -> QWidget:
        if QWidget is None or QFormLayout is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(QLabel("Charts"))
        layout.addWidget(QLabel("Symbol"))
        layout.addWidget(self.chart_symbol_input)
        layout.addWidget(QLabel("Range Days"))
        layout.addWidget(self.chart_days_input)
        self.chart_days_input.setSuffix(" days")
        refresh_btn = QPushButton("Refresh Chart")
        refresh_btn.clicked.connect(self._on_refresh_chart)
        layout.addWidget(refresh_btn)
        layout.addWidget(self.chart_tab_chart)
        layout.addWidget(self.chart_error_text)
        return tab

    def _build_exports_tab_r4(self) -> QWidget:
        tab = QWidget(); page = QWidget()
        layout = QVBoxLayout(page); layout.setContentsMargins(8, 7, 8, 8); layout.setSpacing(8)
        header = QFrame(); header.setProperty("dashboardCard", True)
        header_layout = QHBoxLayout(header); header_layout.setContentsMargins(12, 9, 12, 9)
        heading = QVBoxLayout(); title = QLabel("Exports"); title.setObjectName("surface_title")
        heading.addWidget(title); heading.addWidget(QLabel("Export supported local data without changing application state"))
        header_layout.addLayout(heading, 1)
        symbol_button = QPushButton("Export Current Symbol"); symbol_button.setProperty("primary", True)
        research_button = QPushButton("Export Research CSV")
        symbol_button.clicked.connect(self._on_export_csv); research_button.clicked.connect(self._on_research_export)
        header_layout.addWidget(symbol_button); header_layout.addWidget(research_button)
        layout.addWidget(header)

        body = QHBoxLayout(); body.setSpacing(8)
        left = QVBoxLayout(); left.setSpacing(8)
        presets_card, presets_layout = self._card("Export Presets", "Only implemented CSV outputs are offered")
        preset_grid = QGridLayout(); preset_grid.setSpacing(8)
        presets = (
            ("Current Symbol CSV", "Loaded historical price bars", self._on_export_csv),
            ("Price History CSV", "Traceable provider OHLCV rows", self._on_export_csv),
            ("Research Snapshot CSV", "Loaded SEC/provider research values", self._on_research_export),
            ("Fundamentals CSV", "Traceable official-source metrics", self._on_research_export),
        )
        for index, (title_text, detail, callback) in enumerate(presets):
            card, card_layout = self._card(title_text, detail)
            action = QPushButton("Export"); action.clicked.connect(callback); card_layout.addWidget(action)
            preset_grid.addWidget(card, index // 2, index % 2)
        presets_layout.addLayout(preset_grid); left.addWidget(presets_card, 2)
        recent_card, recent_layout = self._card("Recent Exports", "Actual completed export actions in this QA profile")
        self.export_history_list = QListWidget(); self.export_history_list.addItem("No exports in this session.")
        recent_layout.addWidget(self.export_history_list); recent_layout.addWidget(self.export_result)
        left.addWidget(recent_card, 3); body.addLayout(left, 80)
        options_card, options_layout = self._card("Data Packaging Options", "Current implementation")
        options_layout.addWidget(QLabel("DATE RANGE\nLoaded provider window"))
        options_layout.addWidget(QLabel("FORMAT\nCSV (UTF-8)"))
        options_layout.addWidget(QLabel("DESTINATION\nDocuments\\RangeScoutExports"))
        options_layout.addWidget(QLabel("INCLUDES\nPrice bars or traceable research values"))
        options_layout.addStretch(1); body.addWidget(options_card, 20)
        layout.addLayout(body, 1)
        wrapper = QVBoxLayout(tab); wrapper.setContentsMargins(0, 0, 0, 0); wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_exports_tab(self) -> QWidget:
        if QWidget is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Exports", "Create supported local CSV exports without changing application data"))

        quick_row = QHBoxLayout()
        symbol_button = QPushButton("Export Current Symbol CSV")
        symbol_button.setProperty("primary", True)
        symbol_button.clicked.connect(self._on_export_csv)
        research_button = QPushButton("Export Traceable Research CSV")
        research_button.clicked.connect(self._on_research_export)
        quick_row.addWidget(symbol_button)
        quick_row.addWidget(research_button)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)

        body = QGridLayout()
        presets_card, presets_layout = self._card("Export Presets", "Only implemented and tested CSV formats are offered")
        preset_grid = QGridLayout()
        current_card, current_layout = self._card("Current Symbol CSV", "Historical price bars for the Active Symbol")
        current_action = QPushButton("Export")
        current_action.clicked.connect(self._on_export_csv)
        current_layout.addWidget(current_action)
        preset_grid.addWidget(current_card, 0, 0)
        research_card, research_layout = self._card("Research CSV", "Traceable SEC/provider values with provenance")
        research_action = QPushButton("Export")
        research_action.clicked.connect(self._on_research_export)
        research_layout.addWidget(research_action)
        preset_grid.addWidget(research_card, 0, 1)
        presets_layout.addLayout(preset_grid)
        body.addWidget(presets_card, 0, 0, 1, 3)

        options_card, options_layout = self._card("Data Packaging Options", "Current implementation")
        options_layout.addWidget(QLabel("Format\nCSV (UTF-8)"))
        options_layout.addWidget(QLabel("Destination\nDocuments\\RangeScoutExports"))
        options_layout.addWidget(QLabel("Scope\nActive Symbol or loaded Research snapshot"))
        options_layout.addStretch(1)
        body.addWidget(options_card, 0, 3, 2, 1)

        recent_card, recent_layout = self._card("Recent Exports", "This session's completed export activity")
        self.export_history_list = QListWidget()
        self.export_history_list.addItem("No exports in this session.")
        recent_layout.addWidget(self.export_history_list)
        recent_layout.addWidget(self.export_result)
        body.addWidget(recent_card, 1, 0, 1, 3)
        body.setColumnStretch(0, 2)
        body.setColumnStretch(1, 2)
        body.setColumnStretch(2, 2)
        body.setColumnStretch(3, 2)
        layout.addLayout(body, 1)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_alert_tab_r4(self) -> QWidget:
        tab = QWidget(); page = QWidget()
        layout = QVBoxLayout(page); layout.setContentsMargins(8, 7, 8, 8); layout.setSpacing(8)
        layout.addWidget(self._surface_heading("Alerts", "Local analysis rules, cooldown behavior, and notification channels"))
        hero, hero_layout = self._card("Active Symbol", "Alert rules remain bound to the global symbol context")
        hero.setMaximumHeight(112)
        hero_row = QHBoxLayout()
        self.alert_hero_symbol = QLabel("AAPL"); self.alert_hero_symbol.setObjectName("company_identity")
        self.alert_hero_price = QLabel("Price N/A • Change N/A"); self.alert_hero_price.setObjectName("hero_price")
        self.alert_hero_market = QLabel("MARKET STATUS PENDING")
        hero_row.addWidget(self.alert_hero_symbol, 2); hero_row.addWidget(self.alert_hero_price, 3)
        hero_row.addWidget(self.alert_hero_market, 2); hero_row.addStretch(2)
        hero_layout.addLayout(hero_row); layout.addWidget(hero)
        subnav = QHBoxLayout()
        for index, label in enumerate(("Alert Dashboard", "Alert Log", "Alert History", "Snoozed Alerts")):
            button = QPushButton(label); button.setCheckable(True); button.setChecked(index == 0); subnav.addWidget(button)
        subnav.addStretch(1); layout.addLayout(subnav)

        self.alert_symbol_input = QLineEdit("AAPL"); self.alert_mode_input = QComboBox()
        self.alert_mode_input.addItems(["percent_change", "relative_to_high", "drawdown", "stale_data"])
        self.alert_threshold_input = QDoubleSpinBox(); self.alert_threshold_input.setRange(0, 1000)
        self.alert_threshold_input.setDecimals(2); self.alert_threshold_input.setValue(5.0); self.alert_threshold_input.setSuffix(" %")
        self.live_alert_type_input = QComboBox()
        for alert_type in AlertType:
            self.live_alert_type_input.addItem(alert_type.value.replace("_", " ").title(), alert_type.value)
        self.alert_sound_input = QComboBox(); self.alert_sound_input.addItems(["Off", "On"])
        self.alert_desktop_input = QComboBox(); self.alert_desktop_input.addItems(["Off", "On"])
        add_btn = QPushButton("+ New Alert"); add_btn.setProperty("primary", True)
        live_add_btn = QPushButton("Enable Live Alert"); evaluate_btn = QPushButton("Evaluate")

        body = QHBoxLayout(); body.setSpacing(8)
        active_card, active_layout = self._card("Your Alerts", "User-configured local price, volume, and analysis rules")
        controls = QHBoxLayout(); controls.addWidget(self.alert_symbol_input); controls.addWidget(self.alert_mode_input)
        controls.addWidget(self.alert_threshold_input); controls.addWidget(add_btn); controls.addWidget(evaluate_btn)
        active_layout.addLayout(controls)
        self.alert_list.addItem("No active alerts configured."); active_layout.addWidget(self.alert_list)
        live_controls = QHBoxLayout(); live_controls.addWidget(self.live_alert_type_input); live_controls.addWidget(live_add_btn)
        live_controls.addStretch(1); active_layout.addLayout(live_controls)
        body.addWidget(active_card, 58)
        right = QVBoxLayout(); right.setSpacing(8)
        history_card, history_layout = self._card("Recently Triggered", "Actual local session history only")
        self.alert_history_list = QListWidget(); self.alert_history_list.addItem("No alerts triggered in this session.")
        history_layout.addWidget(self.alert_history_list); right.addWidget(history_card, 1)
        market_card, market_layout = self._card("Market Alerts", "Exchange, regulatory, halt, and resumption events")
        self.market_alert_filter = QComboBox()
        self.market_alert_filter.addItems(("All", "Trading Halts", "Resumptions", "Regulatory", "Watchlist"))
        self.market_alert_filter.currentTextChanged.connect(self._render_market_alerts)
        market_layout.addWidget(self.market_alert_filter)
        self.market_alert_list = QListWidget()
        self.market_alert_list.addItem("No current market notices from the checked official sources.")
        market_layout.addWidget(self.market_alert_list)
        right.addWidget(market_card, 1)
        preferences_card, preferences_layout = self._card("Notification Settings", "Visual state is explicit; sound and desktop are optional")
        preferences = QFormLayout(); preferences.addRow("In-app visual", QLabel("Enabled"))
        preferences.addRow("Sound", self.alert_sound_input); preferences.addRow("Desktop", self.alert_desktop_input)
        preferences.addRow("Duplicate cooldown", QLabel("60 seconds")); preferences_layout.addLayout(preferences)
        self.alert_context_text = QLabel("Active Symbol AAPL\nMarket state pending\nProvider state pending")
        self.alert_context_text.setWordWrap(True); preferences_layout.addWidget(self.alert_context_text)
        right.addWidget(preferences_card, 1); body.addLayout(right, 42)
        layout.addLayout(body, 1)
        add_btn.clicked.connect(self._on_alert_add); live_add_btn.clicked.connect(self._on_live_alert_add)
        evaluate_btn.clicked.connect(self._on_alert_evaluate)
        wrapper = QVBoxLayout(tab); wrapper.setContentsMargins(0, 0, 0, 0); wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_alert_tab(self) -> QWidget:
        if QWidget is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Alerts", "Configure local analysis alerts, cooldown behavior, and notification channels"))
        self.alert_symbol_input = QLineEdit("AAPL")
        self.alert_symbol_input.setPlaceholderText("AAPL")
        self.alert_mode_input = QComboBox()
        self.alert_mode_input.addItems(["percent_change", "relative_to_high", "drawdown", "stale_data"])
        self.alert_threshold_input = QDoubleSpinBox()
        self.alert_threshold_input.setRange(0, 1000)
        self.alert_threshold_input.setDecimals(2)
        self.alert_threshold_input.setValue(5.0)
        self.alert_threshold_input.setSuffix(" %")
        self.live_alert_type_input = QComboBox()
        for alert_type in AlertType:
            self.live_alert_type_input.addItem(alert_type.value.replace("_", " ").title(), alert_type.value)
        self.alert_sound_input = QComboBox(); self.alert_sound_input.addItems(["Off", "On"])
        self.alert_desktop_input = QComboBox(); self.alert_desktop_input.addItems(["Off", "On"])
        add_btn = QPushButton("Add Rule")
        live_add_btn = QPushButton("Enable Live Alert")
        evaluate_btn = QPushButton("Evaluate")
        form = QFormLayout()
        form.addRow("Symbol", self.alert_symbol_input)
        form.addRow("Mode", self.alert_mode_input)
        form.addRow("Threshold", self.alert_threshold_input)
        form.addRow("Actions", self._hbox([add_btn, evaluate_btn]))
        form.addRow("Live Alert Type", self.live_alert_type_input)
        form.addRow("Live Alert Action", live_add_btn)
        body = QGridLayout()
        body.setSpacing(10)
        create_card, create_layout = self._card("Create / Edit Alert", "Rules apply to the Active Symbol and permitted runtime universe")
        create_layout.addLayout(form)
        body.addWidget(create_card, 0, 0)

        active_card, active_layout = self._card("Active Alerts", "Rule type, threshold, severity, and runtime state")
        self.alert_list.addItem("No active alerts configured.")
        active_layout.addWidget(self.alert_list)
        body.addWidget(active_card, 0, 1, 2, 2)

        history_card, history_layout = self._card("Recently Triggered", "Local session history")
        self.alert_history_list = QListWidget()
        self.alert_history_list.addItem("No alerts triggered in this session.")
        history_layout.addWidget(self.alert_history_list)
        body.addWidget(history_card, 0, 3)

        preferences_card, preferences_layout = self._card("Notification Preferences", "Visual is always explicit; sound and desktop are optional")
        preferences = QGridLayout()
        preferences.addWidget(QLabel("Channel"), 0, 0)
        preferences.addWidget(QLabel("State"), 0, 1)
        preferences.addWidget(QLabel("In-app visual"), 1, 0)
        preferences.addWidget(QLabel("Enabled"), 1, 1)
        preferences.addWidget(QLabel("Sound"), 2, 0)
        preferences.addWidget(self.alert_sound_input, 2, 1)
        preferences.addWidget(QLabel("Desktop"), 3, 0)
        preferences.addWidget(self.alert_desktop_input, 3, 1)
        preferences.addWidget(QLabel("Duplicate cooldown"), 4, 0)
        preferences.addWidget(QLabel("60 seconds"), 4, 1)
        preferences_layout.addLayout(preferences)
        preferences_layout.addStretch(1)
        body.addWidget(preferences_card, 1, 0)

        context_card, context_layout = self._card("Alert Context", "Market and provider state remain visible")
        self.alert_context_text = QLabel("Active Symbol AAPL\nMarket state pending\nProvider state pending")
        self.alert_context_text.setWordWrap(True)
        context_layout.addWidget(self.alert_context_text)
        context_layout.addStretch(1)
        body.addWidget(context_card, 1, 3)
        body.setColumnStretch(0, 2)
        body.setColumnStretch(1, 2)
        body.setColumnStretch(2, 2)
        body.setColumnStretch(3, 2)
        layout.addLayout(body, 1)
        add_btn.clicked.connect(self._on_alert_add)
        live_add_btn.clicked.connect(self._on_live_alert_add)
        evaluate_btn.clicked.connect(self._on_alert_evaluate)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _on_live_alert_add(self) -> None:
        alert_type = str(self.live_alert_type_input.currentData())
        if hasattr(self, "runtime"):
            self.runtime.configure_alerts(
                AlertPreferences(
                    enabled=frozenset({AlertType(alert_type)}),
                    visual=True,
                    sound=self.alert_sound_input.currentText() == "On",
                    desktop=self.alert_desktop_input.currentText() == "On",
                    duplicate_cooldown_seconds=60,
                )
            )
        self.alert_list.addItem(
            f"Live runtime alert enabled: {alert_type} | visual=on | sound={self.alert_sound_input.currentText().lower()} | "
            f"desktop={self.alert_desktop_input.currentText().lower()} | duplicate suppression=60s"
        )

    def _build_compare_tab(self) -> QWidget:
        if QWidget is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self.compare_symbol_input = QLineEdit("AAPL")
        self.compare_benchmark_input = QLineEdit("SPY")
        self.compare_symbol_input.setPlaceholderText("AAPL")
        self.compare_benchmark_input.setPlaceholderText("SPY")
        compare_btn = QPushButton("Compare")
        layout.addWidget(QLabel("Comparison"))
        layout.addWidget(QLabel("Symbol"))
        layout.addWidget(self.compare_symbol_input)
        layout.addWidget(QLabel("Benchmark"))
        layout.addWidget(self.compare_benchmark_input)
        layout.addWidget(compare_btn)
        layout.addWidget(self.comparison_result)
        compare_btn.clicked.connect(self._on_compare)
        return tab

    def _build_scanner_tab(self) -> QWidget:
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Scanner", "Aggregated latest market feed for the eligible Active Symbol and watchlist universe"))
        self.scanner_status_text = QLabel("Latest Available • local/cache first • awaiting provider-backed rows")
        self.scanner_status_text.setWordWrap(True)
        layout.addWidget(self.scanner_status_text)

        summary = QHBoxLayout()
        self.scanner_total_text = QLabel("0")
        self.scanner_gainers_text = QLabel("N/A")
        self.scanner_breakouts_text = QLabel("N/A")
        self.scanner_catalysts_text = QLabel("0")
        self.scanner_halted_text = QLabel("N/A")
        self.scanner_market_text = QLabel("Explicit")
        for title, value, note in (
            ("Eligible Rows", self.scanner_total_text, "permitted universe"),
            ("Gainers", self.scanner_gainers_text, "provider-dependent"),
            ("Breakouts", self.scanner_breakouts_text, "configured rules"),
            ("Catalyst Hits", self.scanner_catalysts_text, "official sources"),
            ("Halted", self.scanner_halted_text, "runtime state only"),
            ("Market Status", self.scanner_market_text, "explicit text"),
        ):
            card, card_layout = self._card(title, note)
            value.setObjectName("summary_value")
            card_layout.addWidget(value)
            summary.addWidget(card, 1)
        layout.addLayout(summary)

        filters_card, filters_layout = self._card("Scanner Filters", "Available analysis views")
        filter_row = QHBoxLayout()
        self.scanner_filter_buttons: dict[str, QPushButton] = {}
        for label in ("All Live", "Top Gainers", "Relative Volume", "Breakout", "Opening Range", "VWAP Cross", "News Catalyst", "Watchlist Only"):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setChecked(label == "All Live")
            chip.clicked.connect(lambda _checked=False, selected=label: self._on_scanner_filter(selected))
            self.scanner_filter_buttons[label] = chip
            filter_row.addWidget(chip)
        filter_row.addStretch(1)
        filters_layout.addLayout(filter_row)
        layout.addWidget(filters_card)

        body = QHBoxLayout()
        results_card, results_layout = self._card("All Live / Latest Available", "Progressive, source-attributed rows; double-click to open in Live Trader")
        self.scanner_results = QListWidget()
        self.scanner_results.addItem("Latest Available — awaiting cached or provider-backed rows for the permitted universe.")
        results_layout.addWidget(self.scanner_results)
        body.addWidget(results_card, 75)

        detail_card, detail_layout = self._card("Selected Result", "Active Symbol context and setup summary")
        self.scanner_detail_symbol = QLabel("AAPL")
        self.scanner_detail_symbol.setObjectName("hero_price")
        self.scanner_detail_text = QLabel("Price N/A\nChange N/A\nSetup N/A\nRelative volume N/A")
        self.scanner_detail_text.setWordWrap(True)
        self.scanner_detail_chart = MiniLineChart()
        self.scanner_detail_chart.setMinimumHeight(220)
        detail_layout.addWidget(self.scanner_detail_symbol)
        detail_layout.addWidget(self.scanner_detail_text)
        detail_layout.addWidget(self.scanner_detail_chart)
        body.addWidget(detail_card, 25)
        layout.addLayout(body, 1)
        self.scanner_results.itemDoubleClicked.connect(self._on_scanner_activate)
        self.scanner_results.itemClicked.connect(self._on_scanner_selected)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _on_scanner_activate(self, item: QListWidgetItem) -> None:
        symbol = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(symbol, str) and symbol:
            self.set_active_symbol(symbol, source="scanner", destination=self.live_trader_tab)

    def _on_scanner_filter(self, selected: str) -> None:
        for label, button in self.scanner_filter_buttons.items():
            button.blockSignals(True)
            button.setChecked(label == selected)
            button.blockSignals(False)
        self._active_scanner_filter = selected
        self._render_scanner_rows()

    def _on_scanner_selected(self, item: QListWidgetItem) -> None:
        symbol = item.data(Qt.ItemDataRole.UserRole)
        row = next((value for value in getattr(self, "_scanner_rows", []) if value.symbol == symbol), None)
        if row is None:
            return
        self.scanner_detail_symbol.setText(row.symbol)
        change = "N/A" if row.change is None else f"{row.change:+.2f} ({row.change_percent:+.2f}%)"
        self.scanner_detail_text.setText(
            f"Company  {row.company}\nPrice  {row.price:,.2f}\nChange  {change}\n"
            f"Volume  {'N/A' if row.volume is None else f'{row.volume:,}'}\n"
            f"Sources  {', '.join(row.sources) or 'Local cache'}\nFreshness  {row.freshness}"
        )

    def _render_scanner_rows(self) -> None:
        rows = filter_scanner_rows(
            list(getattr(self, "_scanner_rows", [])),
            getattr(self, "_active_scanner_filter", "All Live"),
            set(getattr(self, "_ticker_watchlist_symbols", [])),
        )
        self.scanner_results.clear()
        rule_hits = getattr(self, "_scanner_rule_hits", {})
        for scan in rows:
            movement = "N/A" if scan.change_percent is None else f"{scan.change:+.2f} ({scan.change_percent:+.2f}%)"
            volume = "N/A" if scan.volume is None else f"{scan.volume:,}"
            annotations = rule_hits.get(scan.symbol, [])
            rule_text = "" if not annotations else " · Rules " + "; ".join(
                f"{humanize_event_code(hit.rule)} ({hit.rule}): {humanize_status_text(hit.detail)}"
                for hit in annotations
            )
            item = QListWidgetItem(
                f"{scan.symbol} · {scan.company} · {scan.price:,.2f} · {movement} · Volume {volume} · "
                f"{scan.freshness} · {', '.join(scan.sources)}{rule_text}"
            )
            item.setData(Qt.ItemDataRole.UserRole, scan.symbol)
            self.scanner_results.addItem(item)
        state = "All Live" if market_session_status(datetime.now(timezone.utc)).is_open else "Latest Available"
        sources = sorted({source for row in rows for source in row.sources})
        self.scanner_status_text.setText(
            f"{state} • {len(rows)} eligible rows • {', '.join(sources) if sources else 'local/cache'} • progressive enrichment"
        )
        self.scanner_total_text.setText(str(len(getattr(self, "_scanner_rows", []))))
        if not rows:
            self.scanner_results.addItem(f"{state} — no rows match this filter; background work never blocks Active Symbol quotes.")

    def _build_settings_tab_r4(self) -> QWidget:
        tab = QWidget(); page = QWidget()
        layout = QVBoxLayout(page); layout.setContentsMargins(10, 8, 10, 10); layout.setSpacing(8)
        layout.addWidget(self._surface_heading("Settings", "RangeScout preferences, providers, research sources, alerts, privacy, and local data"))
        grid = QGridLayout(); grid.setSpacing(8)

        general_card, general_layout = self._card("General", "Snapshot refresh and ticker behavior")
        general_form = QFormLayout(); general_form.addRow("Refresh interval", self.refresh_interval_combo)
        general_form.addRow("Ticker ribbon", self.ticker_position_combo); general_layout.addLayout(general_form)
        general_layout.addWidget(QLabel("Polling frequency does not guarantee exchange-tick latency."))
        grid.addWidget(general_card, 0, 0)

        appearance_card, appearance_layout = self._card("Appearance", "System, Light, and Dark preserve identical geometry")
        appearance_form = QFormLayout(); appearance_form.addRow("Theme", self.theme_combo); appearance_layout.addLayout(appearance_form)
        appearance_layout.addWidget(QLabel("Dark is the flagship workstation presentation.")); appearance_layout.addStretch(1)
        grid.addWidget(appearance_card, 0, 1)

        provider_group = self._build_provider_launch_panel(); provider_group.setProperty("dashboardCard", True)
        grid.addWidget(provider_group, 1, 0)

        streaming_card, streaming_layout = self._card("Streaming", "Finnhub BYO-key WebSocket analysis")
        streaming_layout.addWidget(QLabel("Live trades and candles require supported credentials."))
        streaming_layout.addWidget(QLabel("Intervals  1s • 5s • 15s • 30s • 1m • 5m"))
        streaming_layout.addWidget(QLabel("Reconnect and stale-feed states stay explicit.")); streaming_layout.addStretch(1)
        grid.addWidget(streaming_card, 1, 1)

        research_group = self._build_catalyst_source_settings_panel(); research_group.setTitle("Research Data")
        research_group.setProperty("dashboardCard", True); grid.addWidget(research_group, 2, 0)

        alerts_card, alerts_layout = self._card("Alerts", "Runtime notification controls")
        alerts_layout.addWidget(QLabel("In-app alerts are explicit.")); alerts_layout.addWidget(QLabel("Sound and desktop are optional."))
        alerts_layout.addWidget(QLabel("Duplicate cooldown  60 seconds")); alerts_layout.addStretch(1)
        grid.addWidget(alerts_card, 2, 1)

        company_card, company_layout = self._card("Company Database", "Incremental local identity, listing, alias, and logo-provenance maintenance")
        company_form = QFormLayout()
        company_form.addRow("Company metadata", self.company_update_schedule_combo)
        company_form.addRow("Known logo refresh", self.logo_refresh_schedule_combo)
        company_form.addRow("Status", self.company_database_status_text)
        company_layout.addLayout(company_form)
        company_layout.addWidget(self._hbox([self.update_company_database_btn, self.refresh_company_logos_btn]))
        grid.addWidget(company_card, 3, 0, 1, 2)

        privacy_card, privacy_layout = self._card("Privacy & Local Data", "Settings, watchlists, notes, history, and catalyst metadata remain local")
        privacy_layout.addWidget(QLabel("Exported CSV files survive upgrade and uninstall."))
        privacy_layout.addWidget(self.database_health_text)
        privacy_layout.addWidget(self._hbox([self.check_local_database_btn, self.export_preferences_btn, self.import_preferences_btn]))
        privacy_layout.addWidget(self.clear_recent_symbols_btn)
        delete_btn = QPushButton("Delete Local RangeScout Data"); delete_btn.setProperty("danger", True)
        delete_btn.clicked.connect(self._on_delete_local_data); privacy_layout.addWidget(delete_btn)
        if QInputDialog is not None:
            restart_btn = QPushButton("Verify Restart Persistence"); restart_btn.clicked.connect(self._on_mark_restart); privacy_layout.addWidget(restart_btn)
        grid.addWidget(privacy_card, 4, 0)

        about_card, about_layout = self._card("About", "Product, publisher, release identity, and licensing")
        about_grid = QGridLayout()
        about_grid.addWidget(QLabel("RangeScout 1.6.3"), 0, 0); about_grid.addWidget(QLabel("Dietrich AI Labs"), 0, 1)
        about_grid.addWidget(QLabel("Market analysis workstation • no trade execution"), 1, 0)
        about_grid.addWidget(QLabel("AUTHENTICODE SIGNING PENDING CERTIFICATE"), 1, 1)
        about_grid.addWidget(QLabel("Qt/PySide license and corresponding-source details ship with the application."), 2, 0, 1, 2)
        about_grid.addWidget(QLabel("Shortcuts: Ctrl+K search • Ctrl+R refresh • Ctrl+1…9 pages"), 3, 0, 1, 2)
        about_layout.addLayout(about_grid); grid.addWidget(about_card, 4, 1)
        for column in range(2): grid.setColumnStretch(column, 1)
        for row in range(5): grid.setRowStretch(row, 1)
        layout.addLayout(grid, 1)
        wrapper = QVBoxLayout(tab); wrapper.setContentsMargins(0, 0, 0, 0); wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_settings_tab(self) -> QWidget:
        if QWidget is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        tab = QWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(self._surface_heading("Settings", "RangeScout 1.6.3 preferences, privacy, and local-data controls"))

        grid = QGridLayout()
        grid.setSpacing(10)
        general_card, general_layout = self._card("General", "Snapshot refresh and ticker behavior")
        general_form = QFormLayout()
        general_form.addRow("Refresh interval", self.refresh_interval_combo)
        general_form.addRow("Ticker ribbon", self.ticker_position_combo)
        general_layout.addLayout(general_form)
        general_layout.addWidget(QLabel("Polling frequency does not guarantee exchange-tick latency."))
        grid.addWidget(general_card, 0, 0)

        appearance_card, appearance_layout = self._card("Appearance", "Readable in System, Light, and Dark themes")
        appearance_form = QFormLayout()
        appearance_form.addRow("Theme", self.theme_combo)
        appearance_layout.addLayout(appearance_form)
        appearance_layout.addWidget(QLabel("Dark is the flagship workstation theme; all themes retain explicit status text."))
        grid.addWidget(appearance_card, 0, 1)

        provider_group = self._build_provider_settings_panel()
        provider_group.setProperty("dashboardCard", True)
        grid.addWidget(provider_group, 1, 0, 1, 2)

        streaming_card, streaming_layout = self._card("Streaming", "Finnhub BYO-key WebSocket analysis")
        streaming_layout.addWidget(QLabel("Live trades and tick-driven candles are enabled when supported credentials are configured."))
        streaming_layout.addWidget(QLabel("Intervals: 1s / 5s / 15s / 30s / 1m / 5m"))
        streaming_layout.addWidget(QLabel("Reconnect and stale-feed states remain explicit."))
        streaming_layout.addStretch(1)
        grid.addWidget(streaming_card, 2, 0)

        research_group = self._build_catalyst_source_settings_panel()
        research_group.setTitle("Research Data & Official Sources")
        research_group.setProperty("dashboardCard", True)
        grid.addWidget(research_group, 2, 1)

        alerts_card, alerts_layout = self._card("Alerts", "Runtime notification controls")
        alerts_layout.addWidget(QLabel("Visual alerts are explicit. Sound and desktop notifications are configured on the Alerts surface."))
        alerts_layout.addWidget(QLabel("Duplicate cooldown: 60 seconds"))
        alerts_layout.addStretch(1)
        grid.addWidget(alerts_card, 3, 0)

        privacy_card, privacy_layout = self._card("Privacy & Local Data", "Settings, watchlists, notes, history, and catalyst metadata remain local")
        delete_btn = QPushButton("Delete Local RangeScout Data")
        delete_btn.setProperty("danger", True)
        delete_btn.clicked.connect(self._on_delete_local_data)
        privacy_layout.addWidget(QLabel("Exported user CSV files are preserved by installer upgrades and uninstall."))
        privacy_layout.addWidget(delete_btn)
        if QInputDialog is not None:
            restart_btn = QPushButton("Set Restart Marker")
            restart_btn.clicked.connect(self._on_mark_restart)
            privacy_layout.addWidget(restart_btn)
        grid.addWidget(privacy_card, 3, 1)

        about_card, about_layout = self._card("About", "Product and release identity")
        about_layout.addWidget(QLabel("RangeScout 1.6.1"))
        about_layout.addWidget(QLabel("Dietrich AI Labs"))
        about_layout.addWidget(QLabel("Market analysis workstation • no trade execution"))
        about_layout.addWidget(QLabel("Qt/PySide license and corresponding-source details ship with the application."))
        grid.addWidget(about_card, 4, 0, 1, 2)
        for column in range(2):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        wrapper = QVBoxLayout(tab)
        wrapper.setContentsMargins(0, 0, 0, 0)
        wrapper.addWidget(self._scrollable(page))
        return tab

    def _build_provider_settings_panel(self) -> QGroupBox:
        if QGroupBox is None or QFormLayout is None:
            raise NoGuiRuntimeError("PySide6 is not installed.")
        group = QGroupBox("Market Data Providers")
        layout = QVBoxLayout(group)
        layout.addWidget(QLabel("Use your own free-provider credentials. RangeScout never includes a shared API key."))

        selection_form = QFormLayout()
        selection_form.addRow("Configure", self.provider_settings_selector)
        selection_form.addRow("Active Provider", self.active_provider_combo)
        selection_form.addRow("Configuration", self.provider_configuration_text)
        selection_form.addRow("Connection", self.provider_connection_text)
        layout.addLayout(selection_form)

        self.finnhub_credentials_widget = QWidget()
        finnhub_form = QFormLayout(self.finnhub_credentials_widget)
        finnhub_form.setContentsMargins(0, 0, 0, 0)
        finnhub_form.addRow("Finnhub API Key", self._hbox([self.finnhub_api_key_input, self.get_finnhub_api_key_btn]))
        layout.addWidget(self.finnhub_credentials_widget)

        self.save_provider_credentials_btn = QPushButton("Save Credentials Securely")
        self.delete_provider_credentials_btn = QPushButton("Delete Stored Credentials")
        layout.addWidget(self._hbox([self.save_provider_credentials_btn, self.delete_provider_credentials_btn]))
        layout.addWidget(QLabel("Stored credentials are never displayed again and are not written to settings.json."))
        fabric_form = QFormLayout()
        fabric_form.addRow("Provider fabric", self.fabric_provider_selector)
        fabric_form.addRow("Fabric status", self.fabric_provider_status_text)
        fabric_form.addRow("BYO API key", self._hbox([self.fabric_api_key_input, self.get_fabric_api_key_btn]))
        layout.addLayout(fabric_form)
        layout.addWidget(self._hbox([self.save_fabric_credentials_btn, self.delete_fabric_credentials_btn]))
        layout.addWidget(QLabel("Public crypto sources need no key. Unsupported consumer-site candidates remain disabled; no scraping fallback is used."))
        discovery_form = QFormLayout()
        discovery_form.addRow("Official listing discovery", self.discovery_status_text)
        discovery_form.addRow("Manual refresh", self.refresh_discovery_btn)
        layout.addLayout(discovery_form)

        logo_form = QFormLayout()
        logo_form.addRow("Company logos", self.company_logo_status_text)
        logo_form.addRow(
            "Logo.dev publishable key",
            self._hbox([self.logo_dev_publishable_key_input, self.get_logo_dev_publishable_key_btn]),
        )
        layout.addLayout(logo_form)
        layout.addWidget(self._hbox([self.save_company_logo_key_btn, self.delete_company_logo_key_btn]))
        logo_notice = QLabel(
            'Ticker logos are optional. RangeScout keeps image bytes in session memory only; '
            'SQLite stores retry/provenance metadata, not third-party logo images. '
            '<a href="https://logo.dev">Logo.dev</a>'
        )
        logo_notice.setWordWrap(True)
        logo_notice.setOpenExternalLinks(True)
        layout.addWidget(logo_notice)

        self._refresh_fabric_provider_status()
        self._refresh_company_logo_status()
        return group

    def _build_provider_launch_panel(self) -> QGroupBox:
        group = QGroupBox("Data Providers")
        layout = QVBoxLayout(group)
        layout.addWidget(QLabel("Routing mode, provider health, API keys, and official signup links live in one dedicated screen."))
        mode = self.app.settings.provider_mode
        self.provider_mode_summary = QLabel(
            "Smart Search (Recommended)" if mode == "smart" else f"Forced provider: {mode.replace('_', ' ').title()}"
        )
        self.provider_mode_summary.setWordWrap(True)
        layout.addWidget(self.provider_mode_summary)
        layout.addWidget(self.data_providers_btn)
        layout.addWidget(QLabel("Keys are stored only in Windows Credential Manager; no shared key is embedded."))
        layout.addStretch(1)
        return group

    def _open_data_providers(self) -> None:
        if self._data_providers_dialog is None:
            self._data_providers_dialog = DataProvidersDialog(self.app, self._qt_window)
            self._data_providers_dialog.finished.connect(self._provider_dialog_closed)
            self._data_providers_dialog.mode_combo.currentIndexChanged.connect(self._sync_market_provider_mode)
        self._data_providers_dialog.refresh()
        self._data_providers_dialog.show()
        self._data_providers_dialog.raise_()
        self._data_providers_dialog.activateWindow()

    def _provider_dialog_closed(self, _result: int) -> None:
        mode = self.app.settings.provider_mode
        self.provider_mode_summary.setText(
            "Smart Search (Recommended)" if mode == "smart" else f"Forced provider: {mode.replace('_', ' ').title()}"
        )

    def _build_catalyst_source_settings_panel(self) -> QGroupBox:
        group = QGroupBox("Official Catalyst Sources")
        layout = QFormLayout(group)
        layout.addRow("SEC EDGAR", QLabel("No key required; fair-access throttled"))
        layout.addRow("Nasdaq Halts", QLabel("No key required; maximum once per minute"))
        layout.addRow("White House", QLabel("No key required; metadata and source links"))
        layout.addRow("Congress.gov", self.congress_configuration_text)
        guidance = QLabel("Credential changes are managed only in Data Providers & API Keys.")
        guidance.setWordWrap(True)
        layout.addRow("Credential management", guidance)
        self._refresh_congress_configuration()
        return group

    def _refresh_congress_configuration(self) -> None:
        try:
            configured = self.app.credential_store.load("congress") is not None
        except Exception:
            self.congress_configuration_text.setText("Secure credential storage unavailable")
            return
        self.congress_configuration_text.setText("Configured" if configured else "Missing API key")

    def _on_save_congress_credentials(self) -> None:
        try:
            self.app.provider_configuration.save_credentials("congress", {"api_key": self.congress_api_key_input.text()})
            self.congress_configuration_text.setText("Configured securely")
        except Exception:
            self.congress_configuration_text.setText("Key was not saved. Check the field and try again.")
        finally:
            self.congress_api_key_input.clear()

    def _on_delete_congress_credentials(self) -> None:
        try:
            self.app.provider_configuration.delete_credentials("congress")
        except Exception:
            self.congress_configuration_text.setText("Stored key could not be deleted safely.")
        else:
            self.congress_configuration_text.setText("Free BYO API key required")
        self.congress_api_key_input.clear()

    def _hbox(self, widgets: list[QWidget]) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _wire_events(self) -> None:
        self.provider_combo.currentIndexChanged.connect(self._on_provider_mode_changed)
        self.data_providers_btn.clicked.connect(self._open_data_providers)
        self.refresh_discovery_btn.clicked.connect(self._on_refresh_discovery)
        self.update_company_database_btn.clicked.connect(self._on_update_company_database)
        self.refresh_company_logos_btn.clicked.connect(self._on_refresh_company_logos)
        self.company_update_schedule_combo.currentIndexChanged.connect(self._on_company_schedule_changed)
        self.logo_refresh_schedule_combo.currentIndexChanged.connect(self._on_company_schedule_changed)
        self.check_local_database_btn.clicked.connect(self._on_check_local_database)
        self.export_preferences_btn.clicked.connect(self._on_export_preferences)
        self.import_preferences_btn.clicked.connect(self._on_import_preferences)
        self.clear_recent_symbols_btn.clicked.connect(self._on_clear_recent_symbols)
        self.save_company_logo_key_btn.clicked.connect(self._on_save_company_logo_key)
        self.delete_company_logo_key_btn.clicked.connect(self._on_delete_company_logo_key)
        self.get_logo_dev_publishable_key_btn.clicked.connect(
            lambda _checked=False: self._open_provider_signup("logo_dev", self.company_logo_status_text)
        )
        self.get_congress_api_key_btn.clicked.connect(
            lambda _checked=False: self._open_provider_signup("congress", self.congress_configuration_text)
        )
        self.theme_combo.currentTextChanged.connect(self._apply_theme)
        self.provider_details_btn.clicked.connect(self._toggle_provider_details)
        self.refresh_interval_combo.currentIndexChanged.connect(self._on_refresh_interval_changed)
        self.ticker_position_combo.currentIndexChanged.connect(self._on_ticker_position_changed)
        self.live_candle_interval.currentIndexChanged.connect(self._on_live_candle_interval_changed)
        self.research_period_combo.currentIndexChanged.connect(self._on_research_period_changed)
        self.tabs.currentChanged.connect(self._on_surface_changed)

    def _configure_shortcuts(self) -> None:
        if QShortcut is None or QKeySequence is None:
            return
        self._shortcuts = []
        for sequence, callback in (("Ctrl+K", self._focus_symbol_search), ("Ctrl+R", self._on_refresh)):
            shortcut = QShortcut(QKeySequence(sequence), self._qt_window)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        for index in range(9):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self._qt_window)
            shortcut.activated.connect(lambda destination=index: self.tabs.setCurrentIndex(destination))
            self._shortcuts.append(shortcut)

    def _configure_recent_symbol_search(self) -> None:
        if QCompleter is None or QStringListModel is None:
            return
        self._search_display_to_instrument: dict[str, InstrumentMatch | str] = {symbol: symbol for symbol in self.recent_symbols.values}
        self._recent_symbol_model = QStringListModel(list(self.recent_symbols.values))
        self._recent_symbol_completer = QCompleter(self._recent_symbol_model, self.active_symbol_input)
        self._recent_symbol_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.active_symbol_input.setCompleter(self._recent_symbol_completer)
        self.active_symbol_input.textEdited.connect(self._on_symbol_search_edited)
        self._recent_symbol_completer.activated.connect(self._activate_search_display)

    def _sync_market_provider_mode(self, _index: int = 0) -> None:
        mode = self.app.settings.provider_mode
        index = self.provider_combo.findData(mode)
        if index >= 0 and index != self.provider_combo.currentIndex():
            self.provider_combo.blockSignals(True)
            self.provider_combo.setCurrentIndex(index)
            self.provider_combo.blockSignals(False)

    def _on_symbol_search_edited(self, text: str) -> None:
        if not hasattr(self, "_recent_symbol_completer"):
            return
        self._instrument_discovery_generation += 1
        generation = self._instrument_discovery_generation
        query = text.strip()
        results: list[InstrumentMatch] = []
        if not query:
            values = list(self.recent_symbols.values)
            self._search_display_to_instrument = {value: value for value in values}
        elif len(query) < 2:
            values = []
            self._search_display_to_instrument = {}
        else:
            results = self.local_instrument_search.search(query)
            self._search_display_to_instrument = {result.display_text: result for result in results}
            values = list(self._search_display_to_instrument)
        self._recent_symbol_model.setStringList(values)
        self._recent_symbol_completer.setCompletionPrefix("")
        if values:
            self._recent_symbol_completer.complete()
        if len(query) >= 2 and not self._local_search_is_sufficient(results):
            task = _InstrumentDiscoveryTask(self.app, generation, query)
            task.signals.finished.connect(self._on_provider_discovery_finished)
            self._instrument_discovery_tasks[generation] = task
            QThreadPool.globalInstance().start(task)

    @staticmethod
    def _local_search_is_sufficient(results: list[InstrumentMatch]) -> bool:
        if not results:
            return False
        first = results[0]
        return first.match_kind in {"exact_symbol", "exact_alias", "exact_name", "issuer_name"} and first.score >= 900

    def _on_provider_discovery_finished(
        self, generation: int, query: str, results: object, error: Exception | None,
    ) -> None:
        self._instrument_discovery_tasks.pop(generation, None)
        if generation != self._instrument_discovery_generation or self.active_symbol_input.text().strip() != query:
            return
        if error is not None or not isinstance(results, list):
            return
        matches = [item for item in results if isinstance(item, InstrumentMatch)]
        self._search_display_to_instrument = {item.display_text: item for item in matches}
        values = list(self._search_display_to_instrument)
        self._recent_symbol_model.setStringList(values)
        self._recent_symbol_completer.setCompletionPrefix("")
        if values:
            self._recent_symbol_completer.complete()

    def _activate_search_display(self, display: object) -> None:
        selected = self._search_display_to_instrument.get(str(display), str(display))
        self.set_active_symbol(selected, source="instrument-search")


    def _focus_symbol_search(self) -> None:
        self.active_symbol_input.setFocus()
        self.active_symbol_input.selectAll()
        if not self.active_symbol_input.text().strip() and hasattr(self, "_recent_symbol_completer"):
            self._recent_symbol_completer.complete()

    def _remember_recent_symbol(self, symbol: str) -> None:
        values = self.recent_symbols.add(symbol)
        if hasattr(self, "_recent_symbol_model"):
            self._recent_symbol_model.setStringList(list(values))
        self.app.settings = replace(self.app.settings, recent_symbols=values)
        self.app.persist_settings()

    def _on_clear_recent_symbols(self) -> None:
        self.recent_symbols.clear()
        if hasattr(self, "_recent_symbol_model"):
            self._recent_symbol_model.setStringList([])
        self.app.settings = replace(self.app.settings, recent_symbols=())
        self.app.persist_settings()

    def _configure_system_theme_updates(self) -> None:
        if QApplication is None:
            return
        try:
            hints = QApplication.styleHints()
            signal = getattr(hints, "colorSchemeChanged", None)
            if signal is not None:
                signal.connect(self._on_system_color_scheme_changed)
        except Exception:
            return

    def _qt_system_color_scheme(self) -> object | None:
        if QApplication is None:
            return None
        try:
            return QApplication.styleHints().colorScheme()
        except Exception:
            return None

    def _on_system_color_scheme_changed(self, scheme: object) -> None:
        if self.app.settings.theme == Theme.SYSTEM:
            self._apply_theme(Theme.SYSTEM, system_scheme=scheme, persist=False)

    def _open_provider_signup(self, provider_id: str, status_label: QLabel) -> bool:
        """Open only a fixed official HTTPS destination; never use provider/user URLs."""
        url = PROVIDER_SIGNUP_URLS.get(str(provider_id).strip().lower())
        if not url or not url.startswith("https://"):
            status_label.setText("No official API-key signup page is required for this provider.")
            return False
        opened = bool(QDesktopServices is not None and QUrl is not None and QDesktopServices.openUrl(QUrl(url)))
        if not opened:
            status_label.setText(f"Open the official provider signup page: {url}")
        return opened

    def _on_global_symbol_submitted(self) -> None:
        query = self.active_symbol_input.text().strip()
        mapped = getattr(self, "_search_display_to_instrument", {}).get(query)
        if isinstance(mapped, InstrumentMatch):
            self.set_active_symbol(mapped, source="instrument-search")
            return
        resolved = self.local_instrument_search.resolve_unique(str(mapped or query))
        if resolved is not None:
            self.set_active_symbol(resolved, source="instrument-search")
            return
        results = self.local_instrument_search.search(query)
        if results:
            self.result_text.setText(f"Multiple local matches for '{query}'. Choose a symbol from the ranked list.")
            self._on_symbol_search_edited(query)
        else:
            self.result_text.setText("No matching instrument found")

    def set_active_symbol(self, symbol: str | InstrumentMatch, *, source: str, destination: QWidget | None = None) -> ActiveSymbolState:
        if isinstance(symbol, InstrumentMatch):
            item = symbol.instrument
            state = self.active_symbol.set(
                item.symbol, source=source, instrument_id=item.instrument_id, name=item.name,
                venue=item.venue, asset_class=item.asset_class,
                provider_symbols=tuple(sorted(item.provider_symbols.items())), subtype=item.subtype,
                issuer_type=item.issuer_type, security_role=item.security_role, cik=item.cik,
            )
        else:
            if source not in {"instrument-search", "local-search", "global-search", "search", "market-search"}:
                state = self.active_symbol.set(symbol, source=source)
                if destination is not None:
                    self.tabs.setCurrentWidget(destination)
                return state

            resolved = self.local_instrument_search.resolve_unique(str(symbol))
            if resolved is not None:
                return self.set_active_symbol(resolved, source=source, destination=destination)
            state = self.active_symbol.set(symbol, source=source)
        if destination is not None:
            self.tabs.setCurrentWidget(destination)
        return state

    def _on_active_symbol_changed(self, state: ActiveSymbolState) -> None:
        switch_began = perf_counter()
        self._market_range_revision += 1
        had_quote_work = bool(self._quote_tasks) or self._quote_coalesce_timer.isActive()
        self._quote_coalesce_timer.stop()
        self._cancel_stale_quote_tasks()
        self._clear_symbol_bound_surfaces(state.symbol)
        self._remember_recent_symbol(state.symbol)
        self.active_symbol_title.setText(state.symbol) if hasattr(self, "active_symbol_title") else None
        self.active_symbol_context.setText(f"ACTIVE SYMBOL • {state.source}") if hasattr(self, "active_symbol_context") else None
        for name in (
            "active_symbol_input", "market_symbol_input", "chart_symbol_input", "notes_symbol_input",
            "alert_symbol_input", "compare_symbol_input",
        ):
            widget = getattr(self, name, None)
            if widget is not None and widget.text() != state.symbol:
                widget.setText(state.symbol)
        if hasattr(self, "live_symbol_text"):
            self.live_symbol_text.setText(state.symbol)
        if hasattr(self, "market_company_text"):
            identity = state.name or "Instrument profile pending"
            self.market_company_text.setText(f"{state.symbol}  •  {identity}  •  {state.asset_class.replace('_', ' ').title()}")
        self._set_company_logo_placeholder(state.symbol)
        snapshot = self._load_local_symbol_snapshot(state.symbol)
        if self._auto_network_refresh and hasattr(self, "runtime"):
            if had_quote_work and state.instrument_id is None:
                self._pending_quote_selection_at = switch_began
                self._quote_coalesce_timer.start()
            else:
                self._pending_quote_selection_at = None
                self._request_active_quote_refresh(
                    source="quote-instrument-selection", selected_at=switch_began)
        if self._auto_network_refresh:
            self._request_active_news()
        self._request_company_logo(state.symbol, exchange=snapshot.identity.exchange)
        if hasattr(self, "watchlist_detail_symbol"):
            self.watchlist_detail_symbol.setText(state.symbol)
        if hasattr(self, "scanner_detail_symbol"):
            self.scanner_detail_symbol.setText(state.symbol)
        if hasattr(self, "note_editor_title"):
            self.note_editor_title.setText(f"Research note for {state.symbol}")
        if hasattr(self, "notes_hero_symbol"):
            self.notes_hero_symbol.setText(f"{state.symbol}  •  Active Symbol")
        if hasattr(self, "alert_hero_symbol"):
            self.alert_hero_symbol.setText(f"{state.symbol}  •  Active Symbol")
        if hasattr(self, "alert_context_text"):
            self.alert_context_text.setText(f"Active Symbol {state.symbol}\nMarket and provider state follow the global workstation context.")
        if hasattr(self, "runtime"):
            self._catalyst_dispatch_started = perf_counter()
            self.runtime.set_symbols(state.symbol, self._watchlist_symbols())
        if hasattr(self, "research_status_text"):
            self._research_dirty = True
            self._research_loaded_context = None
            self.current_research_snapshot = None
            self.current_analyst_result = None
            self._clear_research_context(state.symbol)
            if self._is_research_visible():
                self._schedule_research_load()
        if hasattr(self, "notes_list"):
            if self._note_editor_dirty:
                self.note_editor_mode.setText("Unsaved edits — save or discard before changing note context")
            else:
                self._on_reload_notes()
        self._update_watchlist_quick_add_state()
        self._refresh_peer_symbols()
        self._performance_timings["identity_switch_ms"] = (perf_counter() - switch_began) * 1000.0
        if self._auto_network_refresh and hasattr(self, "runtime"):
            self._request_active_history_refresh(force=not bool(snapshot.bars))
        elif not snapshot.bars:
            self._set_chart_empty_state("Offline/local mode — no cached price history is available.")

    def _clear_symbol_bound_surfaces(self, symbol: str) -> None:
        """Synchronously remove every old-symbol value before new work can complete."""
        self.current_quote = None
        self.current_bars = []
        self._quote_refresh_in_flight = False
        self._active_quote_task = None
        for chart_name in ("chart", "chart_tab_chart", "live_chart", "research_chart", "scanner_detail_chart"):
            chart = getattr(self, chart_name, None)
            if chart is not None:
                chart.set_series([])
                chart.set_empty_state(f"Loading {symbol} price history…")
        if hasattr(self, "bars_table"):
            self.bars_table.setRowCount(0)
        loading = f"Loading {symbol} quote…"
        for name, text in (
            ("price_text", f"— {loading}"), ("market_change_text", "— Change loading"),
            ("extended_hours_text", "Extended hours loading…"),
            ("live_price_text", "— Loading"), ("live_change_text", "— Loading"),
            ("research_quote_text", f"— {loading}"), ("watchlist_detail_price", f"— {loading}"),
            ("notes_hero_price", f"— {loading}"), ("alert_hero_price", f"— {loading}"),
            ("metrics_text", f"Loading {symbol} market data…"),
            ("market_range_text", "DAY RANGE\nLoading…\n\n52-WEEK RANGE\nLoading…"),
            ("market_volume_text", "VOLUME\nLoading…\n\nAVERAGE VOLUME\nLoading…"),
            ("market_cap_text", "MARKET CAP\nLoading…\n\nSHARES\nLoading…"),
            ("market_performance_text", "Loading cached price history…"),
            ("market_overview_text", f"Loading {symbol} provider context…"),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setText(text)
                if name in {"price_text", "market_change_text", "live_price_text", "live_change_text"}:
                    widget.setStyleSheet("")
                if name == "price_text":
                    widget.setProperty("priceDirection", "loading")
        for name in ("catalyst_list", "research_overview_catalysts", "research_catalyst_list"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.clear()
                widget.addItem(f"Loading {symbol} catalysts…")
        self._official_catalyst_events = []
        self._provider_news_events = []
        self._news_status_message = "Checking configured eligible news sources…"
        if hasattr(self, "offline_banner"):
            self.offline_banner.setVisible(False)

    def _load_local_symbol_snapshot(self, symbol: str) -> LocalSymbolSnapshot:
        began = perf_counter()
        cached = self._symbol_snapshot_cache.get(symbol)
        if cached is not None:
            quote, bars, cached_at = cached
            instrument = quote.instrument
            snapshot = LocalSymbolSnapshot(
                symbol,
                LocalCompanyIdentity(
                    None, symbol, instrument.name or symbol, instrument.asset_type.value, None,
                    instrument.identifier.exchange, None, quote.currency, instrument.country, None,
                    instrument.sector, None, None, (), None, None,
                ),
                quote, self._last_quote_provider_id, cached_at, tuple(bars), cached_at, 0.0, 0,
            )
            self.current_bars = list(bars)
            self._apply_quote_success(quote, refresh_collections=False, from_cache=True, cached_at=cached_at)
            if bars:
                self._apply_bars_to_charts(list(bars))
            elapsed = (perf_counter() - began) * 1000.0
            self._performance_timings.update({
                "local_sqlite_snapshot_ms": snapshot.elapsed_ms,
                "cached_render_ms": elapsed,
                "time_to_first_meaningful_render_ms": elapsed,
                "local_snapshot_query_count": snapshot.query_count,
            })
            return snapshot

        snapshot = self.app.local_snapshots.load(symbol, bar_limit=int(self.market_days_input.value()))
        identity = snapshot.identity
        self.market_company_text.setText(
            f"{identity.symbol}  •  {identity.security_name}  •  {identity.sector or 'Sector N/A'}"
        )
        self.shell_company_text.setText(
            " • ".join(filter(None, (identity.security_name, identity.exchange, identity.currency)))
        )
        if hasattr(self, "research_company_text"):
            self.research_company_text.setText(f"{identity.security_name}  •  {identity.symbol}")
            self.research_profile_text.setText(
                f"{identity.sector or 'Sector N/A'} / {identity.industry or 'Industry N/A'}"
            )
        self.current_bars = list(snapshot.bars)
        if snapshot.quote is not None:
            self._last_quote_provider_id = snapshot.quote_provider_id or "local_cache"
            self._apply_quote_success(
                snapshot.quote, refresh_collections=False, from_cache=True,
                cached_at=snapshot.quote_received_at or snapshot.loaded_at,
            )
        if snapshot.bars:
            self._apply_bars_to_charts(list(snapshot.bars))
            self._apply_history_presentation(list(snapshot.bars), provider_name="Local SQLite cache", from_cache=True)
        elapsed = (perf_counter() - began) * 1000.0
        self._performance_timings.update({
            "local_sqlite_snapshot_ms": snapshot.elapsed_ms,
            "cached_render_ms": elapsed,
            "time_to_first_meaningful_render_ms": elapsed if snapshot.meaningful else None,
            "local_snapshot_query_count": snapshot.query_count,
        })
        return snapshot

    def _restore_cached_symbol(self, symbol: str) -> bool:
        """Compatibility wrapper retained for focused 1.5 cache tests."""
        return self._load_local_symbol_snapshot(symbol).meaningful

    def _set_chart_empty_state(self, message: str) -> None:
        for name in ("chart", "chart_tab_chart", "live_chart", "research_chart", "scanner_detail_chart"):
            chart = getattr(self, name, None)
            if chart is not None and not getattr(chart, "_closes", []):
                chart.set_empty_state(message)
    def _apply_bars_to_charts(self, bars: list[OhlcvBar]) -> None:
        if not bars:
            return
        payload = prepare_chart_payload(bars)
        for name in ("chart", "chart_tab_chart", "research_chart", "scanner_detail_chart"):
            chart = getattr(self, name, None)
            if chart is not None:
                chart.set_series(payload.closes, highs=payload.highs, lows=payload.lows, volumes=payload.volumes, markers=payload.markers)
        if hasattr(self, "live_chart"):
            self.live_chart.set_series(
                payload.closes, opens=payload.opens, highs=payload.highs, lows=payload.lows,
                volumes=payload.volumes, markers=payload.markers,
            )

    def _refresh_peer_symbols(self) -> None:
        if not hasattr(self, "peer_list"):
            return
        from app.research.peers import curated_peers

        self.peer_list.clear()
        if hasattr(self, "research_overview_peers"):
            self.research_overview_peers.clear()
        peers = curated_peers(self.current_symbol)
        if not peers:
            self.peer_list.addItem("No curated comparable symbols available.")
            if hasattr(self, "research_overview_peers"):
                self.research_overview_peers.addItem("No curated comparable symbols available.")
            return
        for symbol in peers:
            item = QListWidgetItem(symbol)
            item.setData(Qt.ItemDataRole.UserRole, symbol)
            self.peer_list.addItem(item)
            if hasattr(self, "research_overview_peers"):
                self.research_overview_peers.addItem(item.clone())

    def _on_peer_activate(self, item: QListWidgetItem) -> None:
        symbol = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(symbol, str) and symbol:
            self.set_active_symbol(symbol, source="peer")

    def _is_research_visible(self) -> bool:
        return hasattr(self, "tabs") and hasattr(self, "research_tab") and self.tabs.currentWidget() is self.research_tab

    def _research_context(self) -> tuple[str, int, str]:
        state = self.active_symbol.state
        return (state.symbol, state.generation, str(self.research_period_combo.currentData() or "annual"))

    def _set_research_provenance_expanded(self, expanded: bool) -> None:
        if not hasattr(self, "research_provenance_card"):
            return
        self.research_provenance_table.setVisible(bool(expanded))
        self.research_provenance_card.setMaximumHeight(360 if expanded else 96)
        self.research_provenance_toggle.setText(
            "Hide traceable source detail" if expanded else "Show traceable source detail"
        )

    def _set_research_overview_labels(self, route: ResearchRoute) -> None:
        if not hasattr(self, "research_profile_card_title"):
            return
        if route is ResearchRoute.CORPORATE:
            profile_title = "COMPANY PROFILE"
            profile_subtitle = "Official SEC identity and corporate classification"
            metrics_title = "KEY METRICS & FUNDAMENTALS"
            metrics_subtitle = "Provider market metadata and applicable official SEC Company Facts"
        elif route is ResearchRoute.FUND:
            profile_title = "FUND PROFILE"
            profile_subtitle = "Official SEC registrant and fund classification"
            metrics_title = "FUND STRUCTURE & MARKET METRICS"
            metrics_subtitle = "Provider market metadata and applicable official SEC fund filings"
        else:
            profile_title = "INSTRUMENT PROFILE"
            profile_subtitle = "Canonical market-instrument identity and provider routing"
            metrics_title = "MARKET METRICS & AVAILABILITY"
            metrics_subtitle = "Provider quote/history context; corporate SEC facts are not applicable"
        self.research_profile_card_title.setText(profile_title)
        self.research_profile_card_subtitle.setText(profile_subtitle)
        self.research_metrics_card_title.setText(metrics_title)
        self.research_metrics_card_subtitle.setText(metrics_subtitle)

    def _clear_research_context(self, symbol: str) -> None:
        self.research_symbol_avatar.setText(symbol[:4])
        self.research_company_text.setText(f"Loading company profile  •  {symbol}")
        self.research_profile_text.setText("Sector / industry • loading")
        self.research_profile_detail_text.setText("Sector / industry • loading")
        self.research_about_text.setText(f"Loading eligible Research for {symbol}. No values are fabricated.")
        plan = plan_research(self.active_symbol.state.asset_class, self.active_symbol.state.subtype,
                             self.active_symbol.state.issuer_type, self.active_symbol.state.security_role)
        self._set_research_overview_labels(plan.route)
        if plan.route is ResearchRoute.CORPORATE:
            self.research_market_metrics_text.setText(
                "Day range loading\n52-week range loading\nMarket cap loading\nAverage volume loading"
            )
            self.research_key_metrics_text.setText("Revenue loading\nNet income loading\nAssets loading\nEquity loading")
        elif plan.route is ResearchRoute.FUND:
            self.research_market_metrics_text.setText(
                "Day range loading\n52-week range loading\nFund market value loading\nAverage volume loading"
            )
            self.research_key_metrics_text.setText("Fund structure loading\nSEC fund identity loading\nFund filing state loading")
        else:
            self.research_market_metrics_text.setText(
                "Day range loading\n52-week range loading\nVolume loading\nCorporate market cap  Not Applicable"
            )
            self.research_key_metrics_text.setText(
                f"Instrument type  {self.active_symbol.state.asset_class.replace('_', ' ').title()}\n"
                "Corporate fundamentals  Not Applicable\nAnalyst outlook  Not Applicable"
            )
        if hasattr(self, "research_analyst_empty_state"):
            self._set_analyst_empty_state("Loading analyst availability", "No provider value is inferred.")
        self.current_research_snapshot = None
        self.current_analyst_result = None
        for table in self.research_tables.values():
            table.setRowCount(0)
        self._sec_status_message = f"SEC: waiting to load {symbol}."
        self._analyst_status_message = "Analyst Outlook: waiting for Research."
        self._update_research_status()

    def _update_research_status(self) -> None:
        self.research_status_text.setText(f"{self._sec_status_message}\n{self._analyst_status_message}")

    def _on_surface_changed(self, index: int) -> None:  # noqa: ARG002
        if hasattr(self, "app"):
            self.app.settings = replace(self.app.settings, last_page=max(0, int(index)))
            self.app.persist_settings()
        if self._is_research_visible() and (
            self._research_dirty or self._research_loaded_context != self._research_context()
        ):
            self._schedule_research_load()

    def _on_research_period_changed(self, index: int) -> None:  # noqa: ARG002
        self.app.settings = replace(
            self.app.settings, research_period=str(self.research_period_combo.currentData() or "annual")
        )
        self.app.persist_settings()
        self._research_dirty = True
        self._research_loaded_context = None
        self._clear_research_context(self.current_symbol)
        if self._is_research_visible():
            self._schedule_research_load()

    def _schedule_research_load(self, *, force: bool = False, immediate: bool = False) -> None:
        self._scheduled_research_force = self._scheduled_research_force or force
        if immediate:
            self._research_debounce_timer.stop()
            self._start_research_load()
        else:
            self._research_debounce_timer.start()

    def _on_research_refresh(self) -> None:
        self._schedule_research_load(force=True, immediate=True)

    def _start_research_load(self) -> None:
        force = self._scheduled_research_force
        self._scheduled_research_force = False
        if not force and not self._is_research_visible():
            return
        context = self._research_context()
        if not force and not self._research_dirty and self._research_loaded_context == context:
            return
        if context in self._research_pending_contexts or context in self._analyst_pending_contexts:
            return
        request = self.active_symbol.request(source="sec-research")
        self._research_dirty = False
        plan = plan_research(request.asset_class, request.subtype, request.issuer_type, request.security_role)
        for index in range(self.research_tabs.count()):
            applicable = self.research_tabs.tabText(index) in plan.visible_sections
            self.research_tabs.setTabEnabled(index, applicable)
            self.research_tabs.setTabVisible(index, applicable)
        current_index = self.research_tabs.currentIndex()
        if current_index < 0 or not self.research_tabs.isTabEnabled(current_index):
            for index in range(self.research_tabs.count()):
                if self.research_tabs.isTabEnabled(index):
                    self.research_tabs.setCurrentIndex(index)
                    break
        if plan.sec_applicable:
            self._sec_status_message = f"SEC: loading eligible Research for {request.symbol}…"
        else:
            self._sec_status_message = f"Research: NOT APPLICABLE — {plan.message}"
        if plan.analyst_applicable:
            self._analyst_status_message = f"Analyst Outlook: checking secure configuration/cache for {request.symbol}…"
        else:
            self._analyst_status_message = f"Analyst Outlook: NOT APPLICABLE — {plan.message}"
            if hasattr(self, "research_analyst_empty_state"):
                self._set_analyst_empty_state("Not Applicable", plan.message)
        self._update_research_status()
        task = _ResearchTask(
            self.research_service,
            request,
            context[2],
        )
        task.signals.finished.connect(self._on_research_finished)
        self._research_tasks[request.request_id] = task
        self._research_request_context[request.request_id] = context
        self._research_pending_contexts.add(context)
        self._research_dispatch_started[request.request_id] = perf_counter()
        QThreadPool.globalInstance().start(task)
        if plan.analyst_applicable:
            analyst_task = _AnalystTask(self.analyst_service, request, force)
            analyst_task.signals.finished.connect(self._on_analyst_finished)
            self._analyst_tasks[request.request_id] = analyst_task
            self._analyst_request_context[request.request_id] = context
            self._analyst_pending_contexts.add(context)
            self._analyst_dispatch_started[request.request_id] = perf_counter()
            QThreadPool.globalInstance().start(analyst_task)

    def _on_research_finished(
        self,
        request: SymbolRequest,
        snapshot: ResearchSnapshot | None,
        error: Exception | None,
    ) -> None:
        self._research_tasks.pop(request.request_id, None)
        began = self._research_dispatch_started.pop(request.request_id, None)
        if began is not None:
            self._performance_timings["sec_latency_ms"] = (perf_counter() - began) * 1000.0
        context = self._research_request_context.pop(request.request_id, None)
        if context is not None:
            self._research_pending_contexts.discard(context)
        if not self.active_symbol.accepts(request) or context != self._research_context():
            return
        if error is not None:
            _LOG.warning("Research provider failure for %s: %s", request.symbol, error)
            self._sec_status_message = f"Research data unavailable for {request.symbol}. No values were fabricated."
            self._update_research_status()
            return
        if snapshot is None or snapshot.generation != request.generation or snapshot.symbol != request.symbol:
            return
        self._apply_research_snapshot(snapshot)

    def _on_analyst_finished(
        self,
        request: SymbolRequest,
        result: AnalystResult | None,
        error: Exception | None,
    ) -> None:
        self._analyst_tasks.pop(request.request_id, None)
        began = self._analyst_dispatch_started.pop(request.request_id, None)
        if began is not None:
            self._performance_timings["analyst_latency_ms"] = (perf_counter() - began) * 1000.0
        context = self._analyst_request_context.pop(request.request_id, None)
        if context is not None:
            self._analyst_pending_contexts.discard(context)
        if not self.active_symbol.accepts(request) or context != self._research_context():
            return
        if error is not None:
            _LOG.warning("Analyst provider failure for %s: %s", request.symbol, error)
            self._analyst_status_message = f"Analyst Outlook unavailable for {request.symbol}."
            self._update_research_status()
            return
        if result is None or result.generation != request.generation or result.symbol != request.symbol:
            return
        self._apply_analyst_result(result)

    def _apply_research_snapshot(self, snapshot: ResearchSnapshot) -> None:
        self.current_research_snapshot = snapshot
        self._research_loaded_context = self._research_context()
        profile = snapshot.profile
        profile_text = profile.name or "Company profile unavailable"
        self._sec_status_message = (
            f"SEC: {snapshot.symbol} • {profile_text} • CIK {profile.cik or 'N/A'} • "
            f"retrieved {snapshot.retrieved_at.astimezone(NEW_YORK):%Y-%m-%d %H:%M:%S ET}"
        )
        self._update_research_status()
        self.research_company_text.setText(f"{profile_text}  •  {snapshot.symbol}")
        profile_line = f"Sector / industry • {profile.sic_description or 'N/A'}" + (f" • SIC {profile.sic}" if profile.sic else "")
        self.research_profile_text.setText(profile_line)
        self.research_profile_detail_text.setText(profile_line)
        plan = plan_research(self.active_symbol.state.asset_class, self.active_symbol.state.subtype,
                             self.active_symbol.state.issuer_type, self.active_symbol.state.security_role)
        self._set_research_overview_labels(plan.route)
        if plan.route is ResearchRoute.CORPORATE:
            self.research_about_text.setText(
                f"{profile_text} ({snapshot.symbol}) is identified by SEC CIK {profile.cik or 'N/A'}"
                f" and exchange {profile.exchange or 'N/A'}. Detailed narrative description is N/A from the approved source."
            )
        else:
            self.research_about_text.setText(
                f"{profile_text} ({snapshot.symbol}) is a {self.active_symbol.state.asset_class.replace('_', ' ')} instrument. "
                f"Only Research applicable to this canonical instrument type is shown."
            )
        self.market_company_text.setText(f"{snapshot.symbol}  •  {profile_text}")

        def render(section: str, metric: str) -> str:
            value = snapshot.sections.get(section, {}).get(metric)
            if value is None or value.value is None:
                return "N/A"
            units = str(value.units or "")
            currency = units.split("/", 1)[0].upper()
            monetary = len(currency) == 3 and currency.isalpha()
            semantic = "money" if monetary else "number"
            return format_financial_value(value.value, semantic, currency if monetary else "USD").text

        if plan.route is ResearchRoute.CORPORATE:
            self.research_key_metrics_text.setText(
                f"Revenue  {render('Overview', 'Revenue')}\n"
                f"Net income  {render('Overview', 'Net income')}\n"
                f"Assets  {render('Overview', 'Assets')}\n"
                f"Equity  {render('Overview', 'Equity')}"
            )
        elif plan.route is ResearchRoute.FUND:
            overview = snapshot.sections.get("Overview", {})
            structure = overview.get("Instrument structure")
            registrant = overview.get("SEC registrant")
            filing = overview.get("Latest fund filing")
            self.research_key_metrics_text.setText(
                f"Structure  {structure.value if structure and structure.value else 'Fund'}\n"
                f"SEC registrant  {registrant.value if registrant and registrant.value else profile_text}\n"
                f"Latest fund filing  {filing.value if filing and filing.value else 'Unavailable'}\n"
                "NAV / distributions  Provider Not Supported"
            )
        else:
            self.research_key_metrics_text.setText(
                f"Instrument type  {self.active_symbol.state.asset_class.replace('_', ' ').title()}\n"
                "Corporate fundamentals  Not Applicable\n"
                "Analyst estimates  Not Applicable"
            )
        for section, table in self.research_tables.items():
            if section == "Analyst Outlook":
                continue
            table.setRowCount(0)
            for metric, research_value in snapshot.sections.get(section, {}).items():
                self._append_research_value(table, metric, research_value)
        if self.current_quote is not None and self.current_quote.instrument.identifier.symbol == snapshot.symbol:
            for metric, value in self._market_research_values(self.current_quote).items():
                self._append_research_value(self.research_tables["Overview"], metric, value)
            valuation = self._derived_valuation_values(snapshot)
            for metric, value in valuation.items():
                self._append_research_value(self.research_tables["Valuation"], metric, value)
        if self.current_bars and self.current_bars[-1].instrument.symbol == snapshot.symbol:
            from app.research.performance import calculate_price_performance

            for metric, value in calculate_price_performance(self.current_bars).items():
                self._append_research_value(self.research_tables["Performance"], metric, value)

    def _set_analyst_empty_state(self, title: str, detail: str) -> None:
        label = self.research_analyst_empty_state
        label.setText(f"{title}\n{detail}")
        label.setVisible(True)
        self.research_tables["Analyst Outlook"].setVisible(False)

    def _apply_analyst_result(self, result: AnalystResult) -> None:
        self.current_analyst_result = result
        table = self.research_tables["Analyst Outlook"]
        table.setRowCount(0)
        state_text = ", ".join(
            f"{provider.replace('_', ' ').title()}: {state.value.replace('_', ' ')}"
            for provider, state in result.provider_states.items()
        )
        message = " ".join(result.messages)
        if result.values:
            self.research_analyst_empty_state.setVisible(False)
            table.setVisible(True)
            for metric, value in result.values.items():
                self._append_research_value(table, metric, value)
        else:
            self._set_analyst_empty_state(
                state_text.title() if state_text else "Analyst Data Unavailable",
                message or "Configure an optional supported provider key, or use a cached entitled dataset.",
            )
        self._analyst_status_message = f"Analyst Outlook: {state_text or 'unavailable'}"
        if message:
            self._analyst_status_message += f" • {message}"
        self._update_research_status()
        self._refresh_analyst_availability()

    def _refresh_analyst_availability(self) -> None:
        if not hasattr(self, "market_analyst_text"):
            return
        states: list[str] = []
        result_states = self.current_analyst_result.provider_states if self.current_analyst_result else {}
        for provider_id, label, capability in (
            ("finnhub", "Finnhub", "recommendation trends"),
            ("alpha_vantage", "Alpha Vantage", "earnings estimates"),
        ):
            try:
                configured = self.app.credential_store.load(provider_id) is not None
            except Exception:
                configured = False
            state = result_states.get(provider_id)
            detail = (
                state.value.replace("_", " ").title()
                if state is not None
                else (f"Configured — {capability} refresh pending" if configured else f"Missing API key — {capability} unavailable")
            )
            states.append(f"{label}\n  Status: {detail}\n  Capability: {capability.title()}")
        self.market_analyst_text.setText("\n\n".join(states))

    @staticmethod
    def _market_research_values(quote: QuoteSnapshot) -> dict[str, ResearchValue]:
        source = quote.instrument.provider or "market provider"
        retrieved = quote.timestamp

        def value(raw: object, units: str, reason: str) -> ResearchValue:
            if raw is None:
                return ResearchValue.unavailable(source, reason)
            return ResearchValue(raw, source, units=units, retrieved_at=retrieved, selection_reason="Current provider quote metadata.")

        change = quote.last - quote.previous_close if quote.previous_close is not None else None
        percent = change / quote.previous_close * Decimal(100) if change is not None and quote.previous_close else None
        return {
            "Current price": value(quote.last, quote.currency, "Current price unavailable."),
            "Dollar change": value(change, quote.currency, "Previous close unavailable."),
            "Percent change": value(percent, "percent", "Previous close unavailable."),
            "Previous close": value(quote.previous_close, quote.currency, "Previous close unavailable."),
            "Day range": value(
                f"{quote.day_low} – {quote.day_high}" if quote.day_low is not None and quote.day_high is not None else None,
                quote.currency,
                "Day range unavailable from the selected provider.",
            ),
            "52-week range": value(
                f"{quote.fifty_two_week_low} – {quote.fifty_two_week_high}"
                if quote.fifty_two_week_low is not None and quote.fifty_two_week_high is not None else None,
                quote.currency,
                "52-week range unavailable from the selected provider.",
            ),
            "Average volume": value(quote.average_volume, "shares", "Average volume unavailable from the selected provider."),
            "Market capitalization": value(quote.market_cap, quote.currency, "Market capitalization unavailable from the selected provider."),
            "Dividend / yield": value(
                f"{quote.dividend_rate} / {quote.dividend_yield}" if quote.dividend_rate is not None or quote.dividend_yield is not None else None,
                "rate / percent",
                "Dividend metadata unavailable from the selected provider.",
            ),
        }

    def _derived_valuation_values(self, snapshot: ResearchSnapshot) -> dict[str, ResearchValue]:
        quote = self.current_quote
        if quote is None:
            return {}
        overview = snapshot.sections.get("Overview", {})
        earnings = snapshot.sections.get("Earnings", {})
        shares = snapshot.sections.get("Valuation", {}).get("Shares outstanding")
        eps = earnings.get("Diluted EPS")
        now = datetime.now(timezone.utc)

        def calculate(numerator: Decimal, denominator: Decimal, label: str, units: str) -> ResearchValue:
            return ResearchValue(
                numerator / denominator,
                "Calculated from market quote and SEC companyfacts",
                period=eps.period if eps else None,
                units=units,
                filing_date=eps.filing_date if eps else None,
                calculated_at=now,
                selection_reason=label,
            )

        result: dict[str, ResearchValue] = {}
        if eps and isinstance(eps.value, Decimal) and eps.value != 0:
            result["Price / earnings"] = calculate(quote.last, eps.value, "Current price divided by selected diluted EPS.", "ratio")
            result["Earnings yield"] = calculate(eps.value * Decimal(100), quote.last, "Selected diluted EPS divided by current price.", "percent")
        market_cap = quote.market_cap
        if market_cap is None and shares and isinstance(shares.value, Decimal):
            market_cap = quote.last * shares.value
        if market_cap is not None:
            result["Calculated market capitalization"] = ResearchValue(
                market_cap,
                "Calculated from market quote and SEC companyfacts",
                period=shares.period if shares else None,
                units=quote.currency,
                filing_date=shares.filing_date if shares else None,
                calculated_at=now,
                selection_reason="Provider market capitalization or current price multiplied by selected shares outstanding.",
            )
            for label, denominator, explanation in (
                ("Price / sales", overview.get("Revenue"), "Market capitalization divided by selected revenue."),
                ("Price / book", overview.get("Equity"), "Market capitalization divided by selected shareholder equity."),
            ):
                if denominator and isinstance(denominator.value, Decimal) and denominator.value != 0:
                    result[label] = ResearchValue(
                        market_cap / denominator.value,
                        "Calculated from market quote and SEC companyfacts",
                        period=denominator.period,
                        units="ratio",
                        filing_date=denominator.filing_date,
                        calculated_at=now,
                        selection_reason=explanation,
                    )
        return result

    def _on_research_export(self) -> None:
        import csv

        snapshot = self.current_research_snapshot
        if snapshot is None or snapshot.symbol != self.current_symbol:
            self.research_status_text.setText("Load Research for the Active Symbol before exporting.")
            return
        output_dir = Path.home() / "Documents" / "RangeScoutExports"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{snapshot.symbol}-research.csv"
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["symbol", "section", "metric", "value", "period", "units", "source", "filing_date", "availability", "selection_reason"])
            for section, values in snapshot.sections.items():
                for metric, value in values.items():
                    writer.writerow([
                        snapshot.symbol, section, metric, "" if value.value is None else value.value,
                        value.period or "", value.units or "", value.source,
                        value.filing_date.isoformat() if value.filing_date else "",
                        value.availability.value, value.selection_reason,
                    ])
        self.research_status_text.setText(f"Exported traceable Research data to {output}")
        if hasattr(self, "export_history_list"):
            if self.export_history_list.count() == 1 and self.export_history_list.item(0).text().startswith("No exports"):
                self.export_history_list.clear()
            self.export_history_list.insertItem(0, f"Research CSV • {snapshot.symbol} • {output.name}")

    @staticmethod
    def _append_research_value(table: QTableWidget, metric: str, value: ResearchValue) -> None:
        row = table.rowCount()
        table.insertRow(row)
        units = str(value.units or "").lower()
        currency = units.split("/", 1)[0].upper()
        monetary = len(currency) == 3 and currency.isalpha()
        metric_lower = metric.lower()
        semantic = "number"
        if "percent" in units or "yield" in metric_lower or metric_lower.endswith("margin"):
            semantic = "percent"
        elif "ratio" in units or any(token in metric_lower for token in ("ratio", "multiple", "p/e", "price to")):
            semantic = "ratio"
        elif monetary or any(token in metric_lower for token in ("revenue", "income", "assets", "equity", "cash", "debt", "price", "eps", "capitalization")):
            semantic = "eps" if "eps" in metric_lower else "money"
        elif any(token in metric_lower for token in ("year", "date", "accession", "cik")):
            semantic = "text"
        rendered = format_financial_value(value.value, semantic, currency if monetary else "USD").text
        filed = value.filing_date.isoformat() if value.filing_date else "not supplied"
        cells = (
            metric,
            rendered,
            f"{value.period or 'N/A'} • {value.units or 'N/A'}",
            f"{value.source} • filed {filed}",
            f"{value.availability.value} • {value.selection_reason}",
        )
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 1 and semantic != "text":
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            table.setItem(row, column, item)

    @staticmethod
    def _workstation_stylesheet(dark: bool) -> str:
        if dark:
            return """
                QMainWindow, QWidget { background: #07121f; color: #d8e4f3; font-family: 'Segoe UI','Segoe UI Symbol',Arial; font-size: 9pt; }
                QLabel { background: transparent; }
                QWidget#workstation_root { background: #07121f; }
                QFrame#navigation_rail { background: #071522; border-right: 1px solid #1a2e42; }
                QLabel#brand_label { color: #f8fbff; font-size: 16pt; font-weight: 800; padding: 2px; }
                QLabel#brand_detail { color: #4f8cff; font-size: 7.5pt; font-weight: 700; letter-spacing: 2px; padding-left: 8px; }
                QLabel#rail_footer { color: #6f8298; padding: 9px; border-top: 1px solid #1b3046; }
                QListWidget#primary_navigation { background: transparent; border: none; outline: none; }
                QListWidget#primary_navigation::item { color: #a9b9cb; padding: 11px 8px; margin: 2px 0; border: none; border-radius: 7px; }
                QListWidget#primary_navigation::item:hover { background: #0d2036; color: #edf5ff; }
                QListWidget#primary_navigation::item:selected { background: #123b78; border-left: 3px solid #3b82f6; color: #ffffff; font-weight: 700; }
                QFrame#active_symbol_header { background: #06101b; border: none; border-bottom: 1px solid #1e3348; border-radius: 0; }
                QLineEdit#global_symbol_search { background: #0b1928; border: 1px solid #1f354b; border-radius: 7px; padding: 7px 12px; font-size: 9.5pt; }
                QLabel#search_mark { color: #68a0ff; font-size: 18pt; }
                QPushButton#header_utility { background: transparent; border: none; font-size: 13pt; padding: 2px; }
                QWidget#ticker_ribbon { background: #091625; border: none; border-bottom: 1px solid #1b3046; border-radius: 0; }
                QWidget#ticker_ribbon QLabel#ticker_title { color: #dfeaf7; font-weight: 700; padding: 0 10px 0 2px; }
                QWidget#ticker_ribbon QPushButton { background: transparent; border: 1px solid transparent; border-radius: 5px; padding: 4px 2px; color: #a9b9ca; font-size: 7.3pt; }
                QWidget#ticker_ribbon QPushButton:checked { background: #102a4d; border-color: #2f7df4; color: #ffffff; }
                QWidget#ticker_ribbon QLabel#ticker_identity { color: #a9b9ca; font-weight: 700; }
                QWidget#ticker_ribbon QLabel#ticker_value { color: #b8c7d9; }
                QWidget#ticker_ribbon QLabel#ticker_value[tickerDirection="up"] { color: #38c47a; }
                QWidget#ticker_ribbon QLabel#ticker_value[tickerDirection="down"] { color: #ff5b5b; }
                QWidget#ticker_ribbon QLabel#ticker_value[tickerDirection="flat"] { color: #b8c7d9; }
                QLabel#logo_attribution { color: #8fa9c4; font-size: 7pt; font-weight: 600; }
                QLabel#offline_banner { color: #fbbf24; font-weight: 700; padding: 2px 8px; }
                QWidget#ticker_ribbon QPushButton:checked { color: #ffffff; }
                QFrame[dashboardCard="true"], QGroupBox[dashboardCard="true"] { background: #0c1a29; border: 1px solid #1b3146; border-radius: 8px; }
                QFrame#research_header { background: #0e1e31; border: 1px solid #24425f; border-radius: 10px; }
                QLabel#card_title { color: #f1f6fc; font-size: 9pt; font-weight: 700; }
                QLabel#card_subtitle, QLabel#surface_subtitle, QLabel#metric_caption { color: #8296ad; font-size: 8.5pt; }
                QLabel#surface_title { color: #f8fbff; font-size: 18pt; font-weight: 750; }
                QLabel#company_identity { color: #f8fbff; font-size: 15pt; font-weight: 750; }
                QLabel#hero_price { color: #f8fbff; font-size: 21pt; font-weight: 750; }
                QLabel#hero_change { color: #5fd39a; font-size: 12pt; font-weight: 700; }
                QLabel#summary_value { color: #64a0ff; font-size: 20pt; font-weight: 750; }
                QLabel#indicator_strip { background: #0a1625; border: 1px solid #20374f; border-radius: 7px; padding: 8px; color: #9ec7ff; }
                QFrame#status_footer { background: #081524; border-top: 1px solid #1b3046; }
                QScrollArea#surface_scroll, QScrollArea#surface_scroll > QWidget > QWidget { background: transparent; border: none; }
                QTabWidget::pane { border: 1px solid #1b3046; background: #091523; border-radius: 8px; }
                QTabBar::tab { background: transparent; color: #8da0b5; border: none; border-bottom: 2px solid transparent; padding: 8px 9px; font-size: 8pt; }
                QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #3b82f6; font-weight: 700; }
                QGroupBox { background: #0d1a2a; border: 1px solid #1d334a; border-radius: 9px; margin-top: 12px; padding: 12px; font-weight: 700; }
                QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #e8f1fb; }
                QPushButton { background: #12243a; color: #dbe8f7; border: 1px solid #29435e; border-radius: 6px; padding: 6px 10px; }
                QPushButton:hover { background: #19304d; border-color: #3b82f6; }
                QPushButton:checked, QPushButton[primary="true"] { background: #1f67e5; border-color: #3b82f6; color: white; }
                QPushButton[danger="true"] { background: #4b1820; border-color: #9f2d3a; color: #ffd9de; }
                QComboBox, QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox { background: #0a1726; color: #e0eaf6; border: 1px solid #294159; border-radius: 6px; padding: 6px; selection-background-color: #2563eb; }
                QListWidget, QTableWidget { background: #0a1726; alternate-background-color: #0c1b2c; color: #dbe7f4; border: 1px solid #1f354d; border-radius: 7px; outline: none; }
                QListWidget::item { padding: 7px; border-bottom: 1px solid #172c41; }
                QListWidget::item:selected, QTableWidget::item:selected { background: #16478d; color: white; }
                QHeaderView::section { background: #0d2033; color: #8fa4bb; border: none; border-bottom: 1px solid #284159; padding: 7px; font-weight: 700; }
                QScrollBar:vertical { background: #081522; width: 10px; margin: 0; }
                QScrollBar::handle:vertical { background: #29445d; min-height: 28px; border-radius: 5px; }
                QToolTip { background: #101f31; color: white; border: 1px solid #3b82f6; }
            """
        return """
            QMainWindow, QWidget { background: #eef3f8; color: #172437; font-family: 'Segoe UI','Segoe UI Symbol',Arial; font-size: 9.5pt; }
            QLabel { background: transparent; }
            QFrame#navigation_rail { background: #e5edf6; border-right: 1px solid #c4d1df; }
            QLabel#brand_label { color: #102036; font-size: 15pt; font-weight: 800; padding: 3px; }
            QLabel#brand_detail { color: #2563eb; font-size: 7.5pt; font-weight: 700; letter-spacing: 2px; padding-left: 8px; }
            QLabel#rail_footer { color: #64748b; padding: 9px; border-top: 1px solid #c4d1df; }
            QListWidget#primary_navigation { background: transparent; border: none; outline: none; }
            QListWidget#primary_navigation::item { color: #40546b; padding: 12px 10px; margin: 3px 0; border: none; border-radius: 7px; }
            QListWidget#primary_navigation::item:selected { background: #d7e6ff; border-left: 3px solid #2563eb; color: #123c7a; font-weight: 700; }
            QFrame#active_symbol_header { background: #f7fafd; border: none; border-bottom: 1px solid #c8d6e5; border-radius: 0; }
            QFrame#research_header { background: white; border: 1px solid #c8d6e5; border-radius: 8px; }
            QLineEdit#global_symbol_search { background: #f8fbff; border: 1px solid #b9c9da; border-radius: 7px; padding: 8px 12px; }
            QLabel#search_mark { color: #2563eb; font-size: 18pt; }
            QWidget#ticker_ribbon { background: white; border: none; border-bottom: 1px solid #c8d6e5; border-radius: 0; }
            QWidget#ticker_ribbon QPushButton { background: transparent; border: 1px solid transparent; padding: 4px 2px; color: #40546b; font-size: 7.3pt; }
            QWidget#ticker_ribbon QPushButton:checked { background: #e4efff; border-color: #4d86e8; color: #143e7d; }
            QWidget#ticker_ribbon QLabel#ticker_identity { color: #40546b; font-weight: 700; }
            QWidget#ticker_ribbon QLabel#ticker_value { color: #475569; }
            QWidget#ticker_ribbon QLabel#ticker_value[tickerDirection="up"] { color: #087a48; }
            QWidget#ticker_ribbon QLabel#ticker_value[tickerDirection="down"] { color: #b91c1c; }
            QWidget#ticker_ribbon QLabel#ticker_value[tickerDirection="flat"] { color: #475569; }
            QLabel#logo_attribution { color: #52677f; font-size: 7pt; font-weight: 600; }
            QLabel#offline_banner { color: #92400e; font-weight: 700; padding: 2px 8px; }
            QFrame[dashboardCard="true"], QGroupBox[dashboardCard="true"], QGroupBox { background: white; border: 1px solid #cbd8e6; border-radius: 10px; }
            QLabel#card_title { color: #172437; font-size: 9pt; font-weight: 700; }
            QLabel#card_subtitle, QLabel#surface_subtitle, QLabel#metric_caption { color: #667b91; font-size: 8.5pt; }
            QLabel#surface_title { color: #102036; font-size: 18pt; font-weight: 750; }
            QLabel#company_identity { color: #102036; font-size: 15pt; font-weight: 750; }
            QLabel#hero_price { color: #102036; font-size: 21pt; font-weight: 750; }
            QLabel#hero_change { color: #087a48; font-size: 12pt; font-weight: 700; }
            QLabel#summary_value { color: #1d5fd0; font-size: 20pt; font-weight: 750; }
            QLabel#indicator_strip { background: #edf5ff; border: 1px solid #c3d8f5; border-radius: 7px; padding: 8px; color: #174e9f; }
            QFrame#status_footer { background: #e4edf6; border-top: 1px solid #c4d1df; }
            QScrollArea#surface_scroll, QScrollArea#surface_scroll > QWidget > QWidget { background: transparent; border: none; }
            QTabWidget::pane { border: 1px solid #c8d6e5; background: #f7fafd; border-radius: 8px; }
            QTabBar::tab { background: transparent; color: #60758b; border: none; border-bottom: 2px solid transparent; padding: 8px 9px; font-size: 8pt; }
            QTabBar::tab:selected { color: #153d78; border-bottom: 2px solid #2563eb; font-weight: 700; }
            QPushButton { background: #f5f8fc; color: #263b52; border: 1px solid #bdccdc; border-radius: 6px; padding: 7px 11px; }
            QPushButton:checked, QPushButton[primary="true"] { background: #2563eb; border-color: #1d4ed8; color: white; }
            QPushButton[danger="true"] { background: #fff0f1; border-color: #dc6670; color: #9f1725; }
            QComboBox, QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox { background: white; color: #172437; border: 1px solid #bdccdc; border-radius: 6px; padding: 6px; }
            QListWidget, QTableWidget { background: white; alternate-background-color: #f5f8fc; color: #24374c; border: 1px solid #c7d5e3; border-radius: 7px; outline: none; }
            QListWidget::item { padding: 7px; border-bottom: 1px solid #e1e8f0; }
            QListWidget::item:selected, QTableWidget::item:selected { background: #dbeafe; color: #133c78; }
            QHeaderView::section { background: #edf3f9; color: #4c6279; border: none; border-bottom: 1px solid #c5d4e2; padding: 7px; font-weight: 700; }
        """

    def _apply_theme(self, theme: str, *, system_scheme: object | None = None, persist: bool = True) -> None:
        if QApplication is None or self.chart is None or self._qt_window is None:
            return
        effective = resolve_effective_theme(
            theme,
            qt_color_scheme=self._qt_system_color_scheme() if system_scheme is None else system_scheme,
        )
        self._effective_theme = effective
        dark = effective == Theme.DARK
        self._qt_window.setStyleSheet(self._workstation_stylesheet(dark))
        chart_theme = Theme.DARK if dark else Theme.LIGHT
        for attribute in ("chart", "chart_tab_chart", "live_chart", "research_chart", "scanner_detail_chart"):
            chart = getattr(self, attribute, None)
            if chart is not None:
                chart.set_theme(chart_theme)
        self.app.settings = replace(self.app.settings, theme=theme)
        if persist:
            self.app.persist_settings()
        self._update_market_status()
        if hasattr(self, "market_symbol_avatar"):
            self._set_company_logo_placeholder(self.current_symbol)
            self._request_company_logo(self.current_symbol)

    def _on_provider_mode_changed(self, _index: int = 0) -> None:
        provider_id = str(self.provider_combo.currentData() or "smart")
        self.app.set_provider_mode(provider_id)
        if hasattr(self, "provider_mode_summary"):
            self.provider_mode_summary.setText(
                "Smart Search (Recommended)" if provider_id == "smart" else f"Forced provider: {self.provider_combo.currentText()}"
            )
        if self._data_providers_dialog is not None:
            index = self._data_providers_dialog.mode_combo.findData(provider_id)
            if index >= 0:
                self._data_providers_dialog.mode_combo.blockSignals(True)
                self._data_providers_dialog.mode_combo.setCurrentIndex(index)
                self._data_providers_dialog.mode_combo.blockSignals(False)
        if provider_id in getattr(self.app.registry, "list_available", lambda: [])():
            self._on_provider_changed(provider_id)
        self._request_active_quote_refresh(source="provider-mode-change")

    def _on_provider_changed(self, provider_id: str) -> None:
        self.provider = self.app.get_provider(provider_id)
        self.app.provider_id = provider_id
        self.app.provider = self.provider
        if self.app.settings.default_provider != provider_id:
            self.app.settings = replace(self.app.settings, default_provider=provider_id)
            self.app.persist_settings()
        self.status_text.setText(
            f"{self.provider.provider_name} | {self.provider.capabilities.delay.value} | {self._provider_source_label()}"
        )
        self.live_provider_text.setText(self.provider.provider_name)
        self._refresh_ticker_ribbon()
        active_index = self.active_provider_combo.findData(provider_id)
        if active_index >= 0 and active_index != self.active_provider_combo.currentIndex():
            self.active_provider_combo.blockSignals(True)
            self.active_provider_combo.setCurrentIndex(active_index)
            self.active_provider_combo.blockSignals(False)
        if hasattr(self, "runtime"):
            self.runtime.set_provider(provider_id)

    def _selected_provider_settings_id(self) -> str:
        return str(self.provider_settings_selector.currentData() or "")

    def _refresh_provider_settings(self, index: int | None = None) -> None:  # noqa: ARG002
        provider_id = self._selected_provider_settings_id()
        if not provider_id:
            return
        status = self.app.provider_configuration.status(provider_id)
        self.provider_configuration_text.setText(status.configuration_text)
        self.provider_connection_text.setText(status.connection_text)
        is_finnhub = provider_id == "finnhub"
        self.finnhub_credentials_widget.setVisible(is_finnhub)
        self.save_provider_credentials_btn.setVisible(is_finnhub)
        self.delete_provider_credentials_btn.setVisible(is_finnhub)

    def _on_settings_active_provider_changed(self, index: int) -> None:
        provider_id = self.active_provider_combo.itemData(index)
        if not provider_id:
            return
        provider_index = self.provider_combo.findData(str(provider_id))
        if provider_index >= 0 and provider_index != self.provider_combo.currentIndex():
            self.provider_combo.setCurrentIndex(provider_index)
        else:
            self._on_provider_changed(str(provider_id))

    def _on_save_provider_credentials(self) -> None:
        provider_id = self._selected_provider_settings_id()
        if provider_id == "finnhub":
            values = {"api_key": self.finnhub_api_key_input.text()}
        else:
            return
        saved = False
        try:
            self.app.provider_configuration.save_credentials(provider_id, values)
        except Exception:
            self.provider_configuration_text.setText("Credentials were not saved. Check every field and try again.")
        else:
            saved = True
        finally:
            self.finnhub_api_key_input.clear()
        if saved:
            self.analyst_service.invalidate_provider(provider_id)
            self._refresh_provider_settings()
            if hasattr(self, "runtime") and provider_id == self.provider.provider_id:
                self.runtime.set_provider(provider_id)

    def _on_delete_provider_credentials(self) -> None:
        provider_id = self._selected_provider_settings_id()
        try:
            self.app.provider_configuration.delete_credentials(provider_id)
        except Exception:
            self.provider_configuration_text.setText("Stored credentials could not be deleted safely.")
            return
        self.finnhub_api_key_input.clear()
        self.analyst_service.invalidate_provider(provider_id)
        self._refresh_provider_settings()

    def _on_credential_state_changed(self, provider_id: str, configured: bool) -> None:
        """Synchronize every credential-dependent view and runtime immediately."""
        # Runtime revocation/activation is safety-critical and must occur even
        # if an optional settings widget refresh later fails.
        if hasattr(self, "runtime"):
            self.runtime.refresh_credential_source(provider_id)
        self.analyst_service.invalidate_provider(provider_id)
        self._refresh_provider_settings()
        self._refresh_fabric_provider_status()
        self._refresh_congress_configuration()
        self._refresh_company_logo_status()
        if self._data_providers_dialog is not None:
            self._data_providers_dialog.refresh()
        if hasattr(self, "runtime"):
            self.runtime.set_provider(self.provider.provider_id)
        if hasattr(self, "market_analyst_text"):
            self._refresh_analyst_availability()

    def _selected_fabric_provider_id(self) -> str:
        return str(self.fabric_provider_selector.currentData() or "")

    def _refresh_fabric_provider_status(self, index: int | None = None) -> None:  # noqa: ARG002
        provider_id = self._selected_fabric_provider_id()
        status = next((item for item in self.app.fabric_provider_statuses() if item["provider_id"] == provider_id), None)
        if status is None:
            self.fabric_provider_status_text.setText("Unavailable")
            return
        if not status["enabled"]:
            text = f"DISABLED — {status['reason']}"
        elif status["requires_credentials"] and not status["configured"]:
            text = f"BYO key required — {status['delay_class']} — {', '.join(status['capabilities'])}"
        else:
            text = f"Available — {status['delay_class']} — {', '.join(status['capabilities'])}"
        self.fabric_provider_status_text.setText(text)
        key_enabled = bool(status["requires_credentials"])
        signup_available = key_enabled and provider_id in PROVIDER_SIGNUP_URLS
        self.fabric_api_key_input.setVisible(key_enabled)
        self.save_fabric_credentials_btn.setVisible(key_enabled)
        self.delete_fabric_credentials_btn.setVisible(key_enabled)
        self.get_fabric_api_key_btn.setVisible(signup_available)
        self.get_fabric_api_key_btn.setEnabled(signup_available)

    def _on_save_fabric_credentials(self) -> None:
        provider_id = self._selected_fabric_provider_id()
        try:
            self.app.provider_configuration.save_credentials(
                provider_id, {"api_key": self.fabric_api_key_input.text()}
            )
        except Exception:
            self.fabric_provider_status_text.setText("Key was not saved. Check the field and secure storage.")
        else:
            self.analyst_service.invalidate_provider(provider_id)
        finally:
            self.fabric_api_key_input.clear()
        self._refresh_fabric_provider_status()

    def _on_delete_fabric_credentials(self) -> None:
        provider_id = self._selected_fabric_provider_id()
        try:
            self.app.provider_configuration.delete_credentials(provider_id)
        except Exception:
            self.fabric_provider_status_text.setText("Stored key could not be deleted safely.")
        else:
            self.analyst_service.invalidate_provider(provider_id)
            self._refresh_fabric_provider_status()
        self.fabric_api_key_input.clear()

    def _refresh_company_logo_status(self) -> None:
        if not hasattr(self, "company_logo_status_text"):
            return
        status = self.app.company_logo_status()
        configured = status.get("credential_sources", {})
        enabled = [source.replace("_", " ") for source, active in configured.items() if active]
        detail = ", ".join(enabled) if enabled else "no BYO-key logo source configured"
        self.company_logo_status_text.setText(
            ("Configured — " if enabled else "Optional — ")
            + "local permitted/licensed logos are checked first. "
            f"Credential sources: {detail}. Ticker monogram is the final fallback."
        )

    def _on_save_company_logo_key(self) -> None:
        value = self.logo_dev_publishable_key_input.text().strip()
        try:
            self.app.provider_configuration.save_credentials("logo_dev", {"publishable_key": value})
        except Exception:
            self.company_logo_status_text.setText(
                "Logo.dev key was not saved. Check the publishable key and secure storage."
            )
        else:
            self.app.company_logo_service.clear_session_cache()
            self._refresh_company_logo_status()
            self._request_company_logo(self.current_symbol)
        finally:
            self.logo_dev_publishable_key_input.clear()

    def _on_delete_company_logo_key(self) -> None:
        try:
            self.app.provider_configuration.delete_credentials("logo_dev")
        except Exception:
            self.company_logo_status_text.setText("Stored Logo.dev key could not be deleted safely.")
        else:
            self.app.company_logo_service.clear_session_cache()
            self._set_company_logo_placeholder(self.current_symbol)
            self._refresh_company_logo_status()
        self.logo_dev_publishable_key_input.clear()

    def _logo_theme(self) -> str:
        return "light" if self._effective_theme == Theme.LIGHT else "dark"

    def _set_company_logo_placeholder(self, symbol: str) -> None:
        monogram = str(symbol).strip().upper()[:4] or "—"
        for name in ("market_symbol_avatar", "research_symbol_avatar"):
            label = getattr(self, name, None)
            if label is None:
                continue
            if QPixmap is not None:
                label.setPixmap(QPixmap())
            label.setText(monogram)
            label.setToolTip("Company logo unavailable or not configured; showing ticker monogram.")
        for name in ("market_logo_attribution", "research_logo_attribution"):
            attribution = getattr(self, name, None)
            if attribution is not None:
                attribution.clear()
                attribution.setVisible(False)

    def _request_active_news(self) -> None:
        if QThreadPool is None or not hasattr(self.market_data, "fetch_news"):
            self._news_status_message = "No configured provider exposes an eligible news capability."
            self._render_catalyst_news()
            return
        request = self.active_symbol.request()
        task = _NewsTask(self.market_data, request)
        task.signals.finished.connect(self._on_news_finished)
        self._news_tasks[request.generation] = task
        QThreadPool.globalInstance().start(task)

    def _on_news_finished(self, request: SymbolRequest, result: Any, error: Any) -> None:
        self._news_tasks.pop(request.generation, None)
        if not self.active_symbol.is_current(request):
            return
        if error is not None:
            message = str(error)
            if "credentials" in message.lower() or "eligible" in message.lower():
                self._news_status_message = "Finnhub news not configured; official sources were still checked."
            else:
                self._news_status_message = "Configured news source was unavailable; official sources were still checked."
            self._provider_news_events = []
        else:
            payload = getattr(result, "payload", None)
            self._provider_news_events = [item for item in (payload or []) if isinstance(item, CatalystEvent)]
            provider = getattr(getattr(result, "metadata", None), "provider_name", "configured provider")
            self._news_status_message = f"Checked {provider} and official sources."
        self._render_catalyst_news()

    def _request_company_logo(self, symbol: str, exchange: str | None = None) -> None:
        if QThreadPool is None or not hasattr(self.app, "company_logo_service"):
            return
        normalized = str(symbol).strip().upper()
        if not normalized:
            return
        theme = self._logo_theme()
        exchange_key = str(exchange or "").strip().upper()
        key = (normalized, exchange_key, theme)
        if key in self._company_logo_inflight:
            return

        cached = self.app.company_logo_service.cached(normalized, exchange, theme=theme)
        if cached is not None and cached.has_image:
            self._apply_company_logo_asset(cached)
            return

        request = self.active_symbol.request(source="company-logo")
        task = _CompanyLogoTask(self.app.company_logo_service, request, exchange, theme)
        self._company_logo_inflight.add(key)
        self._company_logo_tasks[request.request_id] = (task, key)
        self._logo_dispatch_started[request.request_id] = perf_counter()
        task.signals.finished.connect(self._on_company_logo_finished)
        QThreadPool.globalInstance().start(task)

    def _on_company_logo_finished(
        self,
        request: SymbolRequest,
        asset: Any | None,
        error: Exception | None,
    ) -> None:
        task_record = self._company_logo_tasks.pop(request.request_id, None)
        began = self._logo_dispatch_started.pop(request.request_id, None)
        if began is not None:
            self._performance_timings["logo_latency_ms"] = (perf_counter() - began) * 1000.0
        if task_record is not None:
            self._company_logo_inflight.discard(task_record[1])
        if not self.active_symbol.accepts(request):
            return
        if error is not None or asset is None or not getattr(asset, "has_image", False):
            return
        self._apply_company_logo_asset(asset)

    def _apply_company_logo_asset(self, asset: Any) -> None:
        if QPixmap is None or not getattr(asset, "image_bytes", None):
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(asset.image_bytes):
            return
        is_twelve_data = getattr(asset, "provider_id", "") == "twelve_data_logo"
        for name, size, attribution_name in (
            ("market_symbol_avatar", 72, "market_logo_attribution"),
            ("research_symbol_avatar", 48, "research_logo_attribution"),
        ):
            label = getattr(self, name, None)
            if label is None:
                continue
            rendered = pixmap.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setText("")
            label.setPixmap(rendered)
            source = str(getattr(asset, "message", "Company logo")).strip().rstrip(".")
            license_text = str(getattr(asset, "license_metadata", "") or "Source attribution retained")
            persistence = "permitted local copy" if getattr(asset, "persistent_local_copy", False) else "session-only image"
            label.setToolTip(f"{asset.symbol} company logo • {source} • {persistence} • {license_text}")
            attribution = getattr(self, attribution_name, None)
            if attribution is not None:
                attribution.setText("Logo: Twelve Data" if is_twelve_data else "")
                attribution.setVisible(is_twelve_data)

    def _on_refresh_discovery(self) -> None:
        self.discovery_status_text.setText("Refreshing official Nasdaq Trader listing directories in the background…")
        future = self.app.refresh_instrument_discovery()
        future.add_done_callback(
            lambda _future: self._runtime_bridge.invoke.emit(self._refresh_discovery_status)
        )

    def _refresh_discovery_status(self) -> None:
        status = self.app.instrument_discovery_status()
        if status["running"]:
            self.discovery_status_text.setText("RUNNING — official Nasdaq Trader directories; UI remains responsive")
            return
        if status["last_error"]:
            self.discovery_status_text.setText(
                f"OFFLINE/FAILED — last-known-good registry retained — {status['last_error']}"
            )
            return
        if not status["last_success_utc"]:
            self.discovery_status_text.setText("DUE — no successful official directory refresh recorded yet")
            return
        self.discovery_status_text.setText(
            f"Last success {status['last_success_utc']} | next due {status['next_due_utc']} | "
            f"SHA-256 {status['source_sha256']} | diff +{status['added']} / "
            f"-{status['removed_inactive']} / ~{status['changed']}"
        )

    def _on_update_company_database(self) -> None:
        self.company_database_status_text.setText("Updating official company/listing metadata in the background…")
        future = self.app.refresh_company_database()
        future.add_done_callback(
            lambda _future: self._runtime_bridge.invoke.emit(self._refresh_company_database_status)
        )

    def _on_refresh_company_logos(self) -> None:
        self.company_database_status_text.setText("Refreshing eligible known logos in the background…")
        future = self.app.refresh_company_logos()
        future.add_done_callback(
            lambda _future: self._runtime_bridge.invoke.emit(self._refresh_company_database_status)
        )

    def _refresh_company_database_status(self) -> None:
        if not hasattr(self, "company_database_status_text"):
            return
        status = self.app.company_database_status()
        total = int(status.get("total_instruments", 0))
        logo_count = int(status.get("logo_coverage", 0))
        coverage = (logo_count / total * 100.0) if total else 0.0
        schedule = status.get("schedule", {})
        company_schedule = schedule.get("company_metadata", {})
        logo_schedule = schedule.get("logos", {})
        self.company_database_status_text.setText(
            f"{status.get('current_update_status')} • {total:,} instruments • logos {logo_count:,} ({coverage:.1f}%)\n"
            f"Company: last {company_schedule.get('last_success_utc') or 'Never'} • "
            f"next {company_schedule.get('next_due_utc') or 'Off'}\n"
            f"Logos: last {logo_schedule.get('last_success_utc') or 'Never'} • "
            f"next {logo_schedule.get('next_due_utc') or 'Off'}\n"
            f"Added {status.get('companies_added', 0)} • changed {status.get('companies_changed', 0)} • "
            f"inactive {status.get('inactive_or_delisted', 0)} • aliases {status.get('aliases_or_symbol_changes', 0)} • "
            f"logo successes {status.get('logo_successes', 0)} • logo failures {status.get('logo_failures', 0)} • "
            f"source failures {status.get('source_failures', 0)}"
        )

    def _on_company_schedule_changed(self, _index: int) -> None:
        self.app.settings = replace(
            self.app.settings,
            company_update_schedule=str(self.company_update_schedule_combo.currentData() or "weekly"),
            logo_refresh_schedule=str(self.logo_refresh_schedule_combo.currentData() or "monthly"),
        )
        self.app.persist_settings()
        started = self.app.reevaluate_company_maintenance()
        for future in started.values():
            future.add_done_callback(
                lambda _future: self._runtime_bridge.invoke.emit(self._refresh_company_database_status)
            )
        self._refresh_company_database_status()

    def _on_check_local_database(self) -> None:
        result = self.app.check_local_database()
        if result["healthy"]:
            self.database_health_text.setText("Healthy — integrity check ok; no foreign-key violations.")
        else:
            self.database_health_text.setText(
                "Issue detected — preserve a backup and contact support before any explicit repair. "
                f"Integrity: {result['integrity_check']}; foreign-key issues: {len(result['foreign_key_violations'])}."
            )

    def _on_export_preferences(self) -> None:
        if QFileDialog is None:
            return
        path, _selected = QFileDialog.getSaveFileName(
            self._qt_window, "Export RangeScout Preferences", "RangeScout_preferences.json", "JSON (*.json)"
        )
        if not path:
            return
        export_safe_settings(path, self.app.settings)
        self.database_health_text.setText(f"Preferences exported without credentials: {path}")

    def _on_import_preferences(self) -> None:
        if QFileDialog is None:
            return
        path, _selected = QFileDialog.getOpenFileName(
            self._qt_window, "Import RangeScout Preferences", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            imported = import_safe_settings(path, self.app.settings)
        except ValueError as exc:
            self.database_health_text.setText(str(exc))
            return
        self.app.settings = imported
        self.app.persist_settings()
        self.database_health_text.setText("Preferences imported. Credentials were not read or changed.")

    def _persist_ui_state(self) -> None:
        if not hasattr(self, "tabs"):
            return
        position = self._qt_window.pos()
        size = self._qt_window.size()
        selected_watchlist = self.watchlist_id_input.text().strip() if hasattr(self, "watchlist_id_input") else ""
        self.app.settings = replace(
            self.app.settings,
            window_width=max(1120, size.width()), window_height=max(700, size.height()),
            window_x=position.x(), window_y=position.y(), last_page=max(0, self.tabs.currentIndex()),
            research_period=str(self.research_period_combo.currentData() or "annual"),
            selected_watchlist=selected_watchlist,
            recent_symbols=self.recent_symbols.values,
        )
        self.app.persist_settings()

    def _provider_source_label(self) -> str:
        return "live network-backed/provider-dependent"

    def _toggle_provider_details(self) -> None:
        details = self.app.market_data_router.diagnostics()
        timings = self.performance_diagnostics()
        if details:
            self.provider_diagnostics_text.setText(
                " • ".join(
                    (
                        f"winner {details.get('winning_provider') or 'N/A'}",
                        f"latency {details.get('latency_ms') if details.get('latency_ms') is not None else 'N/A'} ms",
                        f"provider time {details.get('provider_timestamp') or 'N/A'}",
                        f"cache {details.get('cache') or 'N/A'}",
                        f"circuit {details.get('circuit_state') or 'N/A'}",
                        f"rate limit {details.get('rate_limit_state') or 'N/A'}",
                        f"class {details.get('delay_class') or 'N/A'}",
                        f"fallback {details.get('fallback_reason') or 'none'}",
                        f"local snapshot {timings.get('local_sqlite_snapshot_ms', 'N/A')} ms",
                        f"cached render {timings.get('cached_render_ms', 'N/A')} ms",
                        f"quote {timings.get('quote_latency_ms', 'pending')} ms",
                        f"history {timings.get('history_refresh_latency_ms', 'pending')} ms",
                    )
                )
            )
        self.provider_diagnostics_text.setVisible(not self.provider_diagnostics_text.isVisible())

    def _fabric_provider_name(self, provider_id: str) -> str:
        try:
            return self.app.fabric_registry.get(provider_id).descriptor.display_name
        except (KeyError, AttributeError):
            return provider_id

    def _on_refresh_interval_changed(self, index: int) -> None:
        interval_ms = self.refresh_interval_combo.itemData(index)
        if interval_ms not in ALLOWED_LIVE_REFRESH_INTERVALS_MS:
            return
        if self.app.settings.live_refresh_interval_ms != interval_ms:
            self.app.settings = replace(self.app.settings, live_refresh_interval_ms=interval_ms)
            self.app.persist_settings()
        if hasattr(self, "live_refresh_timer"):
            self.live_refresh_timer.setInterval(interval_ms)

    def _configure_live_refresh_timer(self) -> None:
        if QTimer is None:
            return
        self.live_refresh_timer = QTimer(self._qt_window)
        self.live_refresh_timer.setInterval(self.app.settings.live_refresh_interval_ms)
        self.live_refresh_timer.timeout.connect(self._on_live_refresh_tick)
        self.live_refresh_timer.start()

    def _on_live_refresh_tick(self) -> None:
        self._update_market_status()
        if hasattr(self, "runtime"):
            self.runtime.tick()
        self._request_active_quote_refresh()

    def _priority_quote_pool(self) -> Any:
        if self._quote_thread_pool is not None:
            return self._quote_thread_pool
        try:
            pool = QThreadPool(self._qt_window)
            self._quote_pool_is_global = False
        except (TypeError, AttributeError):
            # Lightweight test doubles historically expose only globalInstance().
            pool = QThreadPool.globalInstance()
            self._quote_pool_is_global = True
        if not self._quote_pool_is_global:
            pool.setMaxThreadCount(1)
            pool.setExpiryTimeout(30_000)
        self._quote_thread_pool = pool
        return pool

    def _dispatch_coalesced_quote(self) -> None:
        selected_at = self._pending_quote_selection_at
        self._pending_quote_selection_at = None
        self._request_active_quote_refresh(
            source="quote-symbol-selection",
            selected_at=selected_at,
        )

    def _cancel_stale_quote_tasks(self) -> None:
        pool = self._priority_quote_pool() if self._quote_tasks else None
        for generation, task in tuple(self._quote_tasks.items()):
            cancel = getattr(task, "cancel", None)
            if callable(cancel):
                cancel()
            timer = self._quote_timeout_timers.pop(task.request.request_id, None)
            if timer is not None:
                timer.stop()
            removed = False
            take = getattr(pool, "tryTake", None) if pool is not None else None
            if callable(take):
                try:
                    removed = bool(take(task))
                except RuntimeError:
                    # Qt may have already deleted a completed auto-delete runnable
                    # before its queued completion signal is processed.
                    removed = True
            if removed:
                self._quote_tasks.pop(generation, None)
                self._quote_dispatch_started.pop(generation, None)
                self._quote_selection_started.pop(task.request.request_id, None)
        self._quote_refresh_in_flight = bool(self._quote_tasks)

    def _request_active_quote_refresh(
        self,
        *,
        source: str = "quote-auto-refresh",
        selected_at: float | None = None,
    ) -> None:
        if QThreadPool is None:
            return
        request = self.active_symbol.request(source=source)
        if request.generation in self._quote_tasks:
            return
        selection_started = perf_counter() if selected_at is None else selected_at
        dispatch_started = perf_counter()
        self._quote_refresh_in_flight = True
        self._quote_dispatch_started[request.generation] = dispatch_started
        self._quote_selection_started[request.request_id] = selection_started
        self._performance_timings["quote_dispatch_delay_ms"] = (
            dispatch_started - selection_started
        ) * 1000.0
        self._performance_timings["quote_provider_dispatched"] = request.requested_at.isoformat()
        self._performance_timings["quote_selection_timestamp"] = request.requested_at.isoformat()
        task = _QuoteRefreshTask(self.market_data, self.app.local_snapshots, request)
        self._quote_tasks[request.generation] = task
        task.signals.finished.connect(self._on_active_quote_finished)
        self._active_quote_task = task
        timer = QTimer(self._qt_window)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda request=request: self._on_active_quote_deadline(request))
        elapsed_ms = (perf_counter() - selection_started) * 1000.0
        remaining_ms = max(1, int(ACTIVE_QUOTE_TIMEOUT_TIMER_MS - elapsed_ms))
        self._quote_timeout_timers[request.request_id] = timer
        timer.start(remaining_ms)
        pool = self._priority_quote_pool()
        try:
            pool.start(task, 1000)
        except TypeError:
            pool.start(task)

    def _on_active_quote_deadline(self, request: SymbolRequest) -> None:
        timer = self._quote_timeout_timers.pop(request.request_id, None)
        if timer is not None:
            timer.stop()
        active_task = self._quote_tasks.get(request.generation)
        if active_task is None or active_task.request.request_id != request.request_id:
            return
        if not self.active_symbol.accepts(request):
            return
        self._quote_timed_out_requests.add(request.request_id)
        cancel = getattr(active_task, "cancel", None)
        if callable(cancel):
            cancel()
        selected_at = self._quote_selection_started.get(request.request_id)
        elapsed_ms = (perf_counter() - selected_at) * 1000.0 if selected_at is not None else None
        self._performance_timings["quote_deadline_outcome"] = "timeout"
        self._performance_timings["quote_wall_clock_ms"] = elapsed_ms
        if self.current_quote is not None and self.current_quote.instrument.identifier.symbol == request.symbol:
            stamp = self.current_quote.timestamp.astimezone(NEW_YORK)
            self.offline_banner.setText(
                f"Fresh quote timed out — showing cached data from {stamp:%I:%M %p}".replace("from 0", "from ")
            )
            self.offline_banner.setVisible(True)
            self.result_text.setText(
                f"No fresh {request.symbol} quote arrived within 4 seconds; showing cached data. Use Refresh to retry."
            )
            return
        for name, text in (
            ("price_text", "— Quote timed out"),
            ("live_price_text", "— Quote timed out"),
            ("research_quote_text", "— Quote timed out"),
            ("watchlist_detail_price", "— Quote timed out"),
            ("notes_hero_price", "— Quote timed out"),
            ("alert_hero_price", "— Quote timed out"),
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setText(text)
        self.result_text.setText(
            f"No fresh {request.symbol} quote arrived within 4 seconds. Quote timed out; use Refresh to retry."
        )

    def _on_active_quote_finished(
        self,
        request: SymbolRequest,
        provider_id: str,
        quote: QuoteSnapshot | None,
        error: Exception | None,
    ) -> None:
        timer = self._quote_timeout_timers.pop(request.request_id, None)
        if timer is not None:
            timer.stop()
        self._quote_tasks.pop(request.generation, None)
        began = self._quote_dispatch_started.pop(request.generation, None)
        selected_at = self._quote_selection_started.pop(request.request_id, None)
        if began is not None:
            self._performance_timings["quote_latency_ms"] = (perf_counter() - began) * 1000.0
        if selected_at is not None:
            self._performance_timings["quote_wall_clock_ms"] = (perf_counter() - selected_at) * 1000.0
        router = getattr(self.market_data, "router", None)
        if router is not None:
            diagnostic = router.diagnostics()
            attempts = diagnostic.get("attempts") or []
            if attempts:
                attempt = attempts[0]
                for source_name, target_name in (
                    ("executor_queue_wait_ms", "quote_executor_queue_wait_ms"),
                    ("gate_wait_ms", "quote_gate_wait_ms"),
                    ("adapter_network_ms", "quote_adapter_network_ms"),
                ):
                    if source_name in attempt:
                        self._performance_timings[target_name] = attempt[source_name]
        self._quote_refresh_in_flight = bool(self._quote_tasks)
        if not self._quote_tasks:
            self._active_quote_task = None
        self._update_market_status()
        if error is not None and self.active_symbol.accepts(request):
            if self.current_quote is not None and self.current_quote.instrument.identifier.symbol == request.symbol:
                stamp = self.current_quote.timestamp.astimezone(NEW_YORK)
                self.offline_banner.setText(f"Offline — showing cached data from {stamp:%I:%M %p}".replace("from 0", "from "))
                self.offline_banner.setVisible(True)
                self.result_text.setText(
                    f"Provider unavailable; retaining the last successful {request.symbol} quote as cached data. Retry is available."
                )
            else:
                self.result_text.setText(f"{request.symbol} quote unavailable. Use Refresh to retry.")
            return
        if quote is None or not self.active_symbol.accepts(request):
            return
        self._quote_timed_out_requests.discard(request.request_id)
        self._performance_timings["quote_deadline_outcome"] = "fresh"
        self._last_quote_provider_id = provider_id
        self._performance_timings["quote_provider_winner"] = provider_id
        self._apply_quote_success(quote, refresh_collections=False)
        provider_name = self._fabric_provider_name(provider_id)
        self.result_text.setText(
            f"Updated {request.symbol} using {provider_name}; "
            f"data classification: {quote.delay_label.value}."
        )
        self._record_fresh_completion_if_done()

    def _request_active_history_refresh(self, *, force: bool = False) -> None:
        if QThreadPool is None:
            return
        revision = self._market_range_revision
        request = self.active_symbol.request(source=f"history-background-refresh:{revision}")
        if any(task.request.source == request.source for task in self._history_tasks.values()):
            return
        if not force and self.current_bars:
            newest = self.current_bars[-1].date
            if (datetime.now(timezone.utc).date() - newest).days <= 4:
                return
        task = _HistoryRefreshTask(
            self.market_data, self.app.store.path, request, int(self.market_days_input.value())
        )
        self._history_tasks[request.request_id] = task
        self._history_dispatch_started[request.request_id] = perf_counter()
        task.signals.finished.connect(self._on_active_history_finished)
        QThreadPool.globalInstance().start(task)

    def _on_active_history_finished(
        self,
        request: SymbolRequest,
        provider_id: str,
        provider_name: str,
        bars: list[OhlcvBar] | None,
        error: Exception | None,
    ) -> None:
        self._history_tasks.pop(request.request_id, None)
        began = self._history_dispatch_started.pop(request.request_id, None)
        if began is not None:
            self._performance_timings["history_refresh_latency_ms"] = (perf_counter() - began) * 1000.0
        try:
            range_revision = int(request.source.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            range_revision = -1
        if not self.active_symbol.accepts(request) or range_revision != self._market_range_revision:
            return
        if error is not None:
            if self.current_bars:
                self.result_text.setText(
                    f"History refresh unavailable; retaining {len(self.current_bars)} cached {request.symbol} bars."
                )
            else:
                self._set_chart_empty_state("Price history unavailable from the configured provider.")
                self.result_text.setText(f"History unavailable for {request.symbol}; quote refresh remains independent.")
            return
        if not bars:
            self._set_chart_empty_state("No price history was returned by the configured provider.")
            self.result_text.setText(f"No price history was returned for {request.symbol}; quote refresh remains independent.")
            return
        range_days = int(self.market_days_input.value())
        end = datetime.now(NEW_YORK).date()
        start = end - timedelta(days=range_days)
        selected_bars = [bar for bar in bars if start <= bar.date <= end]
        self.current_bars = selected_bars
        self._apply_bars_to_charts(self.current_bars)
        self._apply_history_presentation(self.current_bars, provider_name=provider_name or provider_id)
        if self.current_quote is not None and self.current_quote.instrument.identifier.symbol == request.symbol:
            # Recompute the quote presentation after history arrives so the
            # fused previous-close fallback can use the new bars.
            self._apply_quote_success(self.current_quote, refresh_collections=False)
        if self.current_quote is not None and self.current_quote.instrument.identifier.symbol == request.symbol:
            self._symbol_snapshot_cache[request.symbol] = (
                self.current_quote, tuple(self.current_bars), datetime.now(timezone.utc)
            )
        self._record_fresh_completion_if_done()

    def _apply_history_presentation(
        self, bars: list[OhlcvBar], *, provider_name: str, from_cache: bool = False
    ) -> None:
        if not bars:
            return
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        first = bars[0].open
        latest = bars[-1].close
        change = ((latest - first) / first * Decimal("100")) if first else Decimal("0")
        peak = highs[0]
        max_drawdown = Decimal("0")
        for bar in bars:
            peak = max(peak, bar.high)
            if peak:
                max_drawdown = min(max_drawdown, (bar.low - peak) / peak * Decimal("100"))
        self.metrics_text.setText(
            f"Loaded bars  {len(bars)}\nPeriod high  {max(highs):.2f}\n"
            f"Period low  {min(lows):.2f}\nChange  {change:.2f}%\nMax drawdown  {max_drawdown:.2f}%"
        )
        self.market_performance_text.setText(
            f"Window  {len(bars)} bars\nChange  {change:.2f}%\nPeriod high  {max(highs):.2f}\n"
            f"Period low  {min(lows):.2f}\nMax drawdown  {max_drawdown:.2f}%"
        )
        self.market_overview_text.setText(
            f"Active Symbol  {self.current_symbol}\nProvider  {provider_name}\n"
            f"Data state  {'cached/local' if from_cache else 'fresh background result'}\n"
            "Coverage  selected symbol and loaded history only"
        )
        self.bars_table.setRowCount(0)
        for bar in bars[-15:]:
            row = self.bars_table.rowCount()
            self.bars_table.insertRow(row)
            values = (
                bar.date.isoformat(), f"{bar.open:.2f}", f"{bar.high:.2f}", f"{bar.low:.2f}",
                f"{bar.close:.2f}", str(bar.volume), bar.provider,
            )
            for column, value in enumerate(values):
                self.bars_table.setItem(row, column, QTableWidgetItem(value))

    def performance_diagnostics(self) -> dict[str, Any]:
        return dict(self._performance_timings)

    def _record_fresh_completion_if_done(self) -> None:
        if self._fresh_cycle_began is not None and not self._quote_tasks and not self._history_tasks:
            self._performance_timings["total_fresh_completion_ms"] = (
                perf_counter() - self._fresh_cycle_began
            ) * 1000.0
            self._fresh_cycle_began = None

    def _apply_quote_success(
        self,
        quote: QuoteSnapshot,
        *,
        refresh_collections: bool = True,
        from_cache: bool = False,
        cached_at: datetime | None = None,
    ) -> None:
        self.current_quote = quote
        fused_close = previous_regular_close(quote, self.current_bars)
        previous_close = fused_close.value
        presentation = directional_price(quote.last, previous_close, quote.currency)
        self.price_text.setText(presentation.text)
        hero_color = presentation.color if presentation.color != "neutral" else (
            "#cbd5e1" if self._effective_theme == Theme.DARK else "#475569"
        )
        self.price_text.setProperty("priceDirection", presentation.direction)
        self.price_text.setStyleSheet(f"color: {hero_color}; font-size: 21pt; font-weight: 750;")
        updated_at = datetime.now(timezone.utc).astimezone(NEW_YORK)
        self.last_updated_text.setText(f"Last Updated: {updated_at:%H:%M:%S} ET")
        market_timestamp = quote.provider_timestamp
        market_timestamp_text = market_timestamp.isoformat() if market_timestamp else "not supplied"
        self.status_text.setText(
            f"{self._fabric_provider_name(self._last_quote_provider_id)} | {quote.delay_label.value} | "
            f"market timestamp {market_timestamp_text} | {self._provider_source_label()}"
        )
        self.live_symbol_text.setText(quote.instrument.identifier.symbol)
        self.live_price_text.setText(f"{presentation.arrow} {quote.last} {quote.currency}")
        symbol = quote.instrument.identifier.symbol
        company = quote.instrument.name or "Company name N/A"
        sector = quote.instrument.sector or "Sector N/A"
        self.market_company_text.setText(f"{symbol}  •  {company}  •  {sector}")
        self._request_company_logo(symbol, exchange=quote.instrument.identifier.exchange)
        if previous_close not in (None, Decimal("0")):
            dollar_change = quote.last - previous_close
            percent_change = (dollar_change / previous_close) * Decimal("100")
            arrow = "▲" if dollar_change > 0 else "▼" if dollar_change < 0 else "—"
            self.live_change_text.setText(f"{arrow} {dollar_change:+.2f} / {percent_change:+.2f}%")
            self.market_change_text.setText(
                f"Regular session • previous close {previous_close:,.2f} {quote.currency} • {fused_close.source}"
            )
            color = presentation.color if presentation.color != "neutral" else ("#cbd5e1" if self._effective_theme == Theme.DARK else "#475569")
            neutral = "#94a3b8" if self._effective_theme == Theme.DARK else "#64748b"
            self.market_change_text.setStyleSheet(f"color: {neutral}; font-size: 10pt; font-weight: 600;")
            self.live_change_text.setStyleSheet(f"color: {color}; font-weight: 700;")
        else:
            self.live_change_text.setText("-- / --")
            self.market_change_text.setText("Regular session • previous close unavailable")
            neutral = "#cbd5e1" if self._effective_theme == Theme.DARK else "#475569"
            self.market_change_text.setStyleSheet(f"color: {neutral}; font-size: 12pt; font-weight: 700;")
            self.live_change_text.setStyleSheet(f"color: {neutral}; font-weight: 700;")
        extended: list[str] = []
        if quote.pre_market_price is not None:
            delta = f" {quote.pre_market_change:+.2f}" if quote.pre_market_change is not None else ""
            percent = f" ({quote.pre_market_change_percent:+.2f}%)" if quote.pre_market_change_percent is not None else ""
            extended.append(f"PRE-MARKET {quote.pre_market_price} {quote.currency}{delta}{percent}")
        if quote.after_hours_price is not None:
            delta = f" {quote.after_hours_change:+.2f}" if quote.after_hours_change is not None else ""
            percent = f" ({quote.after_hours_change_percent:+.2f}%)" if quote.after_hours_change_percent is not None else ""
            extended.append(f"AFTER HOURS {quote.after_hours_price} {quote.currency}{delta}{percent}")
        self.extended_hours_text.setText(" • ".join(extended) if extended else "Extended hours N/A")
        self.live_trade_time_text.setText(
            quote.provider_timestamp.isoformat() if quote.provider_timestamp else "not supplied"
        )
        self.live_provider_text.setText(self.provider.provider_name)
        self.live_last_update_text.setText(f"{updated_at:%H:%M:%S} ET")
        change_text = "— N/A"
        if previous_close not in (None, Decimal("0")):
            delta = quote.last - previous_close
            arrow = "▲" if delta > 0 else "▼" if delta < 0 else "—"
            change_text = f"{arrow} {delta:+.2f} • {delta / previous_close * Decimal(100):+.2f}%"
        self.research_quote_text.setText(f"{quote.last} {quote.currency} • {change_text}")
        if hasattr(self, "notes_hero_price"):
            self.notes_hero_price.setText(f"{quote.last} {quote.currency}  •  {change_text}")
        if hasattr(self, "alert_hero_price"):
            self.alert_hero_price.setText(f"{quote.last} {quote.currency}  •  {change_text}")
        self.live_bid_text.setText("Unavailable")
        self.live_ask_text.setText("Unavailable")
        self.live_spread_text.setText("Unavailable")
        day_range = f"{quote.day_low:,.2f} – {quote.day_high:,.2f}" if quote.day_low is not None and quote.day_high is not None else "N/A"
        year_range = (
            f"{quote.fifty_two_week_low:,.2f} – {quote.fifty_two_week_high:,.2f}"
            if quote.fifty_two_week_low is not None and quote.fifty_two_week_high is not None else "N/A"
        )
        volume = f"{quote.volume:,}" if quote.volume is not None else "N/A"
        average_volume = f"{quote.average_volume:,}" if quote.average_volume is not None else "N/A"
        market_cap = f"{quote.market_cap:,.0f} {quote.currency}" if quote.market_cap is not None else "N/A"
        self.market_range_text.setText(f"DAY RANGE\n{day_range}\n\n52-WEEK RANGE\n{year_range}")
        self.market_volume_text.setText(f"VOLUME\n{volume}\n\nAVERAGE VOLUME\n{average_volume}")
        self.market_cap_text.setText(f"MARKET CAP\n{market_cap}\n\nSHARES\nN/A")
        self.metrics_text.setText(
            f"Previous close  {format_financial_value(previous_close, 'money', quote.currency).text} ({fused_close.source})\n"
            f"Day range  {day_range}\n52-week range  {year_range}\n"
            f"Volume  {volume}\nAverage volume  {average_volume}\nMarket cap  {market_cap}"
        )
        research_plan = plan_research(self.active_symbol.state.asset_class, self.active_symbol.state.subtype,
                             self.active_symbol.state.issuer_type, self.active_symbol.state.security_role)
        if research_plan.route is ResearchRoute.CORPORATE:
            research_market_text = (
                f"Day range  {day_range}\n52-week range  {year_range}\n"
                f"Market cap  {market_cap}\nAverage volume  {average_volume}"
            )
        elif research_plan.route is ResearchRoute.FUND:
            research_market_text = (
                f"Day range  {day_range}\n52-week range  {year_range}\n"
                f"Fund market value  {market_cap}\nAverage volume  {average_volume}"
            )
        else:
            research_market_text = (
                f"Day range  {day_range}\n52-week range  {year_range}\n"
                f"Volume  {volume}\nCorporate market cap  Not Applicable"
            )
        self.research_market_metrics_text.setText(research_market_text)
        self.watchlist_detail_symbol.setText(symbol) if hasattr(self, "watchlist_detail_symbol") else None
        if hasattr(self, "watchlist_detail_price"):
            self.watchlist_detail_price.setText(presentation.text)
            self.watchlist_detail_metrics.setText(
                f"Day range  {day_range}\n52-week range  {year_range}\nVolume  {volume}\nMarket cap  {market_cap}"
            )
        if hasattr(self, "scanner_detail_text"):
            quote_provider_name = self._fabric_provider_name(self._last_quote_provider_id)
            self.scanner_detail_text.setText(
                f"Price  {presentation.text}\n"
                f"Provider  {quote_provider_name}\nSetup  N/A until a scanner hit is selected"
            )
            self._refresh_scanner_latest_row(quote)
        freshness = "Cached" if from_cache else freshness_label(
            freshness=quote.freshness, delay=quote.delay_label, received_at=quote.timestamp
        )
        stamp = (cached_at or quote.timestamp).astimezone(NEW_YORK)
        self.shell_freshness_text.setText(
            f"{freshness} • {self._fabric_provider_name(self._last_quote_provider_id)} • updated {stamp:%H:%M:%S} ET"
        )
        if hasattr(self, "offline_banner"):
            self.offline_banner.setVisible(False)
        if hasattr(self, "alert_context_text"):
            self.alert_context_text.setText(
                f"Active Symbol {symbol}\n{self.market_status_text.text()}\n"
                f"Provider {self._fabric_provider_name(self._last_quote_provider_id)}\nUpdated {updated_at:%H:%M:%S} ET"
            )
        if hasattr(self, "runtime"):
            self.runtime.update_snapshot(
                quote.instrument.identifier.symbol,
                quote.last,
                quote.previous_close,
                quote.provider_timestamp or quote.timestamp,
            )
        if refresh_collections:
            self._refresh_watchlists_widget()
            self._refresh_ticker_ribbon()
        self._symbol_snapshot_cache[symbol] = (quote, tuple(self.current_bars), datetime.now(timezone.utc))
        if len(self._symbol_snapshot_cache) > 24:
            oldest = next(iter(self._symbol_snapshot_cache))
            self._symbol_snapshot_cache.pop(oldest, None)

    def _update_market_status(self) -> None:
        status = market_session_status()
        state = "OPEN" if status.is_open else "CLOSED"
        detail = status.label.split(" - ", 1)[1] if " - " in status.label else ""
        text = f"MARKET {state}" + (f" — {detail}" if detail else "")
        self.market_status_text.setText(text)
        self.live_market_status_text.setText(text)
        self.research_market_status_text.setText(text)
        if hasattr(self, "shell_market_state_text"):
            self.shell_market_state_text.setText(text)
        if hasattr(self, "scanner_market_text"):
            self.scanner_market_text.setText(state)
        if hasattr(self, "notes_hero_market"):
            self.notes_hero_market.setText(text)
            self._style_market_status(self.notes_hero_market, state)
        if hasattr(self, "alert_hero_market"):
            self.alert_hero_market.setText(text)
            self._style_market_status(self.alert_hero_market, state)
        self._style_market_status(self.market_status_text, state)
        self._style_market_status(self.live_market_status_text, state)
        self._style_market_status(self.research_market_status_text, state)
        if hasattr(self, "shell_market_state_text"):
            self._style_market_status(self.shell_market_state_text, state)

    def _style_market_status(self, label: QLabel, state: str) -> None:
        dark = self._effective_theme == Theme.DARK
        colors = {
            "OPEN": "#22c55e" if dark else "#15803d",
            "CLOSED": "#f87171" if dark else "#b91c1c",
            "PRE-MARKET": "#fbbf24" if dark else "#b45309",
            "AFTER HOURS": "#fbbf24" if dark else "#b45309",
            "HALTED": "#ffffff",
        }
        color = colors.get(state, colors["CLOSED"])
        background = "background-color: #991b1b; padding: 4px 8px;" if state == "HALTED" else ""
        label.setStyleSheet(f"font-weight: 700; color: {color}; {background}")

    def _on_mark_restart(self) -> None:
        if QMessageBox is not None:
            QMessageBox.information(
                self._qt_window, "Settings",
                "Theme changes apply immediately. Restart verification checks only that the saved preference persists.",
            )

    def _on_delete_local_data(self) -> None:
        if QMessageBox is None:
            return
        if not self.app:
            return

        confirmation = QMessageBox.question(
            self._qt_window,
            "Delete Local RangeScout Data",
            "This removes settings, notes, watchlists, and local history from this device.\n"
            "Exported CSV files are not removed.\n"
            "Do you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        report: LocalDataDeletionReport = delete_local_data(
            self.app.data_dir,
            store=self.app.store,
        )
        if report.complete:
            QMessageBox.information(
                self._qt_window,
                "Delete Local RangeScout Data",
                "Local RangeScout data removed.",
            )
            self.exit_application()
            return

        lines = ["Could not remove all requested local data:"]
        if report.failed_paths:
            lines.append("Failures:")
            for path, message in sorted(report.failed_paths.items()):
                lines.append(f"- {path}: {message}")
        if report.refused_unsafe_paths:
            lines.append("Refused:")
            for path, message in sorted(report.refused_unsafe_paths.items()):
                lines.append(f"- {path}: {message}")
        QMessageBox.warning(
            self._qt_window,
            "Delete Local RangeScout Data",
            "\n".join(lines),
        )

    def _on_refresh(self) -> None:
        self._update_market_status()
        symbol = self.market_symbol_input.text().strip().upper()
        if not symbol:
            self.result_text.setText("Please enter a symbol, for example AAPL, then click Refresh.")
            return
        previous = self.current_symbol
        self.set_active_symbol(symbol, source="market-search")
        if previous == symbol:
            self._load_local_symbol_snapshot(symbol)
        self.result_text.setText(f"Refreshing {symbol}… cached/local data remains available.")
        self._fresh_cycle_began = perf_counter()
        self._request_active_quote_refresh()
        self._request_active_history_refresh(force=True)

    def _on_export_csv(self) -> None:
        if not self.current_bars:
            self.export_result.setText("No bars loaded yet. Refresh a symbol first.")
            self.result_text.setText("No bars loaded yet. Refresh a symbol first.")
            return
        result = export_bars_csv(self.current_symbol, self.current_bars, Path.home() / "Documents" / "RangeScoutExports")
        self.export_result.setText(f"Exported {result.row_count} rows to {result.path}")
        self.result_text.setText(f"Exported {result.row_count} rows to {result.path}")
        if hasattr(self, "export_history_list"):
            if self.export_history_list.count() == 1 and self.export_history_list.item(0).text().startswith("No exports"):
                self.export_history_list.clear()
            self.export_history_list.insertItem(0, f"Current Symbol CSV • {self.current_symbol} • {Path(result.path).name}")

    def _refresh_watchlists_widget(self) -> None:
        if not hasattr(self, "watchlist_widget"):
            return
        self.watchlist_widget.blockSignals(True)
        self.watchlist_widget.clear()
        records = list(self.watchlist_store.list())
        self._ticker_watchlist_symbols = [symbol for record in records for symbol in record.symbols]
        if not records:
            self._ticker_watchlist_title = "My Watchlist"
            self.watchlist_widget.addItem("No watchlists yet. Create one to get started.")
            self.watchlist_widget.blockSignals(False)
            if self.app.settings.selected_watchlist:
                self.app.settings = replace(self.app.settings, selected_watchlist="")
                self.app.persist_settings()
            self.watchlist_id_input.clear()
            self.watchlist_title_input.clear()
            self.watchlist_symbol_input.clear()
            if hasattr(self, "watchlist_symbol_table"):
                self.watchlist_symbol_table.setRowCount(0)
            if hasattr(self, "market_related_list"):
                self.market_related_list.clear()
                self.market_related_list.addItem("Add symbols to a watchlist for related context.")
            self._refresh_ticker_ribbon()
            if hasattr(self, "runtime"):
                self.runtime.set_symbols(self.current_symbol, [])
            if hasattr(self, "_render_scanner_rows"):
                self._render_scanner_rows()
            self._update_watchlist_quick_add_state()
            return
        for record in records:
            self.watchlist_widget.addItem(f"{record.id} | {record.title} | {', '.join(record.symbols)}")
        selected = self._selected_watchlist_record(persist_fallback=True)
        if selected is None:
            self.watchlist_widget.blockSignals(False)
            return
        selected_row = next(index for index, record in enumerate(records) if record.id == selected.id)
        self.watchlist_widget.setCurrentRow(selected_row)
        self.watchlist_widget.blockSignals(False)
        self.watchlist_id_input.setText(selected.id)
        self.watchlist_title_input.setText(selected.title)
        self._ticker_watchlist_title = selected.title
        if hasattr(self, "watchlist_symbol_table"):
            self.watchlist_symbol_table.setRowCount(0)
            for symbol in selected.symbols:
                row = self.watchlist_symbol_table.rowCount()
                self.watchlist_symbol_table.insertRow(row)
                active_quote = self.current_quote if self.current_quote and self.current_quote.instrument.identifier.symbol == symbol else None
                price = str(active_quote.last) if active_quote else "N/A"
                change = "N/A"
                volume = f"{active_quote.volume:,}" if active_quote and active_quote.volume is not None else "N/A"
                if active_quote and active_quote.previous_close not in (None, Decimal("0")):
                    delta = active_quote.last - active_quote.previous_close
                    change = f"{delta:+.2f} ({delta / active_quote.previous_close * Decimal(100):+.2f}%)"
                values = (symbol, price, change, volume, "Active" if symbol == self.current_symbol else "Watched", "N/A")
                for column, value in enumerate(values):
                    self.watchlist_symbol_table.setItem(row, column, QTableWidgetItem(value))
                if symbol == self.current_symbol:
                    self.watchlist_symbol_table.selectRow(row)
        if hasattr(self, "market_related_list"):
            self.market_related_list.clear()
            for symbol in selected.symbols[:8]:
                self.market_related_list.addItem(f"{symbol}  •  watched  •  click ticker for live context")
        self._refresh_ticker_ribbon()
        if hasattr(self, "runtime"):
            self.runtime.set_symbols(self.current_symbol, self._watchlist_symbols())
        if hasattr(self, "_render_scanner_rows"):
            self._render_scanner_rows()
        self._update_watchlist_quick_add_state()

    def _selected_watchlist_record(self, *, persist_fallback: bool = False):
        records = list(self.watchlist_store.list())
        if not records:
            return None
        selected_id = self.app.settings.selected_watchlist
        record = next((item for item in records if item.id == selected_id), None)
        if record is None:
            record = records[0]
            if persist_fallback:
                self.app.settings = replace(self.app.settings, selected_watchlist=record.id)
                self.app.persist_settings()
        return record

    def _persist_selected_watchlist(self, watchlist_id: str) -> None:
        if self.app.settings.selected_watchlist == watchlist_id:
            return
        self.app.settings = replace(self.app.settings, selected_watchlist=watchlist_id)
        self.app.persist_settings()

    def _watchlist_warning(self, message: str) -> None:
        if hasattr(self, "result_text"):
            self.result_text.setText(message)
        if QMessageBox is not None:
            QMessageBox.warning(self._qt_window, "Watchlist", message)

    def _normalized_watchlist_symbol(self) -> str | None:
        raw = self.watchlist_symbol_input.text()
        try:
            return normalize_symbol(raw)
        except ValueError as exc:
            self._watchlist_warning(f"Enter one valid symbol. {exc}")
            return None

    def _update_watchlist_quick_add_state(self) -> None:
        if not hasattr(self, "market_watchlist_button"):
            return
        record = self._selected_watchlist_record()
        watched = bool(record is not None and self.current_symbol in record.symbols)
        self.market_watchlist_button.setText("✓ Watchlisted" if watched else "Add to Watchlist")
        self.market_watchlist_button.setProperty("watchlisted", watched)
        if record is None:
            self.market_watchlist_button.setToolTip(
                f"Create My Watchlist and add {self.current_symbol}"
            )
        elif watched:
            self.market_watchlist_button.setToolTip(
                f"{self.current_symbol} is already in {record.title}"
            )
        else:
            self.market_watchlist_button.setToolTip(
                f"Add {self.current_symbol} to {record.title}"
            )

    def _refresh_ticker_ribbon(self) -> None:
        if hasattr(self, "runtime"):
            self.runtime_ticker_state(self.runtime.live.states, self.runtime.live.subscription_plan)
            return
        symbols = [symbol for record in self.watchlist_store.list() for symbol in record.symbols]
        self._render_ticker_ribbon({}, plan_ticker_subscriptions(symbols, None))

    def _ticker_values(self, symbol: str, states: dict[str, LiveSymbolState]) -> tuple[str, str, str | None]:
        state = states.get(symbol)
        price = state.price if state and state.price is not None else None
        previous = state.previous_close if state else None
        if price is None and self.current_quote is not None and self.current_quote.instrument.identifier.symbol == symbol:
            price = self.current_quote.last
            previous = self.current_quote.previous_close
        if price is None:
            return "N/A", "N/A", None
        price_text = f"{price:.2f}"
        if previous in (None, Decimal(0)):
            return price_text, "— N/A", "flat"
        delta = price - previous
        percent = delta / previous * Decimal(100)
        direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        arrow = "▲" if direction == "up" else "▼" if direction == "down" else "—"
        return price_text, f"{arrow} {delta:+.2f} {percent:+.2f}%", direction

    def _render_ticker_ribbon(self, states: dict[str, LiveSymbolState], plan: TickerSubscriptionPlan) -> None:
        if not hasattr(self, "ticker_ribbon_layout"):
            return
        while self.ticker_ribbon_layout.count():
            item = self.ticker_ribbon_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        ribbon_title = QPushButton(f"{self._ticker_watchlist_title} ⌄")
        ribbon_title.setObjectName("ticker_selector")
        ribbon_title.setFixedWidth(120)
        ribbon_title.setToolTip("Open and manage the current watchlist")
        ribbon_title.clicked.connect(lambda _checked=False: self.tabs.setCurrentIndex(3))
        self.ticker_ribbon_layout.addWidget(ribbon_title)

        subscribed = list(plan.subscribed)
        watchlist_order = list(self._ticker_watchlist_symbols)
        symbols = [symbol for symbol in watchlist_order if symbol in subscribed]
        symbols.extend(symbol for symbol in subscribed if symbol not in symbols)
        if self.current_symbol not in symbols:
            symbols.insert(0, self.current_symbol)
        symbols = symbols or [self.current_symbol]
        self._ticker_buttons: dict[str, QPushButton] = {}
        self._ticker_identity_labels: dict[str, QLabel] = {}
        self._ticker_value_labels: dict[str, QLabel] = {}
        for symbol in symbols:
            price, movement, direction = self._ticker_values(symbol, states)
            button = QPushButton("")
            button.setObjectName("ticker_symbol")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setMinimumWidth(72)
            button.setMaximumWidth(190)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setChecked(symbol == self.current_symbol)
            chip = QHBoxLayout(button)
            chip.setContentsMargins(5, 0, 5, 0)
            chip.setSpacing(4)
            identity = QLabel(symbol)
            identity.setObjectName("ticker_identity")
            identity.setProperty("identityNeutral", True)
            value = QLabel(f"{price}  {movement}")
            value.setObjectName("ticker_value")
            value.setProperty("tickerDirection", direction or "unavailable")
            identity.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            value.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            chip.addWidget(identity)
            chip.addWidget(value, 1)
            button.setToolTip(f"Open {symbol} as the global Active Symbol")
            button.clicked.connect(lambda _checked=False, selected=symbol: self._open_live_symbol(selected))
            self.ticker_ribbon_layout.addWidget(button, 1)
            self._ticker_buttons[symbol] = button
            self._ticker_identity_labels[symbol] = identity
            self._ticker_value_labels[symbol] = value
        manage = QPushButton("+ Manage")
        manage.setObjectName("ticker_manage")
        manage.setFixedWidth(64)
        manage.setToolTip("Add or manage watchlist symbols")
        manage.clicked.connect(lambda _checked=False: self.tabs.setCurrentIndex(3))
        self.ticker_ribbon_layout.addWidget(manage)

    def _open_live_symbol(self, symbol: str) -> None:
        self.set_active_symbol(symbol, source="ticker-or-watchlist", destination=self.live_trader_tab)

    def _on_ticker_position_changed(self, _index: int) -> None:
        position = str(self.ticker_position_combo.currentData())
        self.app.settings = replace(self.app.settings, ticker_position=position)
        self.app.persist_settings()
        self.root_layout.removeWidget(self.ticker_ribbon)
        if position == "top":
            self.root_layout.insertWidget(0, self.ticker_ribbon)
        elif position == "bottom":
            self.root_layout.addWidget(self.ticker_ribbon)
        self.ticker_ribbon.setVisible(position != "hidden")

    def _on_market_range_selected(self, days: int) -> None:
        """Apply a mutually exclusive range immediately from local history, then enrich if needed."""
        days = max(30, min(1095, int(days)))
        self._market_range_revision += 1
        for value, button in self.market_range_buttons.items():
            button.blockSignals(True)
            button.setChecked(value == days)
            button.blockSignals(False)
        self.market_days_input.setValue(days)
        end = datetime.now(NEW_YORK).date()
        start = end - timedelta(days=days)
        cached = self.app.store.get_bars_any_provider_in_range(self.current_symbol, start=start, end=end)
        if cached:
            self.current_bars = cached
            self._apply_bars_to_charts(cached)
            self._apply_history_presentation(cached, provider_name=cached[-1].provider or "local cache", from_cache=True)
            self.result_text.setText(f"Showing {len(cached)} cached {self.current_symbol} bars for the selected range.")
        tolerance = timedelta(days=7)
        coverage_complete = bool(
            cached
            and cached[0].date <= start + tolerance
            and cached[-1].date >= end - tolerance
        )
        if not coverage_complete and self._auto_network_refresh:
            self._request_active_history_refresh(force=True)

    def _on_watchlist_create_or_update(self) -> None:
        watchlist_id = self.watchlist_id_input.text().strip()
        title = self.watchlist_title_input.text().strip() or watchlist_id
        if not watchlist_id:
            self._watchlist_warning("Enter a watchlist ID before creating or saving a watchlist.")
            return
        try:
            record = self.watchlist_store.create(watchlist_id, title)
        except Exception as exc:
            existing = next((item for item in self.watchlist_store.list() if item.id == watchlist_id), None)
            if existing is None:
                self._watchlist_warning(str(exc))
                return
            existing.title = title  # type: ignore[attr-defined]
            self.watchlist_store._save()
            record = existing
        self._persist_selected_watchlist(record.id)
        self._refresh_watchlists_widget()

    def _on_watchlist_delete(self) -> None:
        watchlist_id = self.watchlist_id_input.text().strip()
        if not watchlist_id:
            self._watchlist_warning("Select a watchlist before deleting it.")
            return
        try:
            self.watchlist_store.delete(watchlist_id)
        except Exception as exc:
            self._watchlist_warning(str(exc))
            return
        if self.app.settings.selected_watchlist == watchlist_id:
            self.app.settings = replace(self.app.settings, selected_watchlist="")
            self.app.persist_settings()
        self._refresh_watchlists_widget()

    def _on_watchlist_add_symbol(self) -> None:
        record = self._selected_watchlist_record()
        if record is None:
            self._watchlist_warning("Create or select a watchlist before adding a symbol.")
            return
        symbol = self._normalized_watchlist_symbol()
        if symbol is None:
            return
        try:
            self.watchlist_store.add_symbol(record.id, symbol)
        except Exception as exc:
            self._watchlist_warning(str(exc))
            return
        self._persist_selected_watchlist(record.id)
        self.watchlist_symbol_input.clear()
        self._refresh_watchlists_widget()

    def _on_add_active_symbol_to_watchlist(self) -> None:
        record = self._selected_watchlist_record(persist_fallback=True)
        if record is None:
            try:
                record = self.watchlist_store.create("my-watchlist", "My Watchlist")
            except Exception as exc:
                self._watchlist_warning(str(exc))
                return
        try:
            symbol = normalize_symbol(self.current_symbol)
        except ValueError as exc:
            self._watchlist_warning(str(exc))
            return
        already = self.current_symbol in record.symbols
        try:
            self.watchlist_store.add_symbol(record.id, symbol)
        except Exception as exc:
            self._watchlist_warning(str(exc))
            return
        self._persist_selected_watchlist(record.id)
        self._refresh_watchlists_widget()
        if hasattr(self, "market_watchlist_button"):
            self.market_watchlist_button.setToolTip(
                f"{symbol} is already in {record.title}" if already else f"Added {symbol} to {record.title}"
            )

    def _on_watchlist_remove_symbol(self) -> None:
        record = self._selected_watchlist_record()
        if record is None:
            self._watchlist_warning("Create or select a watchlist before removing a symbol.")
            return
        symbol = self._normalized_watchlist_symbol()
        if symbol is None:
            return
        try:
            self.watchlist_store.remove_symbol(record.id, symbol)
        except Exception as exc:
            self._watchlist_warning(str(exc))
            return
        self._persist_selected_watchlist(record.id)
        self.watchlist_symbol_input.clear()
        self._refresh_watchlists_widget()

    def _on_watchlist_select(self) -> None:
        if not hasattr(self, "watchlist_widget") or self.watchlist_widget.currentRow() < 0:
            return
        text = self.watchlist_widget.currentItem().text() if self.watchlist_widget.currentItem() else ""
        if not text:
            return
        watchlist_id = text.split("|", 1)[0].strip()
        record = next((item for item in self.watchlist_store.list() if item.id == watchlist_id), None)
        if record is None:
            self._watchlist_warning("Select a valid watchlist.")
            return
        self._persist_selected_watchlist(record.id)
        self.watchlist_id_input.setText(record.id)
        self.watchlist_title_input.setText(record.title)
        self.watchlist_symbol_input.clear()
        self._refresh_watchlists_widget()

    def _on_watchlist_activate(self, item: QListWidgetItem) -> None:
        watchlist_id = item.text().split("|", 1)[0].strip()
        record = next((entry for entry in self.watchlist_store.list() if entry.id == watchlist_id), None)
        if record is not None and record.symbols:
            self.set_active_symbol(record.symbols[0], source="watchlist")

    def _note_title(self, category: str, symbol: str) -> str:
        singular = {
            "Trade Journal": "Trade Journal Entry", "Research Notes": "Research Note",
            "Earnings Notes": "Earnings Note", "Catalyst Notes": "Catalyst Note", "General": "General Note",
        }.get(category, category.rstrip("s"))
        return f"{singular} for {symbol}"

    def _on_note_text_changed(self) -> None:
        if not self._loading_note_editor:
            self._note_editor_dirty = True
            if hasattr(self, "note_editor_mode"):
                mode = "Edit Existing Note" if self._selected_note_id else "New Note"
                self.note_editor_mode.setText(f"{mode} • Unsaved changes")

    def _confirm_note_transition(self) -> bool:
        if not self._note_editor_dirty:
            return True
        if QMessageBox is None:
            return False
        choice = QMessageBox.question(
            self._qt_window, "Unsaved Note Changes",
            "Save your note before changing selection? Choose Yes to save, No to discard, or Cancel to stay.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Yes:
            return self._on_save_note()
        self._note_editor_dirty = False
        return True

    def _on_new_note(self, _checked: bool = False) -> None:
        if not self._confirm_note_transition():
            return
        self._selected_note_id = None
        self._loading_note_editor = True
        self.notes_text.clear()
        self._loading_note_editor = False
        self._note_editor_dirty = False
        self.note_editor_mode.setText("New Note")
        self.note_editor_title.setText(self._note_title(self._active_note_category, self.notes_symbol_input.text().strip().upper()))
        self.notes_list.clearSelection()

    def _on_save_note(self, _checked: bool = False) -> bool:
        symbol = self.notes_symbol_input.text().strip().upper()
        body = self.notes_text.toPlainText().strip()
        if not symbol or not body:
            self.note_editor_mode.setText("Enter a linked symbol and note text before saving")
            return False
        if self._selected_note_id:
            note = self.note_store.update(
                self._selected_note_id, symbol=symbol, text=body, category=self._active_note_category
            )
        else:
            note = self.note_store.add(symbol, body, self._active_note_category)
            self._selected_note_id = note.id
        self._note_editor_dirty = False
        self._on_reload_notes(preserve_selection=True, bypass_unsaved=True)
        self.note_editor_mode.setText("Edit Existing Note • Saved locally")
        return True

    def _on_add_note(self) -> None:
        self._on_save_note()

    def _on_delete_note(self, _checked: bool = False) -> None:
        if not self._selected_note_id:
            return
        if self._note_editor_dirty and not self._confirm_note_transition():
            return
        self.note_store.delete(self._selected_note_id)
        self._selected_note_id = None
        self._note_editor_dirty = False
        self._on_reload_notes(bypass_unsaved=True)
        self._on_new_note()

    def _on_note_category_changed(self, category: str) -> None:
        if not category or category == self._active_note_category:
            return
        if not self._confirm_note_transition():
            return
        self._active_note_category = category
        self._selected_note_id = None
        self._on_reload_notes(bypass_unsaved=True)
        self._on_new_note()

    def _on_note_selected(self, item: QListWidgetItem) -> None:
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if not note_id or note_id == self._selected_note_id:
            return
        if not self._confirm_note_transition():
            return
        note = self.note_store.get(str(note_id))
        if note is None:
            self._on_reload_notes(bypass_unsaved=True)
            return
        self._selected_note_id = note.id
        self._active_note_category = note.category
        category_items = self.note_categories.findItems(note.category, Qt.MatchFlag.MatchExactly)
        if category_items:
            self.note_categories.blockSignals(True)
            self.note_categories.setCurrentItem(category_items[0])
            self.note_categories.blockSignals(False)
        self._loading_note_editor = True
        self.notes_symbol_input.setText(note.symbol)
        self.notes_text.setPlainText(note.text)
        self._loading_note_editor = False
        self._note_editor_dirty = False
        self.note_editor_title.setText(self._note_title(note.category, note.symbol))
        self.note_editor_mode.setText("Edit Existing Note")

    @staticmethod
    def _human_note_time(value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            local = stamp.astimezone(NEW_YORK)
            return f"{local:%b} {local.day}, {local:%Y} · {local:%I:%M %p}".replace(" 0", " ")
        except (TypeError, ValueError):
            return "Date unavailable"

    def _on_reload_notes(self, _checked: bool = False, *, preserve_selection: bool = False, bypass_unsaved: bool = False) -> None:
        if not hasattr(self, "notes_symbol_input") or not hasattr(self, "notes_list"):
            return
        if not bypass_unsaved and self._note_editor_dirty and not self._confirm_note_transition():
            return
        self.note_store.reload()
        selected = self._selected_note_id if preserve_selection else None
        self.notes_list.clear()
        symbol = self.notes_symbol_input.text().strip().upper()
        if not symbol:
            self.notes_list.addItem("Enter a symbol to view or add notes.")
            return
        for note in self.note_store.list_for(symbol, self._active_note_category):
            preview = " ".join(note.text.split())[:90]
            item = QListWidgetItem(
                f"{self._human_note_time(note.modified_at or note.created_at)}\n"
                f"{note.category.rstrip('s')} · {note.symbol}\n{preview}"
            )
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            self.notes_list.addItem(item)
            if note.id == selected:
                self.notes_list.setCurrentItem(item)
        if self.notes_list.count() == 0:
            self.notes_list.addItem(f"No {self._active_note_category.lower()} yet for {symbol}. Choose New Note to create one.")

    def _on_alert_add(self) -> None:
        symbol = self.alert_symbol_input.text().strip().upper()
        mode = self.alert_mode_input.currentText()
        threshold = Decimal(str(self.alert_threshold_input.value()))
        if not symbol:
            self.alert_list.addItem("Enter a symbol before creating an alert.")
            return
        self.alert_rules.append(AlertRule(id=f"{mode}:{symbol}", symbol=symbol, mode=mode, threshold=threshold))
        self.alert_list.addItem(f"Rule added: {symbol} {mode} >= {threshold}")

    def _on_alert_evaluate(self) -> None:
        bars_by_symbol = defaultdict(list[OhlcvBar])
        if self.current_symbol:
            bars_by_symbol[self.current_symbol] = self.current_bars
        events: list[AlertEvent] = evaluate_alerts(self.alert_rules, bars_by_symbol)
        self.alert_list.clear()
        if not events:
            self.alert_list.addItem("No alerts triggered.")
            return
        for event in events:
            self.alert_list.addItem(f"{event.severity.upper()} {event.instrument.symbol}: {event.message}")

    def _on_refresh_chart(self) -> None:
        self.chart_error_text.setText("")
        try:
            symbol = self.chart_symbol_input.text().strip().upper() or self.market_symbol_input.text().strip().upper()
            if not symbol:
                self.chart_error_text.setText("Please enter a symbol first in either Market or Chart controls.")
                return
            self.set_active_symbol(symbol, source="chart-refresh")
            snapshot = self._load_local_symbol_snapshot(symbol)
            if snapshot.bars:
                self.chart_error_text.setText(f"Showing {len(snapshot.bars)} cached/local bars; refreshing in background…")
            else:
                self.chart_error_text.setText(f"Loading {symbol} history in background…")
            self._request_active_history_refresh(force=True)
        except Exception as exc:
            self.chart_error_text.setText(f"Chart refresh failed: {exc}")

    def _on_compare(self) -> None:
        symbol = self.compare_symbol_input.text().strip().upper()
        benchmark = self.compare_benchmark_input.text().strip().upper()
        if not symbol or not benchmark:
            self.comparison_result.setText("Please enter both symbol and benchmark first.")
            return
        if QThreadPool is None:
            self.comparison_result.setText("Comparison worker is unavailable.")
            return
        self.comparison_result.setText(f"Comparing {symbol} with {benchmark} in background…")
        task = _ComparisonTask(self.market_data, symbol, benchmark)
        self._comparison_tasks.add(task)
        task.signals.finished.connect(lambda result, error, active=task: self._on_compare_finished(active, result, error))
        QThreadPool.globalInstance().start(task)

    def _on_compare_finished(self, task: Any, result: Any | None, error: Exception | None) -> None:
        self._comparison_tasks.discard(task)
        if error is not None or result is None:
            self.comparison_result.setText(f"Comparison unavailable: {error or 'No bars to compare.'}")
            return
        self.comparison_result.setText(
            f"{result.symbol}: {result.symbol_change_pct:.2f}% | {result.benchmark}: {result.benchmark_change_pct:.2f}% | "
            f"outperformance {result.relative_outperformance_pct:.2f}%"
        )

    def show(self) -> None:
        self._qt_window.show()

    def exit_application(self) -> None:
        """Exit explicitly; unlike the title-bar X, this terminates RangeScout."""

        if self._tray_controller is not None:
            self._tray_controller.request_exit()
            return
        self._exit_application()

    def _production_transport(self, provider: str, credentials: Any) -> QtWebSocketTransport:
        if provider == "finnhub":
            return QtWebSocketTransport(lambda: finnhub_url(credentials), provider)
        raise ValueError("Selected provider does not support a WebSocket stream.")

    def _watchlist_symbols(self) -> list[str]:
        return [symbol for record in self.watchlist_store.list() for symbol in record.symbols]

    def _on_live_candle_interval_changed(self, index: int) -> None:
        if hasattr(self, "runtime"):
            self.runtime.set_interval(int(self.live_candle_interval.itemData(index)))

    def runtime_stream_status(self, status: StreamStatus | None, display_text: str) -> None:
        self.live_stream_status_text.setText(display_text)
        if status is not None:
            self.provider_connection_text.setText(status.message)

    def runtime_live_state(self, state: LiveSymbolState) -> None:
        if state.symbol != self.current_symbol:
            return
        self.live_symbol_text.setText(state.symbol)
        self.live_price_text.setText(str(state.price) if state.price is not None else "N/A")
        self.live_trade_time_text.setText(state.last_trade_at.isoformat() if state.last_trade_at else "Unavailable")
        self.live_last_update_text.setText(datetime.now(timezone.utc).astimezone(NEW_YORK).strftime("%H:%M:%S ET"))
        if state.price is not None and state.previous_close not in (None, Decimal(0)):
            delta = state.price - state.previous_close
            self.live_change_text.setText(f"{delta:+.2f} / {delta / state.previous_close * Decimal(100):+.2f}%")
        else:
            self.live_change_text.setText("N/A / N/A")
        self.live_bid_text.setText("Unavailable")
        self.live_ask_text.setText("Unavailable")
        self.live_spread_text.setText("Unavailable")
        if state.halt_status and state.halt_status != "RESUMED":
            self.live_market_status_text.setText(f"HALTED — {state.halt_status}")
            self._style_market_status(self.live_market_status_text, "HALTED")
        else:
            self._update_market_status()
        candles = list(state.completed_candles)
        if state.current_candle is not None:
            candles.append(state.current_candle)
        closes = [float(item.close) for item in candles]
        if closes or not self.current_bars:
            self.live_chart.set_series(
                closes,
                opens=[float(item.open) for item in candles],
                highs=[float(item.high) for item in candles],
                lows=[float(item.low) for item in candles],
                volumes=[float(item.volume) for item in candles],
            )
        indicator = state.indicators
        if indicator is None:
            self.live_indicators_text.setText("Indicators: N/A — awaiting previous close and sufficient live candle history")
        else:
            self.live_indicators_text.setText(
                f"VWAP {indicator.vwap:.4f} | EMA9 {indicator.ema9:.4f} | EMA20 {indicator.ema20:.4f} | "
                f"RSI {indicator.rsi:.2f} | MACD {indicator.macd:.4f} | ATR {indicator.atr:.4f} | "
                f"RVOL {indicator.rvol:.2f}"
            )

    def runtime_ticker_state(self, states: dict[str, LiveSymbolState], plan: TickerSubscriptionPlan) -> None:
        self._render_ticker_ribbon(states, plan)
        # Live ticks must never perform storage work. The watchlist cache is
        # refreshed only by explicit watchlist UI/storage operations.
        allowed = {self.current_symbol, *self._ticker_watchlist_symbols, *plan.subscribed}
        additions: list[ScannerRow] = []
        existing = {row.symbol: row for row in getattr(self, "_scanner_rows", [])}
        for symbol, state in states.items():
            normalized = symbol.strip().upper()
            if normalized not in allowed or state.price is None:
                continue
            previous = state.previous_close or state.previous_price
            change = state.price - previous if previous not in (None, Decimal("0")) else None
            percent = change / previous * Decimal("100") if change is not None and previous else None
            matches = self.local_instrument_search.search(normalized, 1)
            company = matches[0].name if matches and matches[0].symbol == normalized else (
                existing[normalized].company if normalized in existing else "Company name unavailable"
            )
            candle = state.current_candle
            additions.append(ScannerRow(
                symbol=normalized,
                company=company,
                price=state.price,
                change=change,
                change_percent=percent,
                volume=int(candle.volume) if candle is not None else None,
                day_high=candle.high if candle is not None else None,
                day_low=candle.low if candle is not None else None,
                vwap=state.indicators.vwap if state.indicators is not None else None,
                halt_state=state.halt_status,
                freshness=humanize_status_text(state.feed_state),
                sources=(self.provider.provider_name,),
                updated_at=state.last_trade_at,
            ))
        if additions:
            self._scanner_rows = aggregate_scanner_rows([*getattr(self, "_scanner_rows", []), *additions])
            self._render_scanner_rows()

    def runtime_scanner_hits(self, hits: list[Any]) -> None:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for hit in hits:
            grouped[str(hit.symbol).strip().upper()].append(hit)
        self._scanner_rule_hits = dict(grouped)
        if hasattr(self, "live_scanner_context"):
            self.live_scanner_context.clear()
        self._render_scanner_rows()
        if not hits and hasattr(self, "live_scanner_context"):
            self.live_scanner_context.addItem("No current rule hits; eligible Scanner feed rows remain visible.")
        for hit in hits:
            if hasattr(self, "live_scanner_context"):
                item = QListWidgetItem(
                    f"{hit.symbol} | {humanize_event_code(hit.rule)} | {humanize_status_text(hit.detail)}"
                )
                item.setData(Qt.ItemDataRole.UserRole, hit.symbol)
                self.live_scanner_context.addItem(item.clone())

    def _refresh_scanner_latest_row(self, quote: QuoteSnapshot) -> None:
        previous = previous_regular_close(quote, self.current_bars).value
        change = quote.last - previous if previous not in (None, Decimal("0")) else None
        percent = change / previous * Decimal("100") if change is not None and previous else None
        row = ScannerRow(
            symbol=quote.instrument.identifier.symbol,
            company=quote.instrument.name or "Company name unavailable",
            price=quote.last, change=change, change_percent=percent, volume=quote.volume,
            day_high=quote.day_high, day_low=quote.day_low,
            freshness=freshness_label(freshness=quote.freshness, delay=quote.delay_label, received_at=quote.timestamp),
            sources=(self._fabric_provider_name(self._last_quote_provider_id),), updated_at=quote.timestamp,
        )
        self._scanner_rows = aggregate_scanner_rows([*getattr(self, "_scanner_rows", []), row])
        self._render_scanner_rows()
        if hasattr(self, "scanner_total_text"):
            self.scanner_total_text.setText(str(len(self._scanner_rows)))

    def runtime_alert_notification(self, notification: AlertNotification) -> None:
        event_label = humanize_event_code(notification.alert_type.value)
        message = humanize_status_text(notification.message)
        text = f"{event_label} · {notification.symbol or 'Market'} · {message} · {notification.occurred_at.astimezone(NEW_YORK):%b %d, %I:%M %p ET}"
        market_types = {AlertType.TRADE_HALT, AlertType.TRADE_RESUME}
        if notification.alert_type in market_types and hasattr(self, "market_alert_list"):
            category = "Resumptions" if notification.alert_type == AlertType.TRADE_RESUME else "Trading Halts"
            self._market_alert_records.append((category, notification.symbol or "", text))
            self._render_market_alerts()
        # Runtime notifications are automatic official/system events. Your
        # Alerts is reserved exclusively for user-created rules.
        if hasattr(self, "alert_history_list"):
            if self.alert_history_list.count() == 1 and self.alert_history_list.item(0).text().startswith("No alerts"):
                self.alert_history_list.clear()
            self.alert_history_list.insertItem(0, text)
        if hasattr(self, "live_recent_alerts"):
            if self.live_recent_alerts.count() == 1 and self.live_recent_alerts.item(0).text().startswith("No recent"):
                self.live_recent_alerts.clear()
            self.live_recent_alerts.insertItem(0, text)

    def _render_market_alerts(self, selected: str | None = None) -> None:
        if not hasattr(self, "market_alert_list"):
            return
        selected = selected or (self.market_alert_filter.currentText() if hasattr(self, "market_alert_filter") else "All")
        watched = set(self._watchlist_symbols())
        self.market_alert_list.clear()
        for category, symbol, text in self._market_alert_records:
            if selected not in {"All", category} and not (selected == "Watchlist" and symbol in watched):
                continue
            self.market_alert_list.addItem(text)
        if not self.market_alert_list.count():
            self.market_alert_list.addItem("No current market notices match this filter from the checked official sources.")

    def runtime_alert_sound(self, notification: AlertNotification) -> None:  # noqa: ARG002
        if QApplication is not None:
            QApplication.beep()

    def runtime_alert_desktop(self, notification: AlertNotification) -> None:
        if self._tray_controller is not None:
            self._tray_controller.show_message(notification.title, notification.message)

    def _configure_system_tray(self) -> None:
        if self._qt_application is None:
            return
        self._tray_controller = SystemTrayController(
            window=self._qt_window,
            application=self._qt_application,
            icon=self._qt_window.windowIcon(),
            on_exit=self._exit_application,
        )
        self._tray_controller.install()
        about_to_quit = getattr(self._qt_application, "aboutToQuit", None)
        connect = getattr(about_to_quit, "connect", None)
        if callable(connect):
            connect(self._on_application_about_to_quit)

    def _intercept_window_close(self) -> bool:
        if os.environ.get("RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE", "").strip() == "1":
            return False
        if self._tray_controller is None:
            return False
        return self._tray_controller.intercept_close()

    def _exit_application(self) -> None:
        self._shutdown_runtime()
        if self._qt_application is not None:
            quit_application = getattr(self._qt_application, "quit", None)
            if callable(quit_application):
                quit_application()

    def _on_application_about_to_quit(self) -> None:
        if self._tray_controller is not None:
            self._tray_controller.prepare_for_application_exit()
        self._shutdown_runtime()

    def _shutdown_runtime(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._persist_ui_state()
        if hasattr(self, "live_refresh_timer"):
            self.live_refresh_timer.stop()
        if hasattr(self, "_quote_coalesce_timer"):
            self._quote_coalesce_timer.stop()
        self._cancel_stale_quote_tasks()
        for timer in self._quote_timeout_timers.values():
            timer.stop()
        self._quote_timeout_timers.clear()
        if self._quote_thread_pool is not None and not self._quote_pool_is_global:
            self._quote_thread_pool.clear()
        if hasattr(self, "runtime"):
            self.runtime.shutdown()
        if hasattr(self, "app"):
            self.app.shutdown()
        if self._tray_controller is not None:
            self._tray_controller.dispose()


def build_window(**kwargs: Any) -> RangeScoutWindow:
    return RangeScoutWindow(**kwargs)
