# Provider Credential Security

The RangeScout 1.3.0 legacy equity selector retains Yahoo and Finnhub. The asset-aware fabric also recognizes reviewed public/no-key providers and optional Twelve Data, Alpha Vantage, and FRED user-key adapters.

- Yahoo requires no credential.
- Finnhub uses the current user's own API key.
- Logo.dev company-logo lookup uses the current user's own publishable key.
- No Dietrich AI Labs shared provider credential is present in source, defaults, packaging, or manifests.

On Windows, Finnhub and Logo.dev credentials are generic credentials in the current user's Windows Credential Manager under the `RangeScout/MarketDataProvider` namespace. They are never written to `settings.json`, SQLite, displayed after storage, or included in logs, errors, screenshots, crash traces, manifests, or QA evidence.

Finnhub authentication is sent only to Finnhub. Selecting an unconfigured provider fails explicitly and never produces substituted prices.
