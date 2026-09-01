# RangeScout 1.6.4 Final Public Wrapper — Independent QA

## Verdict

**PASS — authorized for publication as the RangeScout 1.6.4 public Windows wrapper.**

This record is limited to the final public wrapper assembled around the already QA-passed 1.6.4 installer/runtime. No installer or application rebuild occurred during this wrapper phase.

## QA timing

- Final wrapper intake/start: `2026-09-01T10:14:35-04:00` EDT
- Final decision freeze/stop: `2026-09-01T10:16:44-04:00` EDT
- Active elapsed: `00h02m09s`

## Final integrated source identity

- Repository: `dietrichailabs-oss/RangeScout`
- Final main commit / proposed `v1.6.4` target: `e1778540a14b7beb1a5ec3ec5706964dca606bec`
- Final main tree: `980bb6c89549426051e67c60870619a5e78fc483`
- Parent: `ab23473f7a85d82f62e466c0692b0eacd61dcc04`
- Frozen Engineering commit: `474fd4299813bf29e0206fc5990516ff49052898`
- Frozen Engineering tree: `b2122885e1fc02f61001e9d6819d463fa0c4c303`
- Build: `rs-v1.6.4-qt-runtime-hotfix-eng1`

Independent QA verified the final integration adds only README release-documentation commits after the exact QA-passed Engineering source; no application/runtime source changes were introduced after the frozen Engineering commit.

## Exact final public wrapper

- Artifact: `RangeScout_1.6.4_Windows.zip`
- Size: `45,682,927 bytes`
- SHA-256: `B87775E0DB4B0C684930AE9011DB961EAA3643EC5F4D751FDF0B1D51E038F03F`

Temporary wrapper manifest branch:

- `qa-artifacts/v1.6.4-public-wrapper`
- Manifest binds final main commit/tree, wrapper size/SHA, installer size/SHA, and exact four-member file set.

GitHub Actions transfer used for independent byte retrieval:

- Run ID: `33517830728`
- Artifact ID: `9804342922`
- Actions outer transfer ZIP size: `45,683,097 bytes`
- Actions outer transfer ZIP SHA-256: `0A1536A3B38D346403765EF3AB4242F6869B473F630404FF121EBD629ED0B4AE`
- The Actions outer ZIP contained exactly one inner file: `RangeScout_1.6.4_Windows.zip`, size `45,682,927 bytes`.
- Independent extraction and SHA-256 of that inner public wrapper matched `B87775E0DB4B0C684930AE9011DB961EAA3643EC5F4D751FDF0B1D51E038F03F` exactly.

## Archive integrity and member verification

Independent QA ran ZIP CRC/integrity validation with no bad member and found no absolute paths, drive-qualified paths, or `..` traversal entries.

Exact member set:

1. `RangeScout_1.6.4_Setup.exe`
2. `README.md`
3. `SIGNING_INFORMATION.md`
4. `SIGNING_VERIFICATION.txt`

No QA evidence, worktree files, source archives, development artifacts, temporary transfer files, or extra package material are present in the public wrapper.

## Embedded installer verification

- `RangeScout_1.6.4_Setup.exe`
- Size: `46,160,959 bytes`
- SHA-256: `32F3D0E6BBE825523DDA38F09E775B561528127DB7C6766EAF252328D5328624`

This matches the exact installer that passed the prior 1.6.4 Qt runtime Independent QA gate. The installer was not rebuilt for the public wrapper.

## Embedded documentation verification

`README.md` inside the wrapper:

- Size: `36,453 bytes`
- SHA-256: `1C1BD87BD881DCD236614128F11A8E48AA96F10CA932A4FA8686037F3EC9EF50`
- identifies `RangeScout 1.6.4` as the current public release;
- points the primary download to `RangeScout_1.6.4_Windows.zip`;
- contains the corrected historical 1.6.3 wording: 1.6.3 **was published** and is preserved as prior release history;
- no longer contains the stale phrase that 1.6.3 "is now the current public release".

`SIGNING_INFORMATION.md` and `SIGNING_VERIFICATION.txt` consistently identify RangeScout 1.6.4, disclose that the installer is not Authenticode/publicly trusted-signed, and record the correct Engineering commit/tree/build and installer SHA-256. No cryptographic-signature claim is made from publisher metadata.

## Prior runtime QA binding

The exact embedded installer is already covered by the final 1.6.4 Qt runtime QA PASS, including:

- clean Windows 11 x64 fresh install and UI launch PASS;
- no package-local `icuuc.dll` in corrected runtime;
- public 1.6.3 -> 1.6.4 upgrade PASS;
- stale 1.6.3 ICU shadow DLL removed during upgrade;
- clean/upgraded runtime inventories identical;
- user AppData preserved;
- 1.6.3 watchlist regression preserved;
- full relevant regression green.

## Decision

**Independent QA: PASS.**

The exact wrapper `RangeScout_1.6.4_Windows.zip`, size `45,682,927`, SHA-256 `B87775E0DB4B0C684930AE9011DB961EAA3643EC5F4D751FDF0B1D51E038F03F`, is authorized for publication under tag `v1.6.4` targeting `e1778540a14b7beb1a5ec3ec5706964dca606bec`.

Publication must upload these exact wrapper bytes. If the uploaded asset size or SHA-256 differs, this authorization does not apply and publication verification must stop.

Issue #4 should remain open until the public GitHub release is uploaded and independently re-read to confirm tag target, asset filename, size, SHA-256, release-note hashes, and download identity.