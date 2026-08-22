# P8–P9 — Production composition and extended-instrument readiness

The real `RangeScoutApplication` composition root now owns the provider-fabric registry and bounded router, and shuts both down with the main window. The released Yahoo/Finnhub selector and Live Trader composition remain unchanged, so expanded asset-specific providers cannot accidentally replace an incompatible equities path.

Settings retains the accepted layout and adds an in-card provider-fabric surface showing public/no-key availability, BYO-key requirement/configuration, capability and delay class, and explicit disabled reasons. Twelve Data, Alpha Vantage, and FRED credentials are written only through Windows Credential Manager. Public crypto adapters require no key. Disabled candidates expose no input or execution route.

Existing Active Symbol, stale-result generation binding, Research/Fundamentals, catalysts, scanner, alerts, watchlists, notes, exports, taskbar identity, and single-window behavior are preserved. Options remain model/schema-ready only; no public options feature or scraped chain was enabled.

Focused production-composition gate: `27 passed`.
