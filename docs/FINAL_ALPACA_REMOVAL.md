# Final Alpaca removal — RangeScout 1.1.0 r2

This surgical correction closes `RS11-PUB-ALPACA-001` for the public RangeScout 1.1.0 candidate by deferring Alpaca. Yahoo, Finnhub, and Mock behavior remain in scope and unchanged except that the production provider policy now explicitly rejects every unsupported provider ID.

## Correction map

| Surface | r2 correction |
|---|---|
| Public provider policy | One allowlist contains exactly `yahoo`, `finnhub`, and `mock`. |
| Default registry | No Alpaca provider object is imported, registered, or constructed. |
| Settings migration | Saved `alpaca` or another unsupported provider ID is rewritten to Yahoo while unrelated settings survive. Policy version is 4. |
| Application bootstrap | Explicit unsupported provider selection is rejected before data/runtime initialization; injected registries are filtered to the public allowlist. |
| Provider Settings UI | Alpaca selector labels, credential fields, widgets, and save/delete handling are absent. |
| Credential configuration | Public credential operations accept Finnhub market-data credentials (and the separate Congress catalyst key) but reject Alpaca. |
| Streaming runtime | Only Finnhub has a production streaming limit/codec path. Unsupported provider IDs are rejected before credential lookup or transport construction. |
| Qt transport | Only Finnhub transport construction remains in the production UI. |
| Public documentation | README, release notes, privacy notice, and market-data notice no longer claim Alpaca support. |
| Deferred source | Low-level Alpaca adapter/protocol source may remain for future engineering, but it is not exported by `app.providers` and has no public production composition path. |

## Preserved behavior

IR1 composition, Finnhub BYO-key streaming and 50-symbol guard, Yahoo snapshots/history, Mock, candles, indicators, ticker, official catalysts, alerts, scanner, calendar, themes, local-data controls, and Inno packaging remain preserved.
