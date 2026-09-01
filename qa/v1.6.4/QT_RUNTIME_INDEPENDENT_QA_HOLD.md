# RangeScout 1.6.4 Qt Runtime Hotfix — Independent QA

## Verdict

**HOLD — source correction passes review; exact binary candidate is not independently retrievable yet.**

This HOLD is an artifact-intake hold only. Independent QA found no source-level blocker in the 1.6.4 Qt runtime correction. Engineering should not change source solely because of this verdict.

## Exact Engineering candidate

- Repository: `dietrichailabs-oss/RangeScout`
- Branch: `codex/v1.6.4-qt-runtime-hotfix`
- Commit: `474fd4299813bf29e0206fc5990516ff49052898`
- Tree: `b2122885e1fc02f61001e9d6819d463fa0c4c303`
- Build: `rs-v1.6.4-qt-runtime-hotfix-eng1`
- Base: `63b0b4a0131845dc92ba351e6459a2caec405011`

Remote branch/commit/tree binding was independently verified. The candidate is two commits ahead of the 1.6.3 `main` base with no divergence.

## QA timing

- Intake/start: `2026-09-01T08:46:00-04:00` (conversation clock minute precision)
- Decision freeze: `2026-09-01T08:50:44.993598-04:00`
- Elapsed from recorded intake minute: approximately `00h04m45s`

## Engineering root cause reviewed

Engineering reproduced the public 1.6.3 startup failure on clean Windows and traced it to package-local `_internal/icuuc.dll` shadowing the Windows ICU compatibility DLL. `Qt6Core.dll` required `UCNV_TO_U_CALLBACK_SUBSTITUTE`, while the bundled ICU 78.3 shadow DLL did not export the expected compatibility procedure. Removing only the package-local shadow DLL allowed the otherwise unchanged runtime to launch.

Reported public 1.6.3 shadow DLL SHA-256:
`2882AFACABD9D901762AB196C7A319B03051AF39291E47FEF51FCB69C26AB5B8`

The diagnosis is technically coherent with the observed Windows loader error: `The specified procedure could not be found`.

## Independent source review

Independent QA verified the exact pushed source implements the required hardening:

1. Windows release environment is pinned and enforced to CPython 3.14.6 x64, PySide6 / Addons / Essentials / shiboken6 6.11.1, PyInstaller 6.21.0, and pyinstaller-hooks-contrib 2026.6.
2. Release packaging explicitly treats `icuuc.dll` as a prohibited Qt shadow library.
3. Packaging records its size/hash/file version before removal.
4. Packaging fails if the expected shadow DLL is not observed, so changed dependency collection cannot silently bypass this workaround.
5. Packaging fails if the prohibited shadow DLL remains after removal.
6. The final packaged runtime is audited for AMD64 architecture across critical Qt/Python components.
7. Duplicate/conflicting `Qt6Core.dll` and `QtCore*.pyd` locations are rejected.
8. The final packaged `RangeScout.exe` is launched as a QtCore/QtWidgets/QtWebSockets smoke gate.
9. The Inno Setup upgrade path deletes the prior `{app}\_internal` tree before copying the new runtime, preventing the stale 1.6.3 `icuuc.dll` from surviving an upgrade overlay.
10. 1.6.4 version/build identity changes are limited to expected release metadata plus package hardening and regression tests; no unrelated SEC, provider-routing, database, or watchlist redesign was found in the base-to-candidate file set.

## Engineering evidence reviewed

Issue #4 reports:

- focused tests: `36 passed`
- 1.6.3 watchlist regression: `8 passed`
- full regression: `824 passed`, 10 subtests passed, 0 failures, 3 existing deprecation warnings
- clean Windows 11 Pro x64 build 26200 fresh install: PASS
- packaged `RangeScout.exe` UI launch: PASS
- public 1.6.3 control contains one package-local `icuuc.dll`
- 1.6.3 -> 1.6.4 upgrade: PASS
- upgraded 1.6.4 package-local `icuuc.dll` count: 0
- clean and upgrade runtime inventories: identical
- `%AppData%\RangeScout` preservation sentinel: preserved

Reported candidate identities:

- `RangeScout.exe`: 3,325,515 bytes; SHA-256 `C364F21176F34B0ED510A95CE1F4C468396506FA9F464E182579B87F0EA3C9CE`
- `RangeScout_1.6.4_Setup.exe`: 46,160,959 bytes; SHA-256 `32F3D0E6BBE825523DDA38F09E775B561528127DB7C6766EAF252328D5328624`
- minimal QA ZIP: 45,700,114 bytes; SHA-256 `57404095AD9813003A6F80BE5B6436A8E2371026FA12632D8EB8954ED7080702`

Critical runtime hashes recorded on Issue #4 include Qt6Core.dll, QtCore.pyd, Qt6Widgets.dll, Qt6WebSockets.dll, and qwindows.dll.

## HOLD reason

The exact minimal QA candidate is currently referenced only as a local Engineering path:

`G:\Marks Apps 2\RangeScout\RangeScout_1.6.4_Engineering_QA_Candidate.zip`

No GitHub release asset, Actions artifact, issue attachment, or conversation upload exposing those bytes is available to Independent QA.

Because Issue #4 is specifically a packaged-runtime DLL failure, Independent QA will not issue PASS based solely on source review and Engineering-owned VM evidence. The exact immutable binary candidate must be retrievable so QA can independently verify at minimum:

- candidate ZIP size/SHA-256
- archive integrity/path hygiene
- embedded installer size/SHA-256
- absence of package-local `icuuc.dll`
- presence and hashes of the critical Qt/PySide/Shiboken runtime files
- package/runtime manifest and audit binding
- installer/runtime identity consistency

A separate Windows VM launch may still be required if the supplied artifact/evidence does not independently bind the clean-VM run to the same exact bytes.

## Required next handoff

Provide exactly the existing minimal candidate without rebuilding it:

`RangeScout_1.6.4_Engineering_QA_Candidate.zip`

Expected size: `45,700,114` bytes

Expected SHA-256:
`57404095AD9813003A6F80BE5B6436A8E2371026FA12632D8EB8954ED7080702`

Preferred routing: upload it as a retrievable GitHub Actions artifact / non-public QA artifact, or upload the single ZIP directly for Independent QA. Do not rebuild, merge, tag, publish, or modify source merely to satisfy this HOLD.

## Decision

**Independent QA: HOLD — exact immutable binary artifact intake required.**

Issue #4 remains open. Published v1.6.3 remains unchanged. No merge or publication is authorized by this record.
