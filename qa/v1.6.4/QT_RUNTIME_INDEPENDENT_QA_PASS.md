# RangeScout 1.6.4 Qt Runtime Hotfix — Final Independent QA

## Verdict

**PASS**

The prior artifact-intake HOLD is superseded. Independent QA obtained the exact immutable Engineering QA candidate, verified its bytes and package evidence, and found no release-blocking defect in the 1.6.4 Qt runtime correction.

One LOW, non-product documentation defect remains: `README_FIRST.md` inside the internal QA ZIP contains a malformed/truncated source-tree SHA. The exact source identity is independently and correctly bound by GitHub branch/commit/tree plus the adjacent `QA_ARTIFACT_MANIFEST.json`; the typo does not affect installer/runtime bytes and does not require rebuilding the candidate.

## QA timing

- Initial intake/start: `2026-09-01T08:46:00-04:00` EDT
- Initial artifact-intake HOLD: `2026-09-01T08:50:44.993598-04:00` EDT
- Exact-byte artifact phase resumed: `2026-09-01T09:20:12-04:00` EDT
- Final decision freeze/stop: `2026-09-01T09:28:44.612426-04:00` EDT
- Total wall elapsed from initial intake: `00h42m44.612s`
- Final exact-byte phase elapsed: `00h08m32.612s`

## Exact Engineering source identity

- Repository: `dietrichailabs-oss/RangeScout`
- Engineering branch: `codex/v1.6.4-qt-runtime-hotfix`
- Engineering commit: `474fd4299813bf29e0206fc5990516ff49052898`
- Engineering tree: `b2122885e1fc02f61001e9d6819d463fa0c4c303`
- Build: `rs-v1.6.4-qt-runtime-hotfix-eng1`
- Base: `63b0b4a0131845dc92ba351e6459a2caec405011`

Independent QA verified the remote Engineering branch points to the exact commit/tree above and is two commits ahead of the 1.6.3 `main` base with no divergence.

## Exact QA candidate

- Artifact: `RangeScout_1.6.4_Engineering_QA_Candidate.zip`
- Size: `45,700,114 bytes`
- SHA-256: `57404095AD9813003A6F80BE5B6436A8E2371026FA12632D8EB8954ED7080702`

Temporary immutable intake branch:

- `qa-artifacts/v1.6.4-qt-runtime-eng1`
- artifact commit: `dfcbd03e73e61b5f493f972e21402411bb0f13a7`
- Git blob: `c3b98d1986e98b38b847aed981d1e6e4ccbf5495`

`QA_ARTIFACT_MANIFEST.json` binds the exact candidate ZIP SHA/size to the correct Engineering branch, commit, tree, build, version, and Issue #4.

Because the normal GitHub connector would not stream the 45.7 MB binary, Independent QA used a separate temporary Actions export branch created from the immutable artifact commit. The workflow verified the exact inner ZIP size/SHA before uploading it. No rebuild or recompression of the candidate itself occurred.

Actions transfer evidence:

- temporary export branch: `qa-artifacts/v1.6.4-actions-export`
- workflow commit: `6c2b99a11e0148424fbb8cb067d12eb6311ec631`
- workflow run: `33512762895`
- conclusion: `success`
- Actions artifact ID: `9802310678`
- Actions wrapper digest: `DDA337EC01D8DD8FF84FBDEFA53501251617029FC282E3EC788578E13E6C4EB2`
- inner candidate re-hash by QA: exact `57404095AD9813003A6F80BE5B6436A8E2371026FA12632D8EB8954ED7080702`

## Archive integrity

Independent QA verified the exact candidate ZIP:

- ZIP CRC: PASS
- entries: exactly 4
- path traversal: none
- duplicate paths: none
- case-colliding paths: none
- archive symlinks: none

Entries:

1. `RangeScout_1.6.4_Setup.exe` — `46,160,959 bytes`
2. `QT_RUNTIME_AUDIT.json` — `3,850 bytes`
3. `FINAL_VM_LIFECYCLE.json` — `244,660 bytes`
4. `README_FIRST.md` — `634 bytes`

## Exact installer identity

- Installer: `RangeScout_1.6.4_Setup.exe`
- Size: `46,160,959 bytes`
- SHA-256: `32F3D0E6BBE825523DDA38F09E775B561528127DB7C6766EAF252328D5328624`

