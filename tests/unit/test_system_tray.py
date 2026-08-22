from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest

from app.ui.branding import APP_ICON_RELATIVE_PATH, application_icon_path
from app.ui.system_tray import SystemTrayController, TrayLifecycleState


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in list(self.callbacks):
            callback(*args)


class _Action:
    def __init__(self, text: str, parent=None) -> None:  # noqa: ANN001
        self.text = text
        self.parent = parent
        self.triggered = _Signal()


class _Menu:
    def __init__(self, parent=None) -> None:  # noqa: ANN001
        self.parent = parent
        self.actions = []

    def addAction(self, action) -> None:  # noqa: ANN001
        self.actions.append(action)

    def addSeparator(self) -> None:
        self.actions.append("separator")


class _TrayIcon:
    available = True

    class ActivationReason:
        Trigger = 1
        DoubleClick = 2
        Context = 3

    def __init__(self, icon, parent=None) -> None:  # noqa: ANN001
        self.icon = icon
        self.parent = parent
        self.tooltip = ""
        self.context_menu = None
        self.activated = _Signal()
        self.visible = False
        self.messages = []

    @classmethod
    def isSystemTrayAvailable(cls) -> bool:
        return cls.available

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def setContextMenu(self, menu) -> None:  # noqa: ANN001
        self.context_menu = menu

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def showMessage(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class _Application:
    def __init__(self) -> None:
        self.quit_on_last_window_closed = None
        self.closing = False
        self.saving_session = False

    def setQuitOnLastWindowClosed(self, value: bool) -> None:
        self.quit_on_last_window_closed = value

    def closingDown(self) -> bool:
        return self.closing

    def isSavingSession(self) -> bool:
        return self.saving_session


class _Window:
    def __init__(self) -> None:
        self.visible = True
        self.minimized = False
        self.hide_calls = 0
        self.show_calls = 0
        self.show_normal_calls = 0
        self.raise_calls = 0
        self.activate_calls = 0

    def isVisible(self) -> bool:
        return self.visible

    def isMinimized(self) -> bool:
        return self.minimized

    def hide(self) -> None:
        self.hide_calls += 1
        self.visible = False

    def show(self) -> None:
        self.show_calls += 1
        self.visible = True

    def showNormal(self) -> None:
        self.show_normal_calls += 1
        self.visible = True
        self.minimized = False

    def raise_(self) -> None:
        self.raise_calls += 1

    def activateWindow(self) -> None:
        self.activate_calls += 1


def _controller(*, window: _Window | None = None, application: _Application | None = None):
    exits: list[str] = []
    icon = object()
    controller = SystemTrayController(
        window=window or _Window(),
        application=application or _Application(),
        icon=icon,
        on_exit=lambda: exits.append("exit"),
        tray_icon_type=_TrayIcon,
        menu_type=_Menu,
        action_type=_Action,
    )
    return controller, exits, icon


def test_tray_lifecycle_state_only_hides_before_explicit_exit() -> None:
    state = TrayLifecycleState()
    assert state.should_hide_on_close(tray_available=True)
    assert not state.should_hide_on_close(tray_available=False)
    assert not state.should_hide_on_close(tray_available=True, application_closing=True)
    state.request_exit()
    assert not state.should_hide_on_close(tray_available=True)


def test_persistent_tray_uses_same_icon_hides_restores_and_exits() -> None:
    _TrayIcon.available = True
    window = _Window()
    application = _Application()
    controller, exits, icon = _controller(window=window, application=application)

    assert controller.install()
    tray = controller.tray_icon
    assert tray is not None
    assert tray.icon is icon
    assert tray.tooltip == "RangeScout"
    assert tray.visible
    assert [item.text if isinstance(item, _Action) else item for item in tray.context_menu.actions] == [
        "Open RangeScout",
        "separator",
        "Exit RangeScout",
    ]
    assert application.quit_on_last_window_closed is None

    assert controller.intercept_close()
    assert not window.visible
    assert application.quit_on_last_window_closed is False
    assert tray.messages == [
        (
            "RangeScout is still running",
            "Use the tray icon to reopen RangeScout or choose Exit RangeScout.",
        )
    ]

    tray.activated.emit(_TrayIcon.ActivationReason.Trigger)
    assert window.visible
    assert window.show_calls == 1
    assert window.raise_calls == 1
    assert window.activate_calls == 1

    assert controller.intercept_close()
    assert len(tray.messages) == 1  # close-to-tray notice is once per session
    tray.context_menu.actions[0].triggered.emit()
    assert window.visible

    tray.context_menu.actions[-1].triggered.emit()
    assert exits == ["exit"]
    assert controller.state.exit_requested
    assert not tray.visible
    controller.request_exit()
    assert exits == ["exit"]  # explicit exit is idempotent


def test_tray_unavailable_falls_back_to_normal_close() -> None:
    _TrayIcon.available = False
    window = _Window()
    application = _Application()
    controller, exits, _icon = _controller(window=window, application=application)
    try:
        assert not controller.install()
        assert not controller.intercept_close()
        assert window.visible
        assert application.quit_on_last_window_closed is True
        assert exits == []
    finally:
        _TrayIcon.available = True


def test_os_session_shutdown_is_not_intercepted() -> None:
    _TrayIcon.available = True
    window = _Window()
    application = _Application()
    application.saving_session = True
    controller, _exits, _icon = _controller(window=window, application=application)
    assert controller.install()
    assert not controller.intercept_close()
    assert window.visible


def test_hidden_test_or_automation_window_is_not_intercepted() -> None:
    _TrayIcon.available = True
    window = _Window()
    window.visible = False
    application = _Application()
    controller, _exits, _icon = _controller(window=window, application=application)
    assert controller.install()
    assert not controller.intercept_close()
    assert application.quit_on_last_window_closed is None


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 is not installed",
)
def test_automation_close_bypasses_tray_interception(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ui.main import RangeScoutWindow

    class Tray:
        calls = 0

        def intercept_close(self) -> bool:
            self.calls += 1
            return True

    tray = Tray()
    fake_window = type("FakeRangeScoutWindow", (), {"_tray_controller": tray})()
    monkeypatch.setenv("RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE", "1")
    assert RangeScoutWindow._intercept_window_close(fake_window) is False
    assert tray.calls == 0


def test_non_intercepted_runtime_close_uses_explicit_application_exit() -> None:
    root = Path(__file__).resolve().parents[2]
    main = (root / "app" / "ui" / "main.py").read_text(encoding="utf-8")
    assert "self._qt_window.runtime_close_callback = self._exit_application" in main


def test_ui_settings_restart_probe_creates_controlled_profile_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.handoff import capture_ui_surfaces

    profile_root = tmp_path / "missing" / "rangescout-ui-profiles"
    launched_profiles: list[Path] = []

    def fake_capture(*_args, extra_env, **_kwargs):  # noqa: ANN001
        launched_profiles.append(Path(extra_env["HOME"]))
        return [{"run_success": False}]

    monkeypatch.setattr(capture_ui_surfaces, "_default_profile_root", lambda: profile_root)
    monkeypatch.setattr(capture_ui_surfaces, "_capture_packaged_exe", fake_capture)

    result = capture_ui_surfaces._run_settings_restart_probe(tmp_path, tmp_path / "RangeScout.exe")

    assert profile_root.is_dir()
    assert len(launched_profiles) == 2
    assert all(path.parent == profile_root for path in launched_profiles)
    assert result["restart_passed"] is False


def test_single_icon_asset_is_bound_to_window_tray_executable_and_installer() -> None:
    root = Path(__file__).resolve().parents[2]
    icon = root / APP_ICON_RELATIVE_PATH
    png = root / "docs" / "assets" / "rangescout-icon.png"
    assert application_icon_path(frozen_root=root) == icon
    assert icon.is_file() and icon.read_bytes()[:4] == b"\x00\x00\x01\x00"
    assert png.is_file() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    runner = (root / "app" / "ui" / "runner.py").read_text(encoding="utf-8")
    main = (root / "app" / "ui" / "main.py").read_text(encoding="utf-8")
    packaging = (root / "scripts" / "package_release.py").read_text(encoding="utf-8")
    release = (root / "scripts" / "release_engineering.py").read_text(encoding="utf-8")
    inno = (root / "packaging" / "windows" / "RangeScout.iss").read_text(encoding="utf-8")
    capture = (root / "scripts" / "handoff" / "capture_ui_surfaces.py").read_text(encoding="utf-8")
    installer_evidence = (root / "scripts" / "handoff" / "installer_evidence.py").read_text(encoding="utf-8")
    tray_runtime_evidence = (root / "scripts" / "handoff" / "tray_runtime_evidence.py").read_text(encoding="utf-8")

    assert "load_application_icon()" in runner
    assert "setWindowIcon" in runner
    assert "icon=self._qt_window.windowIcon()" in main
    assert '["--icon", str(app_icon)]' in packaging
    assert "/DAppIcon=" in release
    assert "SetupIconFile={#AppIcon}" in inno
    assert "window.exit_application()" in capture
    assert 'env["RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE"] = "1"' in capture
    assert 'env["RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE"] = "1"' in installer_evidence
    assert 'RANGESCOUT_AUTOMATION_EXIT_ON_CLOSE' in main
    assert 'self.exit_application()' in main
    assert '"window_icon_present"' in tray_runtime_evidence
    assert 'any(window_icon_handles.values())' in tray_runtime_evidence


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 is not installed",
)
def test_runtime_main_window_ignores_user_close_when_intercepted() -> None:
    from app.ui import main as ui_main

    qt_app = ui_main.QApplication.instance() or ui_main.QApplication([])
    window = ui_main._RuntimeMainWindow()
    calls: list[str] = []

    class Event:
        ignored = False

        def ignore(self) -> None:
            self.ignored = True

    event = Event()
    window.runtime_close_interceptor = lambda: calls.append("intercept") or True
    window.runtime_close_callback = lambda: calls.append("shutdown")
    window.closeEvent(event)
    assert event.ignored
    assert calls == ["intercept"]
    window.deleteLater()
    if qt_app is not None and not qt_app.closingDown():
        qt_app.processEvents()


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 is not installed",
)
def test_actual_qt_tray_icon_matches_application_icon_and_hides_visible_window() -> None:
    from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon

    from app.ui.branding import load_application_icon

    # A QApplication must exist before probing the native Windows tray.
    # The full suite happened to create one in an earlier test, but the
    # release gate deliberately runs this test alone as an independent
    # live-Qt check. Probing QSystemTrayIcon first can access-violate Qt's
    # Windows platform integration instead of returning a normal result.
    qt_app = QApplication.instance()
    if qt_app is None:
        qt_app = QApplication([])
    qt_app.processEvents()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        pytest.skip("A real system tray is unavailable in this Qt session")

    icon = load_application_icon()
    assert icon is not None and not icon.isNull()
    qt_app.setWindowIcon(icon)
    window = QMainWindow()
    window.setWindowIcon(icon)
    window.show()
    qt_app.processEvents()
    exits: list[str] = []
    controller = SystemTrayController(
        window=window,
        application=qt_app,
        icon=window.windowIcon(),
        on_exit=lambda: exits.append("exit"),
    )
    try:
        assert controller.install()
        tray = controller.tray_icon
        assert tray is not None
        assert tray.icon().cacheKey() == window.windowIcon().cacheKey() == qt_app.windowIcon().cacheKey()
        assert controller.intercept_close()
        qt_app.processEvents()
        assert not window.isVisible()
        controller.restore()
        qt_app.processEvents()
        assert window.isVisible()
        controller.request_exit()
        assert exits == ["exit"]
    finally:
        controller.dispose()
        window.hide()
        window.deleteLater()
        qt_app.processEvents()
