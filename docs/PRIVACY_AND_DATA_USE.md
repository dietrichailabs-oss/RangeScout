# RangeScout Privacy and Data Use

RangeScout 1.4.0 is a local Windows desktop application. Settings, watchlists, notes, local history, bounded provider metadata/cache state, discovery records, and a bounded SEC research cache are stored under `%AppData%\RangeScout`. Exported CSV files remain outside deletion scope unless the user selects a location inside that directory.

Entered symbols and requests are sent directly to Yahoo or Finnhub according to the selected provider. Research requests are sent to official SEC endpoints. Official catalyst requests can be sent to SEC, Nasdaq Trader, White House, or Congress.gov.

RangeScout has no cloud account and embeds no shared provider key. User-supplied Finnhub, Congress.gov, and Logo.dev credentials are stored in the current Windows user's Credential Manager, never in `settings.json` or SQLite, and are sent only to their respective providers.

**Delete Local RangeScout Data** removes application-owned settings, watchlists, notes, history, and research-cache data. The app fails closed if its approved data root cannot be used safely.
