# RangeScout 1.6.2 R18 — Independent QA Report

## Verdict

**CONDITIONAL PASS — FUNCTIONAL/SOURCE/PACKAGE QA PASSED; NOT YET UNCONDITIONALLY RELEASE-READY**

R18 closes the R17 SEC-frame blocker. Independent QA found no new release-blocking source or package defect in the exact R18 candidate. The remaining condition is destructive native Windows installer lifecycle validation (fresh install / upgrade / uninstall and cleanup) on a disposable Windows target. That gate was not executed in this QA runtime and must not be fabricated.

## QA timing

- Recorded intake start: 2026-08-28 18:28 EDT (conversation clock resolution was one minute; seconds were not exposed)
- Decision freeze: 2026-08-28 18:39:27.402 EDT
- Elapsed from recorded intake minute: approximately 00h11m27.402s

## Exact artifact reviewed

- Intended name: `RangeScout_1.6.2_R18_Engineering_Master_QA_Handoff.zip`
- Uploaded opaque name: `a300e06e-6626-45da-b751-e57d283b8b2e.zip`
- Size: **90,282,168 bytes**
- SHA-256: `E2154557D23CE896065AE3EF47A231353B56BADBA1B0344276B5DB3958EDC50D`
- Version: `1.6.2`
- Build: `rs-v1.6.2-sec-best-align-r18`

## GitHub provenance — PASS

- Repository: `dietrichailabs-oss/RangeScout`
- Engineering branch: `codex/v1.6.2-r18-sec-best-alignment`
- Commit: `cada372574ff12dcc285912587de364b860f1fda`
- Tree: `e539e08ef77433a2ecbec2bd8406fe77dbc1efae`
- Remote commit independently fetched: PASS
- Five Engineering-declared changed source/test files in the exact Source ZIP bind to the corresponding remote Git blobs after CRLF→LF normalization: PASS
- Frozen R17 Engineering commit remained unchanged.

## Package integrity — PASS

- Outer ZIP: 44 entries; CRC clean
- Traversal / symlink / duplicate-name / case-collision checks: PASS
- `CONTENT_MANIFEST.json`: **43/43 entries independently verified**
- Installer: 36,932,361 bytes; SHA-256 `5B0A5822ED09004405BA0BEEA6C5473BF77E283DDA7BBBF622D07C15B888D52D`
- Portable ZIP: 50,921,675 bytes; SHA-256 `390BD424C0BCB245E13390A93E98AFBF641B850955D17903AE8102E74D2B3295`
- Source ZIP: 13,525,761 bytes; SHA-256 `FBA16A51A2F0FB474D87C3E4A39F858491C134C0F1FA3FB2ADD116272A0EE078`
- Portable `RangeScout.exe`: 3,323,566 bytes; SHA-256 `FE4E4C06C47C3FD8E616438355DE4301C95F526D8C4B5A906E0E6541E2D9EAF5`; PE `MZ` header confirmed

## R18 SEC best-alignment correction — PASS

R18 removes the R17 fixed ±14-day nominal-boundary rule and replaces it with deterministic best-alignment logic using both fact start and end dates against the supplied calendar period and adjacent periods. The supplied frame must be the unique best compatible period; ties fail closed. Frame-only fallback retains SEC-style duration bands, while a complete fiscal `fy` + `fp` identity remains atomic and authoritative for fiscal comparison.

Independent QA confirmed the previously blocking classes now behave correctly, including:

- off-calendar fiscal Q1 ending April 30;
- off-calendar annual periods ending January 31;
- July 4 / January 3 52/53-week boundary cases;
- frame-only coherent calendar fallback;
- partial fiscal identity fail-closed behavior;
- wrong adjacent-period decoys;
- malformed/semantic-mismatch frames;
- row-order-independent comparisons.

The implementation is consistent with SEC Frames API documentation describing facts that most closely fit / best align to calendrical periods rather than requiring literal calendar-boundary equality.

## Independent automated testing

Completed independent runnable source/regression assertions before decision freeze:

- R18 + R17 + R16 SEC suites: **65 passed**
- R15 + SEC core + R14: **64 passed**
- R13/R12/R11/R10 accumulated regression: **77 passed**
- Release/package/artifact slice: **12 passed / 3 skipped**
- Persistence / Active Symbol / watchlist / integration slice: **12 passed / 1 skipped**
- Runnable system-tray slice: **7 passed / 3 skipped**

Total completed runnable assertions: **237 passed / 0 product assertion failures / 7 skipped**.

Environment-limited checks were not promoted to product failures:

1. A clean-source installer-safety test expects an already-generated `release/dist/RangeScout-1.6.2-windows` tree, which is intentionally absent from the clean Source ZIP.
2. Some UI/Windows tests require `PySide6`, which is not installed in this QA container. Their collection/import failure is an environment dependency, not a RangeScout assertion failure.

Engineering's reported full-repository and exact-source results remain supporting evidence, not substituted for Independent QA.

## Independent fresh 50-instrument sweep — PASS

Independent QA used a fresh 50-case sample excluded from Engineering's R18 120-case list across equity, preferred, ADR, ETF, CEF, warrant, right, unit, ETN and index categories.

Acceptance checks covered exact ticker ranking, official-name discovery, meaningful partial-name discovery, Research routing, capability state validity, Active Symbol identity propagation, and stale-result rejection after symbol changes.

Result: **50 passed / 0 failed**.

## Remaining condition

### Native Windows installer lifecycle — PENDING

Independent QA did not execute destructive install/upgrade/uninstall against a disposable Windows system. The user's real installation was not touched.

Before unconditional release approval, the exact R18 installer must be validated on a disposable Windows target for at minimum:

- clean install;
- launch of the installed application;
- upgrade from the supported previous installation state;
- preservation/migration of intended user data;
- uninstall;
- removal/preservation behavior exactly matching policy;
- shortcuts/start-menu/install-location behavior;
- no unexpected residue, privilege escalation, or deletion outside the product-owned scope.

If that exact installer passes, no R19 source correction is required. If it fails, Engineering must branch from the exact frozen R18 commit and correct only the demonstrated installer defect.

## Known non-blocker

The installer and packaged executable are unsigned. R18 explicitly records Authenticode as `NotSigned` and signing was not requested for this gate; this report does not convert that into a newly invented blocker.

## Routing — who gets what next

### Next recipient: Native Windows final-gate QA

Receives:
- exact R18 installer from the immutable R18 handoff;
- R18 candidate identity / hashes;
- this Independent QA CONDITIONAL PASS record.

Authority:
- validate the destructive installer lifecycle condition only;
- return PASS / HOLD / FAIL for that condition without modifying the frozen R18 source.

### Engineering / Codex

No R19 code changes are authorized by this report. Keep commit `cada372574ff12dcc285912587de364b860f1fda` frozen. Engineering acts again only if the Windows lifecycle gate demonstrates a reproducible defect.
