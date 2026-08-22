from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

try:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon
except Exception:  # pragma: no cover - exercised only without the optional GUI runtime
    QAction = QMenu = QSystemTrayIcon = None  # type: ignore[assignment]


@dataclass
class TrayLifecycleState:
    """Small, deterministic lifecycle state kept separate from Qt widgets."""

    exit_requested: bool = False
    close_notice_shown: bool = False

    def should_hide_on_close(self, *, tray_available: bool, application_closing: bool = False) -> bool:
        return tray_available and not self.exit_requested and not application_closing

    def request_exit(self) -> None:
        self.exit_requested = True


class SystemTrayController:
    """Own the persistent RangeScout tray icon and close-to-tray lifecycle."""

    def __init__(
        self,
        *,
        window: Any,
        application: Any,
        icon: Any,
        on_exit: Callable[[], None],
        tray_icon_type: Any | None = None,
        menu_type: Any | None = None,
        action_type: Any | None = None,
    ) -> None:
        self._window = window
        self._application = application
        self._icon = icon
        self._on_exit = on_exit
        self._tray_icon_type = tray_icon_type if tray_icon_type is not None else QSystemTrayIcon
        self._menu_type = menu_type if menu_type is not None else QMenu
        self._action_type = action_type if action_type is not None else QAction
        self._state = TrayLifecycleState()
        self._tray_icon: Any | None = None
        self._menu: Any | None = None
        self._open_action: Any | None = None
        self._exit_action: Any | None = None

    @property
    def state(self) -> TrayLifecycleState:
        return self._state

    @property
    def tray_icon(self) -> Any | None:
        return self._tray_icon

    def _system_tray_available(self) -> bool:
        tray_type = self._tray_icon_type
        if tray_type is None:
            return False
        checker = getattr(tray_type, "isSystemTrayAvailable", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    @property
    def available(self) -> bool:
        return self._tray_icon is not None and self._system_tray_available()

    def install(self) -> bool:
        """Install one persistent tray icon and matching Open/Exit menu."""

        if self._tray_icon is not None:
            return self.available
        if self._tray_icon_type is None or self._menu_type is None or self._action_type is None:
            return False
        if not self._system_tray_available():
            return False

        tray_icon = self._tray_icon_type(self._icon, self._window)
        tray_icon.setToolTip("RangeScout")

        menu = self._menu_type(self._window)
        open_action = self._action_type("Open RangeScout", menu)
        exit_action = self._action_type("Exit RangeScout", menu)
        open_action.triggered.connect(self.restore)
        exit_action.triggered.connect(self.request_exit)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(exit_action)

        tray_icon.setContextMenu(menu)
        tray_icon.activated.connect(self._on_activated)
        tray_icon.show()

        self._tray_icon = tray_icon
        self._menu = menu
        self._open_action = open_action
        self._exit_action = exit_action
        return True

    def intercept_close(self) -> bool:
        """Hide the main window when the user presses X/Alt+F4.

        ``False`` deliberately falls back to a normal close when the desktop has
        no usable tray, preventing an inaccessible headless RangeScout process.
        """

        application_closing = False
        if self._application is not None:
            for probe_name in ("closingDown", "isSavingSession"):
                probe = getattr(self._application, probe_name, None)
                if not callable(probe):
                    continue
                try:
                    application_closing = application_closing or bool(probe())
                except Exception:
                    continue

        is_visible = getattr(self._window, "isVisible", None)
        if callable(is_visible) and not bool(is_visible()):
            return False

        tray_available = self.available or self.install()
        if not self._state.should_hide_on_close(
            tray_available=tray_available,
            application_closing=application_closing,
        ):
            if not tray_available and self._application is not None:
                setter = getattr(self._application, "setQuitOnLastWindowClosed", None)
                if callable(setter):
                    setter(True)
            return False

        if self._application is not None:
            setter = getattr(self._application, "setQuitOnLastWindowClosed", None)
            if callable(setter):
                setter(False)
        if self._tray_icon is not None:
            self._tray_icon.show()
        self._window.hide()
        if not self._state.close_notice_shown and self._tray_icon is not None:
            self._tray_icon.showMessage(
                "RangeScout is still running",
                "Use the tray icon to reopen RangeScout or choose Exit RangeScout.",
            )
            self._state.close_notice_shown = True
        return True

    def restore(self) -> None:
        if self._state.exit_requested:
            return
        is_minimized = getattr(self._window, "isMinimized", None)
        if callable(is_minimized) and bool(is_minimized()):
            self._window.showNormal()
        else:
            self._window.show()
        raise_window = getattr(self._window, "raise_", None)
        if callable(raise_window):
            raise_window()
        activate = getattr(self._window, "activateWindow", None)
        if callable(activate):
            activate()

    def _on_activated(self, reason: Any) -> None:
        tray_type = self._tray_icon_type
        activation = getattr(tray_type, "ActivationReason", None)
        if activation is None:
            return
        if reason in (activation.Trigger, activation.DoubleClick):
            self.restore()

    def show_message(self, title: str, message: str) -> bool:
        if self._state.exit_requested:
            return False
        if self._tray_icon is None and not self.install():
            return False
        if not self.available or self._tray_icon is None:
            return False
        self._tray_icon.showMessage(title, message)
        return True

    def request_exit(self) -> None:
        if self._state.exit_requested:
            return
        self._state.request_exit()
        if self._tray_icon is not None:
            self._tray_icon.hide()
        self._on_exit()

    def prepare_for_application_exit(self) -> None:
        self._state.request_exit()
        if self._tray_icon is not None:
            self._tray_icon.hide()

    def dispose(self) -> None:
        if self._tray_icon is not None:
            self._tray_icon.hide()
