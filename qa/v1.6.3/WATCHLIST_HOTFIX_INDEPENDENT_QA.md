# RangeScout 1.6.3 Watchlist Hotfix — Independent QA

## Verdict

**PASS — exact pushed Engineering candidate passes the scoped watchlist hotfix gate.**

This decision applies only to the RangeScout 1.6.3 watchlist selection, single-symbol Add/Remove, Active Symbol quick-add, watched-state updates, and dependent ticker/runtime/scanner refresh behavior requested for Issue #3. It does not merge or publish the candidate and does not modify published v1.6.2 artifacts.

## Exact candidate

- Repository: `dietrichailabs-oss/RangeScout`
- Branch: `codex/v1.6.3-watchlist-hotfix`
- Commit: `ae98a55f6afed24b4a9fc9982dacb8ef07de8b20`
- Tree: `d06e235b972d03272c47b5aa3bed54dfaa17783a`
- Parent/base: `182787909d54ef403e3583699479c0c140239821`
- Build: `rs-v1.6.3-watchlist-hotfix-eng1`

Remote branch/commit/tree binding was independently verified.

## QA timing

- Start: `2026-08-31T13:04:35.309740-04:00`
- Decision freeze: `2026-08-31T13:07:41.475083-04:00`
- Elapsed: `00h03m06.165343s`

## Independent scope review

Independent QA reviewed the exact pushed diff and exact hotfix source. The persistence store already provides idempotent symbol membership; the defect was in UI selection/target wiring.

Validated behavior:

1. Selecting a watchlist persists that exact watchlist as the target.
2. Selecting a watchlist keeps the single-symbol Add/Remove input empty rather than inserting the whole membership list.
3. Add Symbol accepts one normalized supported symbol and adds it only to the selected list.
4. Remove Symbol removes one normalized symbol only from the selected list.
5. Duplicate adds remain idempotent.
6. Missing/invalid selection or malformed multi-symbol input fails closed with a user-visible warning path.
7. Active Symbol quick-add targets the persisted selected watchlist rather than blindly using the first list.
8. With no watchlists, quick-add creates `my-watchlist` / `My Watchlist` and adds the current Active Symbol.
9. Existing membership yields `✓ Watchlisted`; changing Active Symbol recomputes the watched/unwatched state.
10. Watchlist mutations refresh the flattened watchlist symbol cache, ticker path, runtime symbol universe, and scanner rendering.
11. Production `RuntimeCoordinator.set_symbols()` delegates to `LiveTradingRuntime.set_symbols()`, whose `_replan()` immediately calls the ticker sink with the new subscription plan, so ticker refresh is not dependent on a later market tick.

## Independent deterministic harness

A separate QA harness exercised the exact pushed watchlist/Active Symbol logic with independent cases beyond Engineering's listed assertions:

- selected-list persistence and single-symbol field clearing
- single-symbol add/remove and duplicate idempotence
- invalid/missing input warning/fail-closed behavior
- selected-list quick-add targeting versus first-list decoy
- no-list default creation
- watched-state transition AAPL -> MSFT
- ticker/runtime/scanner refresh on add/remove
- stale persisted selection fallback/persistence
- supported canonical punctuation forms (`BRK.B`, `BTC/USD`, `COF$J`, `^GSPC`, `RF-PE`)
- blank symbol rejection

Result: **10/10 PASS**.

## Engineering evidence reviewed

Issue #3 records:

- focused gate: `24 passed`
- package/identity gate: `17 passed`
- complete suite: `819 passed`, one documented pre-existing company-master evidence-hash failure outside Issue #3 scope

The unrelated company-master evidence-hash failure was not hidden or repaired in this hotfix and is not promoted to a watchlist blocker.

## Environment limitation

The Independent QA container does not contain PySide6, so Independent QA did not claim a native Qt rerun of Engineering's integration suite. Engineering's Qt results are supporting evidence only. Independent QA independently verified the exact GitHub source and exercised the scoped hotfix logic deterministically.

## Decision

No scoped release blocker was found in commit `ae98a55f6afed24b4a9fc9982dacb8ef07de8b20` / tree `d06e235b972d03272c47b5aa3bed54dfaa17783a`.

**Independent QA verdict: PASS.**

Issue #3 should remain open until the project owner explicitly authorizes merge/release handling. This QA record does not merge or publish anything.
