# RangeScout 1.6.2 R17 — Independent QA HOLD

## Verdict

**HOLD — NOT RELEASE-READY**

Independent QA reviewed the exact R17 Engineering candidate bound to:

- Repository: `dietrichailabs-oss/RangeScout`
- Engineering branch: `codex/v1.6.2-r17-sec-frame-identity`
- Commit: `b293491ad99c9544c297ea3158b4ad5268f17356`
- Tree: `8672d8d3aa5152af950f5935c0a071254b6f8631`
- Build: `rs-v1.6.2-sec-frame-identity-r17`
- Engineering QA handoff size: `385631832` bytes
- Engineering QA handoff SHA-256: `991AE543A3F96E02ACC3A298F10684A867EFF7CDC3E4877A48A81A15E7E617A0`

QA start: `2026-08-28T14:14:29.754613-04:00`
QA decision freeze: `2026-08-28T14:24:08.925999-04:00`
Elapsed: `00h09m39.171s`

## What R17 fixed

R17 correctly fixes the two R16 blockers:

1. fiscal `fy` + fiscal-quarter `fp` are now treated as one coherent fiscal pair;
2. partial fiscal identity no longer hybridizes with SEC calendar `frame` identity;
3. both fiscal fields absent may use a separately labeled calendar fallback;
4. the July 4 Q2 and January 3 annual 52/53-week boundary reproducers pass.

These behaviors must be preserved.

## Independent positives

- GitHub remote commit fetch: PASS
- Exact Source ZIP changed-file Git blob binding: PASS
- Outer ZIP CRC/path/symlink/duplicate/case-collision checks: PASS
- CONTENT_MANIFEST: 44/44 verified
- Embedded R16 baseline exact hash/size: PASS
- Source ZIP integrity: PASS
- R17 + R16 SEC suites: 40 passed
- R15 + SEC core + R14: 64 passed
- R13/R12/R11/R10 accumulated regression: 77 passed
- Release/package/artifact slice: 12 passed / 3 skipped
- Completed Independent QA test total before decision freeze: **193 passed / 0 assertion failures / 3 skipped**

Destructive installer install/upgrade/uninstall remains pending a disposable Windows target and is not the reason for this HOLD.

# RS-R17-QA-RSCH-001 — HIGH / RELEASE BLOCKER

R17 introduced `_FRAME_BOUNDARY_TOLERANCE_DAYS = 14` and hard-rejects recognized SEC frames when the fact end date is more than 14 days from a nominal Mar 31 / Jun 30 / Sep 30 / Dec 31 boundary.

That is narrower than SEC frame semantics. SEC describes frame facts as those that most closely fit a requested calendrical period and states that frames are assembled using dates that best align with a calendar quarter or year because company financial calendars can begin/end on arbitrary dates. SEC documents annual duration as 365 days +/- 30 days and quarterly duration as 91 days +/- 30 days.

Official reference: https://www.sec.gov/search-filings/edgar-application-programming-interfaces

## Deterministic quarterly failure

Current:
- Revenue `110`
- start `2025-02-01`
- end `2025-04-30`
- `fy=2026`
- `fp=Q1`
- `frame=CY2025Q1`

Prior:
- Revenue `100`
- start `2024-02-01`
- end `2024-04-30`
- `fy=2025`
- `fp=Q1`
- `frame=CY2024Q1`

R17 with frame: Revenue and YoY growth are `NOT_AVAILABLE`.

Removing only `frame` from the same facts: Revenue `110`, YoY `10.0%`, correctly comparing fiscal Q1 FY2025 to fiscal Q1 FY2026.

## Deterministic annual failure

Current:
- Revenue `1100`
- start `2024-02-01`
- end `2025-01-31`
- `fy=2025`
- `fp=FY`
- `frame=CY2024`

Prior:
- Revenue `1000`
- start `2023-02-01`
- end `2024-01-31`
- `fy=2024`
- `fp=FY`
- `frame=CY2023`

R17 with frame: current Revenue is `NOT_AVAILABLE`.

Removing only `frame`: current Revenue is `1100 / AVAILABLE`.

# Required R18 direction

Do **not** fix this by changing 14 to another unexplained fixed tolerance.

1. When a credible complete fiscal pair exists, treat it as authoritative for fiscal comparison. A recognized frame may validate syntax/semantic class and serve as supporting calendar metadata, but must not reject an otherwise coherent fiscal fact solely because its endpoint is farther than N days from a nominal calendar boundary.
2. Preserve R17's atomic fiscal identity and fail-closed partial fiscal identity behavior.
3. When both fiscal identity components are absent and calendar frame fallback is used, replace the 14-day gate with a deterministic best-alignment model.
4. For duration facts, evaluate both start and end against the nominal frame window and adjacent candidate windows; the supplied frame should be the best compatible calendrical period. Keep quarter/annual/YTD duration validation independent.
5. For instant facts, use a deterministic nearest compatible frame boundary and fail closed on ambiguity.
6. Continue rejecting malformed frames, semantic-class mismatches, and clearly wrong adjacent period labels.

Mandatory R18 fixtures include Feb1-Apr30 Q1, May1-Jul31 Q2, Aug1-Oct31 Q3, Nov1-Jan31 cross-year quarter, Feb1-Jan31 annual, Aug1-Jul31 annual, existing July4/Jan3 cases, frame-only fallback, both partial-fiscal cases, malformed/wrong-adjacent frames, instant/YTD cases, and row-order/decoy permutations.

## Next route

**Engineering / Codex -> R18 -> Independent QA**

Create R18 from exact commit `b293491ad99c9544c297ea3158b4ad5268f17356`, push the new Engineering branch/commit/tree, freeze one immutable R18 Engineering Master QA Handoff, and return it to Independent QA.

No QA PASS or release approval is granted by this record.