Independent PE/string inspection was consistent with expected Inno Setup metadata including `Dietrich AI Labs`, `RangeScout 1.6.4 Setup`, and version `1.6.4.0`.

## Root cause independently reviewed

Engineering reproduced the public 1.6.3 failure and identified package-local `_internal/icuuc.dll` as the Windows DLL-search-order collision.

- PyInstaller-bundled ICU file version: `78.3`
- Shadow DLL SHA-256: `2882AFACABD9D901762AB196C7A319B03051AF39291E47FEF51FCB69C26AB5B8`
- `Qt6Core.dll` required the Windows ICU compatibility export `UCNV_TO_U_CALLBACK_SUBSTITUTE`.
- The package-local ICU library did not provide the expected compatibility export.
- Windows selected the package-local DLL first, causing `QtCore.pyd` to fail with `The specified procedure could not be found.`

The diagnosis matches the field symptom and the supplied before/after evidence.

## Source/package hardening review

Independent source review verified:

1. Release builds enforce CPython `3.14.6` x64.
2. PySide6, Addons, Essentials, and shiboken6 are pinned to `6.11.1`.
3. PyInstaller is pinned to `6.21.0`.
4. `pyinstaller-hooks-contrib` is pinned to `2026.6`.
5. `icuuc.dll` is explicitly treated as a prohibited Qt shadow library.
6. Packaging records the shadow DLL path, size, hash and file version before removal.
7. Packaging fails if the expected shadow DLL is unexpectedly absent, exposing dependency-collection drift instead of silently changing behavior.
8. Packaging fails if the prohibited DLL remains after removal.
9. Critical Qt/Python runtime PE files are required to be AMD64.
10. Duplicate/conflicting `Qt6Core.dll` or `QtCore*.pyd` locations are rejected.
11. The final packaged EXE is executed as a QtCore / QtWidgets / QtWebSockets smoke gate.
12. The installer deletes the prior `{app}\_internal` tree before copying the new runtime, preventing stale 1.6.3 Qt/ICU files from surviving an upgrade.

## Exact Qt runtime audit

`QT_RUNTIME_AUDIT.json` reports build identity `rs-v1.6.4-qt-runtime-hotfix-eng1`, version `1.6.4`, status `PASS`, and the exact pinned build environment.

Critical rows independently checked against the exact clean-VM inventory include:

- `RangeScout.exe` — `3,325,515 bytes` — `C364F21176F34B0ED510A95CE1F4C468396506FA9F464E182579B87F0EA3C9CE`
- `Qt6Core.dll` — `65FE6224B6C47A15B058738031D31DCE9928C4D7A58E1B8DB6434F6F5CDDD702`
- `QtCore.pyd` — `BE52341A5DF1F76ECCA2FB1E94EB429DFA46F0B659ED6536F25119B565B21EA8`
- `Qt6Widgets.dll` — `5A9F37DEDD3DC5BCC1A4BB8EA919C49FC62DF751A1450ACCC34379EB6710EAB6`
- `Qt6WebSockets.dll` — `FC8F98B96BAF70CFA1501BCD43DAF3C50507B8409BEC62AA375A05E093648E72`
- `qwindows.dll` — `54D736DE022F707E9F7F555E4C9F9E993253CD5B0EE2364E6A9458C180828C42`

Audit invariants:

- exactly one `_internal/PySide6/Qt6Core.dll`
- exactly one `_internal/PySide6/QtCore.pyd`
- all critical runtime PE rows AMD64
- prohibited `icuuc.dll` remaining count: `0`
- packaged import/launch smoke exit code: `0`
- QtCore: imported during packaged startup
- QtWidgets: imported during packaged startup
- QtWebSockets: imported through `app.streaming.qt_transport`

The audit records removal of the exact bad `icuuc.dll` with SHA `2882AFACABD9D901762AB196C7A319B03051AF39291E47FEF51FCB69C26AB5B8`.

## Clean Windows lifecycle evidence

`FINAL_VM_LIFECYCLE.json` status: `PASS`.

Environment:

- Microsoft Windows 11 Pro
- version/build: `10.0.26200` / `26200`
- x64
- no PySide6 installation
- no Qt SDK/qmake
- no Engineering build environment
- `python.exe` only represented by the unprovisioned WindowsApps alias

