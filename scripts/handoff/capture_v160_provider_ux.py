#!/usr/bin/env python
"""Capture deterministic provider UX and cleaned Settings evidence."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.application.bootstrap import RangeScoutApplication
from app.security.credentials import InMemoryCredentialStore
from app.ui.main import RangeScoutWindow


def capture(output: Path, profile: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    application = RangeScoutApplication(data_dir=profile, credential_store=InMemoryCredentialStore())
    qt = QApplication.instance() or QApplication([])
    window = RangeScoutWindow(application=application, auto_refresh=False)
    created: list[Path] = []
    try:
        window._qt_window.resize(1672, 941)
        window._qt_window.show()
        window.tabs.setCurrentWidget(window.settings_tab)
        window.theme_combo.setCurrentText("Dark")
        window._apply_theme("dark", persist=False)
        qt.processEvents()
        settings_path = output / "settings_clean_dark_1672x941.png"
        window._qt_window.grab().save(str(settings_path))
        created.append(settings_path)

        window._open_data_providers()
        dialog = window._data_providers_dialog
        assert dialog is not None
        dialog.resize(1120, 720)
        assert dialog.select_provider("finnhub")
        dialog.key_input.setText("••••••••••••")
        qt.processEvents()
        finnhub_dark = output / "data_providers_finnhub_dark_1120x720.png"
        dialog.grab().save(str(finnhub_dark))
        created.append(finnhub_dark)

        assert dialog.select_provider("logo_dev")
        dialog.key_input.setText("••••••••••••")
        qt.processEvents()
        logo_dark = output / "data_providers_logo_dev_dark_1120x720.png"
        dialog.grab().save(str(logo_dark))
        created.append(logo_dark)

        window.theme_combo.setCurrentText("Light")
        window._apply_theme("light", persist=False)
        assert dialog.select_provider("finnhub")
        dialog.key_input.setText("••••••••••••")
        qt.processEvents()
        finnhub_light = output / "data_providers_finnhub_light_1120x720.png"
        dialog.grab().save(str(finnhub_light))
        created.append(finnhub_light)
    finally:
        window._shutdown_runtime()
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    for path in capture(Path(args.output), Path(args.profile)):
        print(path)


if __name__ == "__main__":
    main()
