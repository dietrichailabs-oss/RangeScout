# RangeScout 1.6.2 Release Notes

RangeScout 1.6.2 is the current Windows release from Dietrich AI Labs.

## Major highlights

### Smarter instrument search and identity

- Adds deterministic canonical instrument identity across Market, Research, discovery, and provider routing.
- Gives exact tickers precedence while improving ranked company-name search, punctuation normalization, historical aliases, and disambiguation.
- Expands supported security semantics across equities, funds, preferreds, ADRs, warrants, rights, units, ETNs, indices, partnerships, and related structures.
- Reconciles official discovery updates without casually destroying identity or listing history, and rejects incomplete official-source refreshes rather than treating them as removals.

### Better market and provider routing

- Improves fused quote/history presentation, immediate Active Symbol transitions, and rejection of stale background results.
- Routes canonical instruments through provider-specific symbols and explicit capability checks across the broader catalog.
- Preserves the reserved visible-quote priority lane so slower history, Research, scanner, news, and logo work cannot starve the quote currently on screen.
- Shows unsupported, unavailable, cached, delayed, and offline states explicitly instead of fabricating a route or value.

### Stronger Research and fundamentals

- Routes corporate issuers, funds, and other market instruments through appropriate Research paths.
- Improves preferred, partnership, LLC-unit, IFRS, US-GAAP, taxonomy, currency, and reporting-regime handling.
- Keeps legitimate missing or incomparable facts visible as unavailable or `N/A`.

### SEC filing and fiscal-period accuracy

- Separates quarterly, year-to-date, annual, transition, and instant facts deterministically.
- Reconciles partial amendments/restatements per metric and stabilizes quarterly growth comparisons.
- Separates fiscal identity from calendar frames, treats `fy + fp` atomically, and uses frame-only fallback only when appropriate.
- Adds deterministic SEC best-alignment handling for off-calendar and 52/53-week reporters while continuing to reject wrong adjacent frames.

### Reliability, persistence, and installer behavior

- Expands migrations and idempotent schema recovery while keeping reference seeding deterministic.
- Preserves notes, settings, local state, AppData, and exports through the supported lifecycle.
- Verifies the supported 1.6.1 → 1.6.2 upgrade path and confirms uninstall remains limited to RangeScout-owned install scope.
- Broadens regression and package-integrity coverage across discovery, routing, classification, Research, SEC periods, persistence, packaging, upgrade, and uninstall behavior.

RangeScout preserves its local-first workstation, system-tray operation, company/logo database, Live Trader, SEC fundamentals, scanner, alerts, watchlists, notes, exports, provider diagnostics, and secure user-supplied credential storage.

## Download and integrity

- Windows package: `RangeScout_1.6.2_Windows.zip`
- Installer: `RangeScout_1.6.2_Setup.exe`
- Installer size: `36,932,361` bytes
- Installer SHA-256: `5B0A5822ED09004405BA0BEEA6C5473BF77E283DDA7BBBF622D07C15B888D52D`
- Windows ZIP size: `36,433,791` bytes
- Windows ZIP SHA-256: `518CA36DD60143259925908B4F720F48F499EA046EED4D3327DADB6B4B0B65DE`

Windows may display a reputation or security warning because the installer is not publicly trusted Authenticode-signed. Verify the published SHA-256 before installation if desired.

## Availability and disclosures

- Market and provider availability, coverage, latency, and entitlement vary.
- Public sources do not all require credentials; some enhanced providers require credentials supplied by the user for the applicable free or eligible account tier.
- Supported credentials are stored through Windows Credential Manager; no shared Dietrich AI Labs provider key is embedded.
- When legitimate provider data is unavailable, RangeScout displays an unavailable or `N/A` state rather than fabricating a value.
- RangeScout is an informational and analytical tool. It does not provide investment advice and does not execute trades.
- No public simulated-data provider or deferred brokerage provider is exposed.

See the [RangeScout repository](https://github.com/dietrichailabs-oss/RangeScout), [GitHub release](https://github.com/dietrichailabs-oss/RangeScout/releases/tag/v1.6.2), [product page](https://www.dietrichailabs.com/rangescout.html), and [download center](https://www.dietrichailabs.com/downloads.html) for current information.
