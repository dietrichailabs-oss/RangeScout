# RangeScout 1.4 Architecture

- `app.application.active_symbol` owns one normalized Active Symbol, monotonic generation, request identity, source, and timestamps. Asynchronous results are applied only when symbol and generation still match.
- `app.providers` exposes only Yahoo and BYO-key Finnhub through the production registry. Provider failures never produce synthetic market values.
- `app.research` uses official SEC ticker mapping, submissions, and standard-taxonomy company facts. Values preserve provenance and deterministic selection reasons; unsupported values are N/A.
- `app.application.runtime_coordinator` composes live data, candles, indicators, ticker, official catalyst feeds, alerts, and the provider-limited scanner.
- `app.ui` presents the nine primary workspaces and ten Research subtabs while routing symbol changes through the Active Symbol owner.
- `app.ui.system_tray` owns the persistent Windows tray icon, close-to-tray interception, restore/activate actions, explicit exit, and nonblocking desktop alert messages. `app.ui.branding` resolves the single icon asset shared by the executable, taskbar, window, shortcuts, installer, and tray.
- Credentials stay behind `app.security.credentials` and Windows Credential Manager.
- Historical market data and the bounded SEC response cache remain local and deletable through Settings.

The production runtime contains no trading execution path. Live Trader provides analysis and a risk calculator only.
