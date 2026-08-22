from __future__ import annotations

from pathlib import Path

from app.ui import runner


def test_stable_windows_app_user_model_id_and_safe_platform_behavior() -> None:
    calls: list[str] = []
    assert runner.WINDOWS_APP_USER_MODEL_ID == "DietrichAILabs.RangeScout"
    assert runner.set_windows_app_user_model_id(platform_name="win32", setter=lambda value: calls.append(value) or 0)
    assert calls == ["DietrichAILabs.RangeScout"]
    assert not runner.set_windows_app_user_model_id(platform_name="linux", setter=lambda value: calls.append(value) or 0)
    assert calls == ["DietrichAILabs.RangeScout"]
    assert not runner.set_windows_app_user_model_id(platform_name="win32", setter=lambda value: (_ for _ in ()).throw(OSError("unavailable")))


def test_windows_identity_is_set_before_qapplication_and_qt_identity_is_consistent(monkeypatch) -> None:
    events: list[str] = []

    class FakeApplication:
        def __init__(self, arguments):
            events.append("QApplication")
            self.arguments = arguments
            self.application_name = ""
            self.organization_name = ""
            self.display_name = ""

        def setApplicationName(self, value):
            self.application_name = value

        def setOrganizationName(self, value):
            self.organization_name = value

        def setApplicationDisplayName(self, value):
            self.display_name = value

    monkeypatch.setattr(runner, "set_windows_app_user_model_id", lambda: events.append("AppUserModelID") or True)
    monkeypatch.setattr(runner, "QApplication", FakeApplication)
    app = runner.create_qt_application(["RangeScout.exe"])
    assert events == ["AppUserModelID", "QApplication"]
    assert app.application_name == "RangeScout"
    assert app.organization_name == "Dietrich AI Labs"
    assert app.display_name == "RangeScout"


def test_production_ui_sources_do_not_spawn_primary_screen_processes_or_extra_applications() -> None:
    source_root = Path(__file__).resolve().parents[2] / "app" / "ui"
    runner_source = (source_root / "runner.py").read_text(encoding="utf-8")
    main_source = (source_root / "main.py").read_text(encoding="utf-8")
    combined = runner_source + main_source
    assert "subprocess" not in combined
    assert "QProcess" not in combined
    assert runner_source.count("QApplication(arguments)") == 1


def test_qt_application_uses_the_shared_rangescout_icon(monkeypatch) -> None:
    icon = object()

    class FakeApplication:
        def __init__(self, arguments):
            self.arguments = arguments
            self.icon = None

        def setApplicationName(self, value):
            self.application_name = value

        def setOrganizationName(self, value):
            self.organization_name = value

        def setApplicationDisplayName(self, value):
            self.display_name = value

        def setWindowIcon(self, value):
            self.icon = value

    monkeypatch.setattr(runner, "set_windows_app_user_model_id", lambda: True)
    monkeypatch.setattr(runner, "QApplication", FakeApplication)
    monkeypatch.setattr(runner, "load_application_icon", lambda: icon)
    app = runner.create_qt_application(["RangeScout.exe"])
    assert app.icon is icon
