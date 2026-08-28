# RangeScout 1.6.2 R18 — Final Independent QA Decision

## Verdict

**PASS — RELEASE GATE CLOSED**

R18 previously held a CONDITIONAL PASS pending destructive native Windows installer lifecycle validation. The final evidence bundle was independently reviewed and that condition is now closed.

No R19 source correction is required or authorized by this result.

## QA review timing

- Installer VM run: 2026-08-28T19:21:34.7074969-04:00 to 2026-08-28T19:23:31.8910667-04:00
- Installer run elapsed: 117.184 seconds
- Final evidence review intake: 2026-08-28 19:32 EDT (minute precision supplied by conversation clock)
- Final decision freeze: 2026-08-28T19:35:09.362749-04:00

## Exact frozen candidate

- Repository: `dietrichailabs-oss/RangeScout`
- Engineering branch: `codex/v1.6.2-r18-sec-best-alignment`
- Engineering commit: `cada372574ff12dcc285912587de364b860f1fda`
- Engineering tree: `e539e08ef77433a2ecbec2bd8406fe77dbc1efae`
- Build: `rs-v1.6.2-sec-best-alignment-r18`
- Engineering handoff: `RangeScout_1.6.2_R18_Engineering_Master_QA_Handoff.zip`
- Engineering handoff size: 90,282,168 bytes
- Engineering handoff SHA-256: `E2154557D23CE896065AE3EF47A231353B56BADBA1B0344276B5DB3958EDC50D`

## Exact installer validated

- `RangeScout_1.6.2_Setup.exe`
- 36,932,361 bytes
- SHA-256 `5B0A5822ED09004405BA0BEEA6C5473BF77E283DDA7BBBF622D07C15B888D52D`
- Installed `RangeScout.exe` SHA-256 `FE4E4C06C47C3FD8E616438355DE4301C95F526D8C4B5A906E0E6541E2D9EAF5`

## Installer lifecycle — PASS

The exact installer completed the lifecycle on a disposable Windows Sandbox target (Windows Enterprise 24H2, build 26100, AMD64):

1. exact installer filename/size/hash verified before execution;
2. clean current-user install exited 0;
3. install location, 219 installed files, shortcut, uninstall entry and product registry were recorded;
4. installed executable reported file/product version 1.6.2 and registry build identity `rs-v1.6.2-sec-best-alignment-r18`;
5. packaged R18 launched successfully through the AAPL global-search smoke path and exited cleanly;
6. clean uninstall exited 0 and removed RangeScout-owned install files, shortcut, uninstall entry and product registry while preserving user AppData;
7. exact supported RangeScout 1.6.1 installer was installed and launched;
8. in-place 1.6.1 -> exact R18 upgrade exited 0;
9. install location remained stable and the R18 executable replaced the prior runtime;
10. `settings.json` remained byte-identical across the installer upgrade;
11. a user-data sentinel remained byte-identical and dark theme remained configured after R18 launch;
12. final R18 uninstall exited 0;
13. Inno Setup reported `Uninstallation process succeeded`, `Removed all? Yes`, and `Need to restart Windows? No`;
14. outside-scope and user-export sentinels remained byte-identical after uninstall;
15. no source or binary was modified for this gate and no R19 was created.

## Evidence-bundle review

- Uploaded evidence bundle: `04aa9ae0-5fda-43ed-90b7-7dcd0846bc00.zip`
- Size: 7,461,031 bytes
- SHA-256: `A5B7D98026757E1930D807FBB13AD52BB21D8D8BABDC662131629B973BB41103`
- Entries: 36
- CRC: PASS
- Path traversal: none
- Symlinks: none
- Duplicate names: none
- Case collisions: none
- `EVIDENCE_MANIFEST.json`: 35/35 payload entries independently size/hash verified

The evidence preserves two earlier inconclusive harness attempts. Run 1 stopped before installer execution due to a Windows Sandbox CIM permission error. Run 2 installed R18 but incorrectly assumed the launcher process owned the GUI. Run 3 changed only the harness to detect the actual `RangeScout` GUI process and then completed the entire lifecycle. This is acceptable because the application source and installer were unchanged.

Three packaged-application screenshots were independently inspected: clean R18 Market/AAPL, prior 1.6.1 Market/AAPL, and upgraded R18 Settings/AAPL with dark theme preserved.

## Prior QA state incorporated

The R18 source/package candidate already held Independent QA CONDITIONAL PASS with:

- 237 runnable independent assertions passed / 0 product failures / 7 skipped;
- fresh Independent QA 50-instrument sample: 50/50 PASS;
- package/content manifest integrity PASS;
- exact Source ZIP to remote Git blob binding PASS;
- R17 SEC-frame blocker closed by R18.

The native installer condition was the sole remaining release gate. It is now PASS.

## Final routing

- SEC/fiscal-frame blocker: CLOSED
- Native Windows installer lifecycle: CLOSED
- R19 source cycle: NOT REQUIRED
- Exact R18 candidate remains frozen.

**Final Independent QA verdict: PASS.**