### Fresh 1.6.4 install

- candidate installer size/SHA in VM evidence exactly matches the independently extracted installer
- installer exit: `0`
- installed package-local `icuuc.dll`: `0`
- installed runtime inventory: `264 files`
- launch: PASS
- launched EXE size: `3,325,515 bytes`
- launched EXE SHA-256: `C364F21176F34B0ED510A95CE1F4C468396506FA9F464E182579B87F0EA3C9CE`
- main window: `RangeScout`

### Public 1.6.3 control and 1.6.3 -> 1.6.4 upgrade

Public 1.6.3 control installer:

- SHA-256: `11271C0F14D22C732455E8C6B99DFC80539A6B11D7A7E8CAC54180653FD22FD7`
- package-local `icuuc.dll` before upgrade: `1`

Upgrade with exact 1.6.4 candidate:

- installer exit: `0`
- package-local `icuuc.dll` after upgrade: `0`
- runtime inventory: `264 files`
- clean-install vs upgraded runtime inventory differences: `0`
- exact runtime match: `true`
- `%AppData%\RangeScout` preservation sentinel: `true`
- launch: PASS
- upgraded EXE size/SHA exactly matches fresh install and runtime audit

Independent QA cross-checked every critical runtime audit row against the VM inventory and found matching paths, sizes, and SHA-256 values. Fresh and upgraded inventories have no duplicate paths/case collisions and are byte-identical.

## Regression evidence

Engineering records reviewed:

- focused Qt/package/company/release tests: `36 passed`
- existing 1.6.3 watchlist regression: `8 passed`
- final full Windows regression: `824 passed`, `10 subtests passed`, `0 failures`, `3 existing deprecation warnings`

No unrelated product change was identified in the 1.6.3-base to 1.6.4-candidate scope.

## Non-blocking finding — QA metadata typo

**Finding:** `RS-164-QA-DOC-001`

**Severity:** LOW / internal evidence metadata only

`README_FIRST.md` inside the QA ZIP records the source tree as:

`b2122885e1fc02f61001e9d6819d463fa0c4b69`

That value is malformed (39 hex characters) and does not equal the actual Engineering tree.

Correct tree:

`b2122885e1fc02f61001e9d6819d463fa0c4c303`

Why this does not block PASS:

- the GitHub Engineering branch/commit/tree binding was independently verified;
- Issue #4 records the correct tree;
- `QA_ARTIFACT_MANIFEST.json` records the correct tree and binds it to the exact QA ZIP size/SHA;
- the installer/runtime bytes and clean-VM lifecycle evidence are unaffected;
- this internal QA README is not a public runtime dependency.

Do not rebuild/repackage solely to correct this note. Correct the typo in future archival/generated handoff metadata.

## Final decision

**Independent QA: PASS**

The exact RangeScout 1.6.4 Qt runtime hotfix candidate corrects the public 1.6.3 pre-UI QtCore loader failure in the reviewed scope. Fresh clean-Windows installation and public 1.6.3-to-1.6.4 upgrade are both bound to the exact candidate installer and pass with zero package-local `icuuc.dll`, matching clean/upgrade runtime inventories, preserved user AppData, and successful UI launch.

The prior artifact-intake HOLD is superseded by this PASS.

No merge, tag, or publication is performed by this QA record. Issue #4 should remain open until normal post-QA release housekeeping is completed.

## Who gets what next

### Engineering / release integration

Receives:

- Engineering commit `474fd4299813bf29e0206fc5990516ff49052898`
- tree `b2122885e1fc02f61001e9d6819d463fa0c4c303`
- exact QA candidate ZIP SHA `57404095AD9813003A6F80BE5B6436A8E2371026FA12632D8EB8954ED7080702`
- exact installer SHA `32F3D0E6BBE825523DDA38F09E775B561528127DB7C6766EAF252328D5328624`
- this Independent QA PASS record

Purpose: perform only explicitly authorized post-QA integration/release work using the exact approved source/candidate identity.

Authority: Engineering may integrate only after user authorization; Engineering may not change the approved product/package bytes and still inherit this PASS.

Blocked state: any rebuilt or byte-changed installer/package requires new artifact identity and QA re-verification.
