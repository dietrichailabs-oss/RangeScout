# RangeScout 1.4.0 system-tray Engineering checkpoint

- Base: QA-passed RangeScout 1.4.0 company-logo candidate `rs-v1.4.0-company-logos-eng2`.
- New build identity: `rs-v1.4.0-company-logos-tray-eng1`.
- User close behavior: title-bar X / Alt+F4 hides the visible main window when a usable system tray is present.
- Explicit termination: tray menu **Exit RangeScout** shuts down runtime services and quits Qt.
- Restore behavior: tray **Open RangeScout**, single click, and double click show, raise, and activate the main window.
- Fail-safe: when the system tray is unavailable, close proceeds normally.
- Icon identity: `resources/rangescout.ico` is used by Qt, QSystemTrayIcon, PyInstaller, Inno Setup, taskbar, and shortcuts.
- Alert behavior: existing desktop alert notifications reuse the single persistent tray icon; no duplicate tray icon is created.
- Automation behavior: timed auto-close and UI evidence capture call explicit application exit so release tooling cannot hang in the tray.
- Packaged-runtime proof: `scripts/handoff/tray_runtime_evidence.py` sends a real WM_CLOSE to the exact EXE, verifies the process survives, and verifies the visible main window is hidden before cleanup.
- Route: new immutable build requires Independent QA; prior QA PASS does not automatically extend to these changed bytes.
