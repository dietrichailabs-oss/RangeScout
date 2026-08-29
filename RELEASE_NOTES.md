# RangeScout 1.6.2 Release Notes

RangeScout 1.6.2 is the current Windows release from Dietrich AI Labs.

## Highlights

- Improved canonical instrument identity, ranked search, and discovery across supported instrument types.
- Instrument-aware Research routing with clearer supported and unavailable states.
- Improved SEC-backed fiscal-period and frame alignment, including deterministic handling for off-calendar and 52/53-week issuers.
- Stronger Active Symbol synchronization, immediate old-symbol clearing, request deduplication, and stale-result protection.
- Provider-aware quote, history, Research, analyst, and catalyst behavior with explicit provenance and availability states.
- Preserved reserved quote priority so slower history, Research, scanner, news, and logo work cannot starve visible quote requests.
- Lifecycle and persistence corrections across watchlists, notes, settings, local data, installer upgrade, and uninstall behavior.
- General reliability, package-integrity, and regression hardening.

RangeScout preserves the established local-first workstation experience, system-tray operation, company/logo database, SEC fundamentals, Live Trader, scanner, alerts, watchlists, notes, exports, provider diagnostics, and secure user-supplied credential storage.

## Download and integrity

- Installer: `RangeScout_1.6.2_Setup.exe`
- Installer size: `36,932,361` bytes
- Installer SHA-256: `5B0A5822ED09004405BA0BEEA6C5473BF77E283DDA7BBBF622D07C15B888D52D`

Windows may display a reputation or security warning because the installer is not publicly trusted-signed. Verify the published SHA-256 before installation if desired.

## Availability and disclosures

- Market and provider availability, coverage, latency, and entitlement vary.
- Some enhanced features require credentials supplied by the user for the applicable provider's free or eligible account tier.
- When legitimate provider data is unavailable, RangeScout displays an unavailable or `N/A` state rather than fabricating a value.
- RangeScout is an informational and analytical tool. It does not provide investment advice and does not execute trades.
- No public simulated-data provider or deferred brokerage provider is exposed, and no shared Dietrich AI Labs provider credential is embedded.

See the [RangeScout repository](https://github.com/dietrichailabs-oss/RangeScout), [product page](https://www.dietrichailabs.com/rangescout.html), and [download center](https://www.dietrichailabs.com/downloads.html) for current information.
