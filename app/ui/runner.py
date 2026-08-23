from __future__ import annotations

import sys
import os
from pathlib import Path

from app.ui.main import NoGuiRuntimeError, build_window
from app.ui.branding import load_application_icon
from app.domain.errors import DataRootError

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
except Exception:
    QApplication = None  # type: ignore[assignment]
    QMessageBox = None  # type: ignore[assignment]
    QTimer = None  # type: ignore[assignment]


STARTUP_TAB_INDEXES = {
    "market": 0,
    "live-trader": 1,
    "research": 2,
    "watchlists": 3,
    "scanner": 4,
    "alerts": 5,
    "notes": 6,
    "exports": 7,
    "settings": 8,
}

WINDOWS_APP_USER_MODEL_ID = "DietrichAILabs.RangeScout"


def set_windows_app_user_model_id(
    *,
    platform_name: str | None = None,
    setter=None,
) -> bool:
    """Set the stable process identity used by Windows taskbar grouping."""
    if (platform_name or sys.platform) != "win32":
        return False
    try:
        if setter is None:
            import ctypes

            setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        return int(setter(WINDOWS_APP_USER_MODEL_ID)) == 0
    except Exception:
        return False


def create_qt_application(arguments: list[str]):
    if QApplication is None:
        raise NoGuiRuntimeError("PySide6 is required to launch the desktop UI.")
    set_windows_app_user_model_id()
    app = QApplication(arguments)
    app.setApplicationName("RangeScout")
    app.setOrganizationName("Dietrich AI Labs")
    display_name_setter = getattr(app, "setApplicationDisplayName", None)
    if callable(display_name_setter):
        display_name_setter("RangeScout")
    icon = load_application_icon()
    icon_setter = getattr(app, "setWindowIcon", None)
    if icon is not None and callable(icon_setter):
        is_null = getattr(icon, "isNull", None)
        if not callable(is_null) or not bool(is_null()):
            icon_setter(icon)
    return app


def main() -> None:
    diagnostic_output = _argument_value("--live-network-diagnostic-output")
    diagnostic_data_dir = _argument_value("--live-network-diagnostic-data-dir")
    diagnostic_symbols = _argument_value("--live-network-diagnostic-symbols")
    qt_arguments = [
        value for value in sys.argv
        if not value.startswith("--live-network-diagnostic-")
        and value not in {diagnostic_output, diagnostic_data_dir, diagnostic_symbols}
    ]
    app = create_qt_application(qt_arguments)
    if diagnostic_output:
        from app.diagnostics.live_network import run_live_network_diagnostic

        symbols = tuple(
            symbol.strip().upper() for symbol in (diagnostic_symbols or "AAPL,BA,NVDA").split(",") if symbol.strip()
        )
        if symbols != ("AAPL", "BA", "NVDA"):
            raise SystemExit("The supported live-network diagnostic symbol set is AAPL,BA,NVDA.")
        data_dir = Path(diagnostic_data_dir) if diagnostic_data_dir else Path(diagnostic_output).parent / "diagnostic-profile"
        raise SystemExit(
            run_live_network_diagnostic(
                app, output_path=Path(diagnostic_output), data_dir=data_dir, symbols=symbols
            )
        )
    try:
        startup_symbol = os.environ.get("RANGESCOUT_START_SYMBOL", "").strip().upper()
        window = build_window(auto_refresh=not startup_symbol)
    except DataRootError:
        if QMessageBox is None:
            raise
        QMessageBox.critical(
            None,
            "Startup Error",
            "RangeScout cannot safely use your local application-data folder. "
            "No alternate temporary storage was used. "
            "Check your AppData permissions/path and try again.",
        )
        raise SystemExit(1)

    if startup_symbol:
        symbol = startup_symbol
        window.market_symbol_input.setText(symbol)
        window.set_active_symbol(symbol, source="global-search")

    startup_tab = os.environ.get("RANGESCOUT_START_TAB", "").strip().lower()
    startup_theme = os.environ.get("RANGESCOUT_START_THEME", "").strip().lower()
    if startup_tab:
        tab_index = STARTUP_TAB_INDEXES.get(startup_tab)
        if tab_index is not None and getattr(window, "tabs", None) is not None:
            window.tabs.setCurrentIndex(tab_index)
            app.processEvents()

    if startup_theme:
        theme_combo = getattr(window, "theme_combo", None)
        if theme_combo is not None and hasattr(theme_combo, "setCurrentText"):
            if startup_theme in {"system", "light", "dark"}:
                try:
                    theme_combo.setCurrentText(startup_theme)
                    app.processEvents()
                except Exception:
                    pass

    window.show()

    if startup_symbol and QTimer is not None:
        def _load_startup_symbol() -> None:
            window._on_refresh()
            window._on_research_refresh()

        # Paint the requested symbol/tab/theme first, then perform legitimate
        # provider loads on the running Qt event loop.
        QTimer.singleShot(50, _load_startup_symbol)

    auto_close_seconds = os.environ.get("RANGESCOUT_AUTO_CLOSE_SECONDS")
    if auto_close_seconds and QTimer is not None:
        try:
            close_seconds = max(float(auto_close_seconds), 0.25)
            QTimer.singleShot(int(close_seconds * 1000), window.exit_application)
        except (TypeError, ValueError):
            pass

    exit_code = app.exec()
    raise SystemExit(exit_code)


def run() -> None:
    main()


def _argument_value(name: str) -> str | None:
    prefix = name + "="
    for index, value in enumerate(sys.argv):
        if value.startswith(prefix):
            return value[len(prefix):]
        if value == name and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return None


if __name__ == "__main__":
    main()
