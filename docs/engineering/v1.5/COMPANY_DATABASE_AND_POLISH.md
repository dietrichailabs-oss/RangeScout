# RangeScout 1.5.0 company database and polish architecture

RangeScout 1.5.0 extends the existing additive SQLite model to schema v6. The company master remains keyed by stable local instrument identity and records canonical symbol, security name, venue/MIC, CIK, aliases, active state, listing/delisting dates, and logo lookup/provenance/refresh metadata. Schema v6 adds a last-known quote cache and hot-path indexes used by the one-transaction local symbol snapshot.

Logo provider credentials and image BLOBs are excluded from SQLite. A local logo path is populated only for a project/user-controlled or per-asset license-cleared file. Finnhub, Twelve Data, and Logo.dev response bytes remain session-only unless current terms explicitly permit persistence. Wikimedia/Simple Icons candidates stay disabled until per-asset license and attribution metadata are retained.

Company/listing refresh delegates to the existing official Nasdaq Trader transactional discovery importer. Logo maintenance is bounded, incremental, single-worker, retry-aware, and limited to eligible known symbols. An application-owned hourly due check independently evaluates company and logo schedules throughout long-running tray sessions, reads schedule changes without restart, deduplicates in-flight work, and stops with the application. Defaults are weekly company metadata and monthly known-logo refresh; Off and manual controls are supported.

The UI resolves `System` into an effective Windows light/dark theme, clears every old symbol-bound surface synchronously on a committed symbol change, renders bounded memory/SQLite cache data first, and then permits generation-checked network work. Cached data is never labeled live.

Non-sensitive workstation state may be exported/imported through an allowlist. Credentials remain exclusively in the secure credential-store path backed by Windows Credential Manager in production.
