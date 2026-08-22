# P6–P7 — Futures architecture and optional BYO providers

Futures are architecture-ready but market data remains disabled because no exact zero-cost feed passed the current licensing gate. The domain represents root/full symbol, exchange, month code/year, expiration, multiplier, tick size/value, and delay class. Deterministic continuous-series selection discloses the active contract and supports only explicit `none` or difference adjustment; no opaque smoothing or realtime claim exists.

Optional user-key adapters now cover:

- Twelve Data for quota-aware quote/history capabilities allowed by the user's plan.
- Alpha Vantage as a low-frequency/end-of-day fallback with a conservative local daily quota.
- FRED for macro series only; it cannot enter a market quote race.

Keys remain in Windows Credential Manager through the existing secure credential store. Adapters retrieve a key only at request time. Results, exceptions, health state, caches, and credential representations exclude secret values. No Dietrich AI Labs key is embedded.

Focused gate: `56 passed, 1 skipped` (interactive Windows credential-session proof skipped where unavailable).
