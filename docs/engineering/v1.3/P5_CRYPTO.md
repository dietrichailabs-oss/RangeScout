# P5 — Legitimate public crypto coverage

Three production-capable adapters passed the current official-source terms gate:

- Coinbase Exchange public market data: quote, candles/history, and product discovery.
- Kraken public Spot REST market data: quote, OHLC/history, and asset-pair discovery.
- CoinPaprika no-key free endpoints: USD-normalized quotes and broad coin discovery. Premium historical scope is not claimed or invoked.

All use a bounded JSON transport with short timeouts, response-size limits, generic redacted failures, 429 handling, and a RangeScout user agent. No adapter exposes account, trading, order, wallet, or private endpoints. Venue/product IDs and provider timestamps/receipt-time disclosures remain attached to normalized results. `BTC-USD` and Kraken `XBTUSD` are mapped without pretending their raw provider strings are identical.

Google Finance, MSN Money, and Binance.US remain non-executable disabled candidates. No consumer HTML scraping exists.

Focused gate: `30 passed`.
