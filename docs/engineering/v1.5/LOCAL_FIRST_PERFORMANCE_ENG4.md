# RangeScout 1.5.0 local-first performance correction

Build `rs-v1.5.0-local-first-eng4` replaces the UI-bound combined quote/history report with independently completing worker tasks.

## Selection and startup path

1. Commit and normalize the Active Symbol.
2. Invalidate prior generations and synchronously clear all symbol-bound presentation.
3. Render an in-memory snapshot when available.
4. Otherwise read identity, last-known quote, and one provider's recent bars in one bounded SQLite transaction (three indexed statements).
5. Render local identity, cached quote, and cached charts immediately.
6. Dispatch quote and stale/missing history independently through Qt workers.
7. Accept results only when symbol and generation still match.

Market session status remains entirely local through the NYSE calendar. Research/SEC and Analyst Outlook remain visibility/TTL driven. Company logos remain local-first, with network fallback occurring only in a worker when no permitted local/session asset resolves. Catalysts, discovery, company maintenance, scanner/runtime feeds, and provider diagnostics remain asynchronous.

## Network budgets

Visible quote races have a three-second maximum router budget. Background history has its own fifteen-second budget. A completed quote is never joined to or delayed by history, SEC, analyst, logo, or catalyst work. A request that exceeds its capability budget is ignored for that cycle and contributes to provider health/circuit diagnostics.

## Local data

Schema v6 adds `rs_last_quotes` and indexes for canonical symbols, aliases, company names, MICs, provider mappings, historical symbol/date reads, and quote recency. SQLite uses WAL plus bounded busy timeouts. `RangeScout_Company_Master.sqlite` contains only stable public issuer/listing reference data—no prices, credentials, or proprietary provider payloads—and is merged additively without overwriting newer user records.
