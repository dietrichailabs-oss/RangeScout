# RangeScout 1.2 Provider Review

The production registry exposes Yahoo and Finnhub only. Yahoo is the default network-backed provider. Finnhub uses a user-supplied credential stored in Windows Credential Manager. Unsupported, legacy, injected, environment, CLI, or configuration provider identifiers cannot enter the production registry and migrate safely to Yahoo where applicable.

Provider failure is explicit and never creates synthetic prices. SEC Research is a separate official-data subsystem with a declared Dietrich AI Labs user agent, an 8 requests/second limiter, bounded caching, provenance, and explicit unavailable states.
