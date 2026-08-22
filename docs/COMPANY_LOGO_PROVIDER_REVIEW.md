# RangeScout Company Logo Provider Review

**Review date:** 2026-08-19
**Feature:** company/security logo display for the Active Symbol
**Implementation state:** RangeScout 1.4.0 Engineering candidate; not part of the frozen QA-approved 1.3.0 bytes

## Selected provider: Logo.dev Logo API

RangeScout's proposed logo feature uses Logo.dev's documented **stock ticker image endpoint** rather than scraping company sites or consumer finance pages.

Documented production pattern:

```text
https://img.logo.dev/ticker/AAPL?token=<publishable-key>&size=96&retina=true&format=png&fallback=404
```

RangeScout requests a ticker logo only after a user-driven Active Symbol change or quote update. Missing/unconfigured logos fall back to the existing ticker monogram.

## Authentication

- Logo.dev requires a **publishable key** for Logo API image requests.
- RangeScout stores the configured key in the existing Windows Credential Manager abstraction under provider id `logo_dev`.
- The key is never written to `settings.json`, SQLite, logs, evidence, or error strings.
- No Dietrich AI Labs shared key is embedded in the source or runtime.

## Caching / persistence decision

The free Logo.dev documentation describes end-user/browser caching but says storing logos on your own infrastructure requires an appropriate caching/self-hosting license.

Therefore this patch intentionally:

- keeps fetched image bytes only in a bounded in-process session cache;
- does **not** store logo image bytes in SQLite;
- uses SQLite only for non-image status/retry/provenance metadata;
- never stores the publishable key or authenticated logo URL in SQLite;
- uses provider/CDN refresh behavior again on a later application session.

This preserves RangeScout's SQLite speed architecture without turning the local database into a persistent third-party logo mirror.

## Attribution / public-release note

Logo.dev's current documentation states that free plans require attribution. Dietrich AI Labs/Release must confirm the applicable account classification before enabling the provider in a public build.

The UI includes a visible `Logo.dev` link in Settings. Logo.dev's native-app guidance also requires free-plan attribution on the application's public website or app-store listing. That public attribution/account verification remains a mandatory publication condition and is not claimed by this Engineering handoff.

## Failure behavior

- no key: monogram, no network call;
- missing logo: monogram plus 30-minute negative retry suppression;
- provider rate limit: monogram plus 10-minute retry suppression;
- network/HTTP failure: monogram, no modal error spam;
- stale Active Symbol result: discarded by the existing Active Symbol generation gate;
- image over 2 MiB or non-image response: rejected;
- key never appears in user-visible errors.

## Supported initial lookup

- U.S. stock/ETF ticker lookup directly;
- selected documented non-U.S. exchange suffix mappings when RangeScout has a trusted exchange value;
- unknown exchange strings are not guessed;
- cryptocurrency logo lookup is intentionally out of this patch's UI scope even though Logo.dev offers a separate crypto endpoint.

## Official sources reviewed

- Logo.dev Stock Ticker Logo API documentation: `https://www.logo.dev/docs/logo-images/ticker`
- Logo.dev Logo API introduction: `https://www.logo.dev/docs/logo-images/introduction`
- Logo.dev caching guidance: `https://www.logo.dev/docs/platform/caching`
- Logo.dev fair-use guidance: `https://www.logo.dev/docs/platform/fair-use`
- Logo.dev attribution guidance: `https://www.logo.dev/docs/platform/attribution`
- Logo.dev terms: `https://www.logo.dev/legal/terms`

The sources above were refreshed on 2026-08-19. The Terms page reported `Last updated: July 28, 2026`. Current documentation continues to permit ticker lookup in financial applications, requires a user publishable key, forbids bulk scraping/redistribution, documents 24-hour end-user caching, reserves self-hosting for eligible paid plans, and requires free-plan public attribution.
