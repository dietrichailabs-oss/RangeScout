from __future__ import annotations

from decimal import Decimal

import pytest

from app.application.bootstrap import RangeScoutApplication
from app.scanner.engine import ScannerRow
from app.security.credentials import InMemoryCredentialStore
from app.ui.main import QApplication, QMessageBox, build_window


@pytest.fixture()
def watchlist_window(tmp_path):
    if QApplication is None:
        pytest.skip("PySide6 unavailable")
    qt = QApplication.instance() or QApplication([])
    application = RangeScoutApplication(
        data_dir=tmp_path / "RangeScout",
        credential_store=InMemoryCredentialStore(),
    )
    window = build_window(application=application, auto_refresh=False, catalyst_sources=[])
    try:
        yield window, qt
    finally:
        window._shutdown_runtime()


def _select(window, qt, watchlist_id: str) -> None:
    row = next(
        index
        for index in range(window.watchlist_widget.count())
        if window.watchlist_widget.item(index).text().startswith(f"{watchlist_id} |")
    )
    window.watchlist_widget.setCurrentRow(row)
    qt.processEvents()


def test_selecting_watchlist_persists_target_and_keeps_symbol_entry_empty(watchlist_window) -> None:
    window, qt = watchlist_window
    window.watchlist_store.create("list-a", "List A").symbols.extend(["AAPL", "MSFT"])
    window.watchlist_store._save()
    window.watchlist_store.create("list-b", "List B")
    window._refresh_watchlists_widget()

    _select(window, qt, "list-a")

    assert window.app.settings.selected_watchlist == "list-a"
    assert window.watchlist_id_input.text() == "list-a"
    assert window.watchlist_title_input.text() == "List A"
    assert window.watchlist_symbol_input.text() == ""
    persisted = (window.app.data_dir / "settings.json").read_text(encoding="utf-8")
    assert '"selected_watchlist": "list-a"' in persisted


def test_add_remove_and_duplicate_membership_are_single_symbol_operations(watchlist_window) -> None:
    window, qt = watchlist_window
    window.watchlist_store.create("list-a", "List A")
    window._refresh_watchlists_widget()
    _select(window, qt, "list-a")

    window.watchlist_symbol_input.setText("  msft  ")
    window._on_watchlist_add_symbol()
    window.watchlist_symbol_input.setText("MSFT")
    window._on_watchlist_add_symbol()

    assert window.watchlist_store.watchlists["list-a"].symbols == ["MSFT"]
    assert window.watchlist_symbol_input.text() == ""

    window.watchlist_symbol_input.setText("msft")
    window._on_watchlist_remove_symbol()

    assert window.watchlist_store.watchlists["list-a"].symbols == []
    assert window.watchlist_symbol_input.text() == ""


def test_invalid_or_missing_watchlist_input_is_visible_to_the_user(
    watchlist_window, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _qt = watchlist_window
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, message: warnings.append(message))

    window._on_watchlist_add_symbol()
    assert warnings and "Create or select" in warnings[-1]

    window.watchlist_store.create("list-a", "List A")
    window._refresh_watchlists_widget()
    window.watchlist_symbol_input.setText("AAPL, MSFT")
    window._on_watchlist_add_symbol()
    assert "Enter one valid symbol" in warnings[-1]
    assert window.watchlist_store.watchlists["list-a"].symbols == []


def test_quick_add_targets_selected_list_not_first_list(watchlist_window) -> None:
    window, qt = watchlist_window
    window.watchlist_store.create("list-b", "List B")
    window.watchlist_store.create("list-a", "List A")
    window._refresh_watchlists_widget()
    _select(window, qt, "list-a")
    window.set_active_symbol("NVDA", source="test")

    window._on_add_active_symbol_to_watchlist()

    assert window.app.settings.selected_watchlist == "list-a"
    assert window.watchlist_store.watchlists["list-a"].symbols == ["NVDA"]
    assert window.watchlist_store.watchlists["list-b"].symbols == []


def test_quick_add_creates_default_watchlist_when_none_exist(watchlist_window) -> None:
    window, _qt = watchlist_window
    window.set_active_symbol("AAPL", source="test")

    window._on_add_active_symbol_to_watchlist()

    record = window.watchlist_store.watchlists["my-watchlist"]
    assert record.title == "My Watchlist"
    assert record.symbols == ["AAPL"]
    assert window.app.settings.selected_watchlist == "my-watchlist"
    assert "Watchlisted" in window.market_watchlist_button.text()


def test_existing_membership_and_active_symbol_transition_update_button_state(watchlist_window) -> None:
    window, qt = watchlist_window
    window.watchlist_store.create("list-a", "List A")
    window.watchlist_store.add_symbol("list-a", "AAPL")
    window._refresh_watchlists_widget()
    _select(window, qt, "list-a")

    window.set_active_symbol("AAPL", source="test")
    assert "Watchlisted" in window.market_watchlist_button.text()
    window._on_add_active_symbol_to_watchlist()
    assert window.watchlist_store.watchlists["list-a"].symbols == ["AAPL"]

    window.set_active_symbol("MSFT", source="test")
    assert window.market_watchlist_button.text() == "Add to Watchlist"


def test_mutations_refresh_ticker_runtime_and_scanner_watchlist_universe(
    watchlist_window, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, qt = watchlist_window
    window.watchlist_store.create("list-a", "List A")
    window._refresh_watchlists_widget()
    _select(window, qt, "list-a")
    runtime_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        window.runtime,
        "set_symbols",
        lambda active, watched: runtime_calls.append((active, list(watched))),
    )
    window._scanner_rows = [
        ScannerRow("AAPL", "Apple", Decimal("100"), sources=("fixture",)),
        ScannerRow("MSFT", "Microsoft", Decimal("200"), sources=("fixture",)),
    ]
    window._active_scanner_filter = "Watchlist Only"

    window.watchlist_symbol_input.setText("AAPL")
    window._on_watchlist_add_symbol()

    assert window._ticker_watchlist_symbols == ["AAPL"]
    assert runtime_calls[-1] == (window.current_symbol, ["AAPL"])
    assert window.scanner_results.count() == 1
    assert window.scanner_results.item(0).data(256) == "AAPL"

    window.watchlist_symbol_input.setText("AAPL")
    window._on_watchlist_remove_symbol()

    assert window._ticker_watchlist_symbols == []
    assert runtime_calls[-1] == (window.current_symbol, [])
    assert window.scanner_results.item(0).data(256) is None
