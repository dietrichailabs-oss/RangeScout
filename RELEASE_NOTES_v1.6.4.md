# RangeScout 1.6.4 Engineering Candidate

RangeScout 1.6.4 is a narrowly scoped clean-Windows Qt runtime launch hotfix.

- Windows release dependencies are exact-version locked and built in an isolated CPython 3.14.6 x64 environment.
- The package removes PyInstaller's incompatible package-local `icuuc.dll` before hashing and staging. That v78.3 DLL shadowed the Windows ICU compatibility DLL required by Qt 6.11.1 and caused `QtCore.pyd` to fail while resolving the imported `UCNV_TO_U_CALLBACK_SUBSTITUTE` procedure.
- The package gate records and verifies Python, PySide6, Shiboken, Qt, PyInstaller, PyInstaller hooks, PE architecture, duplicate-runtime state, shadow-library removal, and critical-file identity.
- The packaged executable smoke gate imports QtCore, QtWidgets, and QtWebSockets and launches the final frozen application.
- Fresh-install and public-1.6.3-upgrade validation use the final packaged executable on a disposable Windows VM.

The QA-passed RangeScout 1.6.3 watchlist behavior is unchanged. This is an Engineering candidate pending Independent QA; it is not published or approved.