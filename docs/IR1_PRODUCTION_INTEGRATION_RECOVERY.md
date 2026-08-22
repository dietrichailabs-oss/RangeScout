# IR1 production integration recovery

IR1 resumes from stopped commit `514be48af9ba3d738f69dad85b7b1d24c1a4be35`; it does not replace the accepted M2–M13 component work.

## Production composition

`RuntimeCoordinator` is the application-owned composition root. It owns:

- `LiveTradingRuntime`: secure credential loading, Qt WebSocket transport, the existing `StreamingConnection`, subscription lifecycle, existing `CandleAggregator`, bounded candle history, indicators, ticker state, scanner observations, and live alerts.
- `CatalystRuntime`: bounded background official-feed work, existing parsers/correlation/storage, catalyst alerts, halt state, and delivery to the real Live Trader sidebar.
- the real M11 `AlertDispatcher`, including visual, optional sound, and optional desktop channels.

The window creates one coordinator, forwards provider/symbol/watchlist/interval changes, and shuts it down before the Qt window closes. Reconnect callbacks honor the existing manual-close guard. Worker completions are queued to the owning Qt thread and ignored after shutdown.

## Correction map

| File | Authorized correction |
|---|---|
| `app/application/runtime_coordinator.py` | New single composition root and subsystem lifecycle. |
| `app/application/live_trading_runtime.py` | New production stream-to-candle-to-indicator/ticker/scanner/alert path; 600-candle and 10,000-event dedupe bounds. Derived/UI publication is coalesced to 50 ms or candle completion while every trade still updates the forming candle. |
| `app/application/catalyst_runtime.py` | New two-worker bounded scheduler, retained-event merge, production feed-to-sidebar path, alerts, and shutdown guard. |
| `app/catalysts/sec_resolver.py` | New official SEC ticker/CIK resolver with 24-hour minimal local cache. |
| `app/ui/main.py` | Coordinator construction, real UI sinks, truthful bid/ask and stream mode, interval/provider/watchlist lifecycle, actual alert preferences, and close handling. |
| `app/ui/runner.py` | Correct production startup-tab routing for the added Live Trader and Scanner tabs. |
| `app/catalysts/symbol_mapping.py` | Integration defect: preserve authoritative adapter symbols/company names/sectors and union catalog enrichment. |
| `app/catalysts/feeds/http.py` | Integration defect: decode declared gzip/deflate official responses before parser use. |
| `tests/integration/test_ir1_production_composition.py` | Required real-window production composition acceptance test with deterministic transport/feed injection. |
| `tests/performance/test_ir1_production_runtime_stress.py` | Production-path M13 burst, reconnect, stale, limits, failure, bounds, interval, and shutdown tests. |
| `scripts/handoff/capture_ui_surfaces.py` | Correct final evidence routing for all ten current production tabs and normalize display-name separators/case during fail-closed checks. |

## Truthful runtime behavior

- Finnhub: BYO key, one connection, at most 50 symbols.
- Yahoo and Mock: explicit `SNAPSHOT MODE`; no fake WebSocket state.
- Bid/ask/spread: `Unavailable` unless a selected provider path actually supplies them.
- Scanner: active symbol plus subscribed/watchlist universe only.
- Indicators: `N/A` until actual previous-close/candle inputs are available.
- Government events without deterministic symbol/sector mapping remain broad events; no ticker is fabricated.

## Polling and retention

- SEC: 15 minutes; 0.2-second request floor; declared owner contact User-Agent.
- Nasdaq halts: 60 seconds minimum.
- White House: 15 minutes.
- Congress.gov: 15 minutes and only when a secure BYO key exists.
- Feed workers: maximum two production threads.
- Catalyst store: 1,000 sanitized deduplicated events.
- Candle history: 600 completed candles per subscribed symbol.

No failed-candidate installer or portable payload is authorized for publication.
