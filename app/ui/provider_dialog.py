"""Owned provider-routing and secure credential configuration surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.configuration.settings import FORCED_PROVIDER_MODES, SMART_PROVIDER_MODE
from app.market_data.contracts import Capability
from app.security.credentials import ProviderCredentials

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


SIGNUP_URLS = {
    "finnhub": "https://finnhub.io/register",
    "twelve_data": "https://twelvedata.com/",
    "alpha_vantage": "https://www.alphavantage.co/support/#api-key",
    "fred": "https://fred.stlouisfed.org/docs/api/api_key.html",
    "congress": "https://api.congress.gov/sign-up/",
    "logo_dev": "https://www.logo.dev/",
}

DISPLAY_NAMES = {
    "smart": "Smart Search (Recommended)",
    "twelve_data": "Twelve Data",
    "alpha_vantage": "Alpha Vantage",
    "coinpaprika": "CoinPaprika",
    "coinbase_exchange": "Coinbase",
    "logo_dev": "Logo.dev",
    "congress": "Congress.gov",
}

REFERENCE_ROWS = (
    ("sec", "SEC EDGAR", "Research / fundamentals", "No key required", "Official HTTPS source"),
    ("nasdaq", "Nasdaq Trader", "Listings / market halts", "No key required", "Official HTTPS source"),
    ("white_house", "White House", "Official catalysts", "No key required", "Official HTTPS source"),
    ("congress", "Congress.gov", "Official catalysts", "API key", "Free BYO key"),
    ("logo_dev", "Logo.dev", "Optional company logos", "Publishable key", "Optional BYO key"),
)


@dataclass(frozen=True)
class ProviderRow:
    provider_id: str
    name: str
    used_for: str
    key_type: str
    status: str
    details: str


def provider_rows(application: Any) -> list[ProviderRow]:
    rows: list[ProviderRow] = []
    health = application.market_data_router.health.snapshot()
    for item in application.fabric_provider_statuses():
        provider_id = str(item["provider_id"])
        capabilities = tuple(str(value) for value in item["capabilities"])
        configured = bool(item["configured"])
        enabled = bool(item["enabled"])
        if not enabled:
            status = "Disabled / unsupported"
        elif bool(item["requires_credentials"]):
            status = "Configured" if configured else "Missing API key"
        else:
            status = "Available"
        relevant = [value for key, value in health.items() if key.startswith(provider_id + "|")]
        if relevant:
            if any(value.get("rate_limited_until") for value in relevant):
                status = "Rate limited"
            elif any(value.get("circuit_state") == "open" for value in relevant):
                status = "Temporarily unavailable"
        details = str(item.get("reason") or "")
        rows.append(
            ProviderRow(
                provider_id,
                str(item["display_name"]),
                ", ".join(value.replace("_", " ").title() for value in capabilities) or "Unavailable",
                "API key" if item["requires_credentials"] else "No key required",
                status,
                details,
            )
        )
    for provider_id, name, used_for, key_type, details in REFERENCE_ROWS:
        configured = False
        try:
            configured = application.credential_store.load(provider_id) is not None
        except Exception:
            pass
        status = "Configured" if configured else ("Optional" if provider_id == "logo_dev" else "Available")
        if key_type != "No key required" and not configured:
            status = "Missing API key" if provider_id == "congress" else "Optional"
        rows.append(ProviderRow(provider_id, name, used_for, key_type, status, details))
    return rows


def split_provider_rows(rows: list[ProviderRow]) -> tuple[list[ProviderRow], list[ProviderRow]]:
    unavailable = [row for row in rows if row.status == "Disabled / unsupported"]
    active = [row for row in rows if row.status != "Disabled / unsupported"]
    return active, unavailable


class DataProvidersDialog(QDialog):
    """Single public surface for routing choice, provider state, and BYO credentials."""

    def __init__(self, application: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.application = application
        self.setObjectName("data_providers_dialog")
        self.setWindowTitle("Data Providers & API Keys")
        self.setModal(False)
        self.resize(1120, 720)
        self._rows: list[ProviderRow] = []

        outer = QVBoxLayout(self)
        title = QLabel("Data Providers & API Keys")
        title.setObjectName("surface_title")
        subtitle = QLabel(
            "Choose automatic Smart Search or force one eligible provider. API keys stay in Windows Credential Manager."
        )
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        mode_group = QGroupBox("Quote Provider Mode")
        mode_form = QFormLayout(mode_group)
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("provider_mode_combo")
        self.mode_combo.addItem(DISPLAY_NAMES[SMART_PROVIDER_MODE], SMART_PROVIDER_MODE)
        statuses = {str(item["provider_id"]): item for item in application.fabric_provider_statuses()}
        for provider_id in sorted(FORCED_PROVIDER_MODES, key=lambda value: DISPLAY_NAMES.get(value, value).lower()):
            item = statuses.get(provider_id)
            if item and item["enabled"] and Capability.QUOTE.value in item["capabilities"]:
                self.mode_combo.addItem(DISPLAY_NAMES.get(provider_id, str(item["display_name"])), provider_id)
        current = self.mode_combo.findData(application.settings.provider_mode)
        self.mode_combo.setCurrentIndex(max(0, current))
        self.mode_help = QLabel("")
        self.mode_help.setWordWrap(True)
        mode_form.addRow("Provider Mode", self.mode_combo)
        mode_form.addRow("Behavior", self.mode_help)
        outer.addWidget(mode_group)

        table_help = QLabel("Select a provider or use Configure to manage its credential and view details.")
        table_help.setWordWrap(True)
        outer.addWidget(table_help)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("provider_status_table")
        self.table.setHorizontalHeaderLabels(("Provider", "Used For", "API Key Required", "Status", "Action"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 150)
        outer.addWidget(self.table, 1)

        self.unavailable_toggle = QPushButton("Unavailable / Future Providers")
        self.unavailable_toggle.setCheckable(True)
        self.unavailable_toggle.setChecked(False)
        self.unavailable_toggle.setObjectName("unavailable_providers_toggle")
        self.unavailable_table = QTableWidget(0, 3)
        self.unavailable_table.setObjectName("unavailable_provider_table")
        self.unavailable_table.setHorizontalHeaderLabels(("Provider", "Status", "Reason"))
        self.unavailable_table.verticalHeader().setVisible(False)
        self.unavailable_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.unavailable_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.unavailable_table.setVisible(False)
        outer.addWidget(self.unavailable_toggle)
        outer.addWidget(self.unavailable_table)

        credential_group = QGroupBox("Selected Provider Credential")
        credential_form = QFormLayout(credential_group)
        self.selected_name = QLabel("Select a provider row")
        self.key_input = QLineEdit()
        self.key_input.setObjectName("provider_credential_input")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_input.setPlaceholderText("Enter your own provider key")
        self.save_button = QPushButton("Save Securely")
        self.delete_button = QPushButton("Delete Stored Key")
        self.signup_button = QPushButton("Get API Key")
        self.details_button = QPushButton("Details")
        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        for button in (self.save_button, self.delete_button, self.signup_button, self.details_button):
            buttons_layout.addWidget(button)
        buttons_layout.addStretch(1)
        self.credential_status = QLabel("Credentials are never written to settings.json or displayed after saving.")
        self.credential_status.setWordWrap(True)
        self.details_text = QLabel("")
        self.details_text.setObjectName("selected_provider_details")
        self.details_text.setWordWrap(True)
        self.details_text.setVisible(False)
        credential_form.addRow("Provider", self.selected_name)
        credential_form.addRow("Key", self.key_input)
        credential_form.addRow("Actions", buttons)
        credential_form.addRow("Storage", self.credential_status)
        credential_form.addRow("Details", self.details_text)
        outer.addWidget(credential_group)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        footer = QHBoxLayout()
        footer.addWidget(QLabel("No shared Dietrich AI Labs keys. No consumer-page scraping."))
        footer.addStretch(1)
        footer.addWidget(close_button)
        outer.addLayout(footer)

        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.save_button.clicked.connect(self._save)
        self.delete_button.clicked.connect(self._delete)
        self.signup_button.clicked.connect(self._signup)
        self.details_button.clicked.connect(self._toggle_details)
        self.unavailable_toggle.toggled.connect(self.unavailable_table.setVisible)
        self.refresh()
        self._mode_changed()

    def refresh(self) -> None:
        selected_id = self._selected_provider_id()
        self._rows, unavailable = split_provider_rows(provider_rows(self.application))
        for row_index in range(self.table.rowCount()):
            widget = self.table.cellWidget(row_index, 4)
            if widget is not None:
                self.table.removeCellWidget(row_index, 4)
                widget.setParent(None)
                widget.deleteLater()
        self.table.clearContents()
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            values = (row.name, row.used_for, row.key_type, row.status)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, row.provider_id)
                item.setToolTip(row.details)
                self.table.setItem(row_index, column, item)
            if row.key_type != "No key required":
                configure = QPushButton("Configure")
                configure.setObjectName(f"configure_{row.provider_id}")
                configure.clicked.connect(lambda _checked=False, provider_id=row.provider_id: self.select_provider(provider_id, focus=True))
                self.table.setCellWidget(row_index, 4, configure)
            else:
                action = QTableWidgetItem("No action required")
                action.setData(Qt.ItemDataRole.UserRole, row.provider_id)
                self.table.setItem(row_index, 4, action)
            if row.provider_id == selected_id:
                self.table.selectRow(row_index)
        if not self.table.selectedItems() and self._rows:
            self.table.selectRow(0)
        self.unavailable_toggle.setText(f"Unavailable / Future Providers ({len(unavailable)})")
        self.unavailable_table.setRowCount(len(unavailable))
        for row_index, row in enumerate(unavailable):
            for column, value in enumerate((row.name, row.status, row.details or "Pending provider/terms review")):
                self.unavailable_table.setItem(row_index, column, QTableWidgetItem(value))

    def _mode_changed(self, _index: int = 0) -> None:
        provider_id = str(self.mode_combo.currentData() or SMART_PROVIDER_MODE)
        normalized = self.application.set_provider_mode(provider_id)
        if normalized == SMART_PROVIDER_MODE:
            self.mode_help.setText(
                "Races only eligible, configured, healthy providers. The first fresh valid result wins; quota and circuit-breaker state are honored."
            )
        else:
            self.mode_help.setText(
                f"Forced mode: only {self.mode_combo.currentText()} is queried. RangeScout will show a clear error and will not silently fall back."
            )

    def _selected_provider_id(self) -> str:
        selected = self.table.selectedItems()
        return str(selected[0].data(Qt.ItemDataRole.UserRole)) if selected else ""

    def select_provider(self, provider_id: str, *, focus: bool = False) -> bool:
        for row_index, row in enumerate(self._rows):
            if row.provider_id == provider_id:
                self.table.selectRow(row_index)
                self.table.scrollToItem(self.table.item(row_index, 0))
                if focus and row.key_type != "No key required":
                    QTimer.singleShot(0, lambda: self.key_input.setFocus(Qt.FocusReason.OtherFocusReason))
                return True
        return False

    def _selection_changed(self) -> None:
        provider_id = self._selected_provider_id()
        row = next((item for item in self._rows if item.provider_id == provider_id), None)
        if row is None:
            return
        self.selected_name.setText(row.name)
        needs_key = row.key_type != "No key required"
        self.key_input.setVisible(needs_key)
        self.save_button.setVisible(needs_key)
        self.delete_button.setVisible(needs_key)
        self.signup_button.setVisible(provider_id in SIGNUP_URLS)
        self.signup_button.setText("Get Publishable Key" if provider_id == "logo_dev" else "Get API Key")
        self.key_input.setPlaceholderText("Enter publishable key" if provider_id == "logo_dev" else "Enter your own provider API key")
        self.key_input.clear()
        self.details_text.setText(row.details or "No additional provider details are available.")
        self.details_text.setVisible(False)
        self.details_button.setText("Details")
        self.credential_status.setText(
            "No key required." if not needs_key else f"{row.status}. Stored securely; saved values are never displayed."
        )

    def _toggle_details(self) -> None:
        visible = not self.details_text.isVisible()
        self.details_text.setVisible(visible)
        self.details_button.setText("Hide Details" if visible else "Details")

    def _save(self) -> None:
        provider_id = self._selected_provider_id()
        value = self.key_input.text().strip()
        field = "publishable_key" if provider_id == "logo_dev" else "api_key"
        try:
            self.application.provider_configuration.save_credentials(provider_id, {field: value})
        except Exception:
            self.credential_status.setText("Key was not saved. Check the value and Windows secure storage.")
        else:
            self.credential_status.setText("Saved securely. The key will not be displayed again.")
        finally:
            self.key_input.clear()
        self.refresh()

    def _delete(self) -> None:
        provider_id = self._selected_provider_id()
        try:
            self.application.provider_configuration.delete_credentials(provider_id)
        except Exception:
            self.credential_status.setText("Stored key could not be deleted safely.")
        else:
            self.credential_status.setText("Stored key deleted.")
        self.key_input.clear()
        self.refresh()

    def _signup(self) -> None:
        url = SIGNUP_URLS.get(self._selected_provider_id())
        if not url:
            return
        try:
            opened = bool(QDesktopServices.openUrl(QUrl(url)))
        except Exception:
            opened = False
        if opened:
            self.credential_status.setText("Opened the official provider signup page.")
        else:
            self.credential_status.setText(f"Open the official provider signup page: {url}")
