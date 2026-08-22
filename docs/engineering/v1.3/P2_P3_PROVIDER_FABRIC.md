# P2–P3 — Provider fabric and smart routing

The RangeScout 1.3 provider layer now has capability-driven contracts, immutable provenance, a terms-aware registry, bounded contextual health windows, CLOSED/OPEN/HALF_OPEN circuit breakers, rate-limit cooldown, a bounded TTL cache, result validation, compatibility-aware discrepancy disclosure, and a bounded concurrent router.

The fastest raw response is never sufficient. Request, instrument, symbol, venue, capability, timestamp, freshness/delay, payload, and numeric invariants must pass before a response can win.

Consumer-site candidates have no executable network path. Google Finance, MSN Money, and Binance.US remain disabled. Yahoo remains frozen on its existing approved code path. No retired provider was reintroduced.

Provider review decisions are frozen in `PROVIDER_REVIEW_MATRIX.json`; newly enabled execution is limited to official public structured APIs whose current official documentation was reviewed on 2026-08-18.

Focused gate:

`32 passed, 1 skipped`

Coverage includes malformed/stale/wrong-symbol/wrong-venue/wrong-request rejection, fastest-valid selection, timeout fallback, 429 handling, circuit open/recovery, bounded cache hit/expiry, discrepancy disclosure without averaging, incompatible delay disclosure, provider removal during flight, truthful offline state, shutdown, credentials, existing registry behavior, and continued retired-provider public-runtime exclusion.
