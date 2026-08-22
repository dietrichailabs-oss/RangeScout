# RangeScout 1.1 — Master Engineering Roadmap

> Final public 1.1.0 product decision: Alpaca is deferred. Any Alpaca implementation goals below are historical engineering context and do not authorize public 1.1.0 registration, selection, credentials, transport construction, network use, or support claims. Public 1.1.0 providers are exactly Yahoo, Finnhub, and Mock.

## Authoritative baseline

**Released baseline:** RangeScout v1.0.1  
**Build identity:** `rs-v1.0.1-live3`

v1.0.1 is **FROZEN**.

Do not:
- rewrite, retag, replace, or modify the existing v1.0.1 GitHub release;
- silently alter the v1.0.1 product baseline;
- fold 1.1 work back into 1.0.1.

Start a new development cycle for:

**Target public release:** `RangeScout 1.1.0`

---

# Global development rules

RangeScout 1.1 is a **modular milestone-based development cycle**.

Do **not** implement the entire roadmap as one giant monolithic change.

After every milestone:

1. keep source changes limited to that milestone;
2. run focused tests;
3. run the required regression slice;
4. commit intentionally;
5. record changed files;
6. record test results;
7. preserve previous milestone behavior;
8. produce a checkpoint/handoff before moving forward.

No milestone may silently rewrite or invalidate a completed milestone.

If a major architectural blocker is discovered:
**STOP and report it instead of taking a side quest.**

---

# Permanent product constraints

## Free-data requirement

RangeScout must remain usable without Dietrich AI Labs paying a recurring market-data subscription.

Allowed approach:

- **Yahoo** — existing general market data / historical workflows
- **Finnhub** — primary free streaming provider using the user’s own free API key
- **Alpaca** — alternate free streaming provider using the user’s own free account/key and available free data tier
- **Mock** — deterministic offline/testing provider

No paid data provider may become mandatory.

Never embed a Dietrich AI Labs secret/API key into:
- source;
- executable;
- installer;
- defaults;
- manifests;
- GitHub.

If a provider requires an account/key, use **BYO user key** stored securely and locally.

---

# Permanent Windows packaging standard

Starting with RangeScout 1.1.0, every public release must produce:

- `RangeScout_1.1.0_Setup.exe`
- `RangeScout_1.1.0_Portable.zip`
- `RangeScout_1.1.0_Source.zip`
- `SHA256SUMS.txt`

Use **Inno Setup** for the Windows installer.

The installer must include the complete tested one-directory runtime:
- `RangeScout.exe`
- adjacent `_internal`
- required launch/runtime files
- notices/licenses
- required user documentation

Do **not** publish a bare `RangeScout.exe` as though it were a standalone portable binary.

The installer and portable ZIP must contain the **same approved runtime payload**.


## Public GitHub release asset policy

The **internal engineering/QA release set** may still contain the installer, portable package,
source archive, SHA-256 manifests, SBOM, evidence, and compliance material needed to verify the build.

The **public GitHub Release page**, however, must stay intentionally clean.

For RangeScout 1.1.0 and future public releases, upload exactly these two custom release assets:

- `RangeScout_1.1.0_Setup.exe`
- `README.md`

Do not upload the portable ZIP, raw EXE, QA evidence, manifests, handoff bundles, SBOM, test logs,
or other engineering artifacts as public GitHub Release assets.

The public README must:
- identify the installer as the recommended download;
- show the installer SHA-256;
- explain version/build identity;
- link to the repository/tag for application source;
- link to any separate project-controlled corresponding-source location required by third-party licenses;
- contain concise install/use/privacy/data-delay notes.

GitHub's automatically generated source-code links are outside this custom-asset rule.

Do not remove or hide legally required source availability merely to keep the release page visually clean.
Keep those materials at a stable project-controlled location and link them from the README where required.

---

# M0 — Release Engineering Foundation

## Goal

Establish the permanent RangeScout release packaging model before the larger 1.1 feature work progresses too far.

## Inno Setup requirements

`RangeScout_1.1.0_Setup.exe` must:

- install the complete approved runtime;
- support default install path;
- support install paths containing spaces;
- add Start Menu shortcut;
- optionally add Desktop shortcut;
- register in Add/Remove Programs;
- carry correct product/version/publisher metadata;
- support clean upgrade from prior RangeScout versions;
- support clean uninstall;
- preserve `%AppData%\RangeScout` by default;
- not silently delete user-exported CSV files;
- not silently delete local application data.

## Portable package

`RangeScout_1.1.0_Portable.zip` must contain the exact same approved runtime payload.

## Acceptance gate

- installer runtime manifest == portable runtime manifest;
- installer launches correctly;
- portable launches correctly;
- uninstall succeeds;
- path-with-spaces install/uninstall succeeds.

---

# M1 — Provider and Credential Platform

## Goal

Create a clean provider layer for:

- Yahoo
- Finnhub
- Alpaca
- Mock

## Requirements

Add a provider settings area:

**Settings → Market Data Providers**

Show:
- Yahoo
- Finnhub
- Alpaca
- Mock

Show configuration/connection status.

Users must be able to:
- enter Finnhub credentials;
- delete Finnhub credentials;
- enter Alpaca credentials;
- delete Alpaca credentials;
- select active provider where applicable.

## Credential safety

Do not store streaming-provider secrets in plaintext `settings.json`.

Do not expose credentials in:
- logs;
- screenshots;
- error dialogs;
- crash traces;
- manifests.

Provider failures must not silently switch to another provider.

---

# M2 — Streaming Engine

## Goal

Add a dedicated streaming subsystem separate from existing historical-data fetch logic.

Preferred module family:

```text
app/streaming/
    connection.py
    events.py
    subscriptions.py
    reconnect.py
    candle_aggregator.py
```

## Requirements

Support:
- connect;
- disconnect;
- subscribe;
- unsubscribe;
- reconnect;
- backoff;
- heartbeat/health;
- stale-stream detection;
- clean shutdown;
- provider error translation.

Use WebSocket/streaming where supported.

**Do not implement 1 ms HTTP polling.**

Incoming trade events must be processed as they arrive.

Preserve provider timestamps with millisecond precision or better when available.

Network activity must not block the Qt UI thread.

---

# M3 — Live Candle Engine

## Required candle intervals

- 1 second
- 5 seconds
- 15 seconds
- 30 seconds
- 1 minute
- 5 minutes

## Candle behavior

For every qualifying trade:

```text
OPEN   = first trade in bucket
HIGH   = highest trade
LOW    = lowest trade
CLOSE  = most recent trade
VOLUME = accumulated qualifying volume
```

The forming candle updates on every incoming trade.

Separate:
- **data ingestion cadence**
from
- **UI repaint cadence**

Recommended UI redraw:
- approximately 25–100 ms

Do not repaint the entire chart for every trade if message frequency is high.

## Tests

Add deterministic tests for:
- first trade;
- multiple trades;
- high/low expansion;
- close updates;
- volume accumulation;
- bucket rollover;
- out-of-order timestamps;
- duplicate events;
- reconnect boundary;
- interval switching.

---

# M4 — Live Trader Dashboard

Create a dedicated **Live Trader** workspace.

## Required fields

- Symbol
- Current price
- Dollar change
- Percent change
- Bid
- Ask
- Spread
- Last trade timestamp
- Provider
- Stream status
- Market OPEN/CLOSED
- Last successful update time

## Candle selector

- 1s
- 5s
- 15s
- 30s
- 1m
- 5m


## Market-status presentation

Anywhere market status is visible, including the existing Market view and the new Live Trader view:

- `MARKET OPEN` must use **bold green text**.
- `MARKET CLOSED` must use **bold red text**.
- If shown, `PRE-MARKET` and `AFTER HOURS` should use a distinct **bold amber/orange** treatment.
- `HALTED` must use a stronger red warning treatment than normal CLOSED status.
- Explicit status text must always remain present; never rely on color alone.
- The styling must remain readable in System, Light, and Dark themes.

Do not remove or replace the existing Market or Charts tabs.

---

# M5 — Local Trading Indicators

Calculate locally wherever possible.

Required:

- VWAP
- EMA 9
- EMA 20
- RSI
- MACD
- ATR
- RVOL
- volume-spike detection
- gap %
- day high
- day low
- premarket high
- premarket low
- opening range 1m
- opening range 5m
- opening range 15m
- distance from VWAP
- distance from day high
- distance from day low

Keep indicator calculations isolated from the UI layer.

Add deterministic unit tests.

## Risk calculator

Inputs:
- entry;
- stop;
- max dollar risk.

Outputs:
- share count;
- actual risk;
- distance to stop.

This is calculation tooling only.

Do not implement automated trade recommendations or order execution.

---

# M6 — Watchlist Ticker Ribbon

Add a persistent ticker ribbon.

## Settings

```text
Ticker Position:
Top
Bottom
Hidden
```

For watched symbols show, when available:

- symbol;
- price;
- dollar change;
- percent change;
- halt status;
- stale-feed status.

Clicking a ticker should open/load that symbol in Live Trader.

## Important

Do not fire one high-frequency HTTP request per watchlist symbol.

Use existing streaming subscriptions/batching.

Respect free-provider subscription limits.

If the watchlist exceeds available streaming slots:
- show the limit;
- degrade gracefully;
- do not create a request storm.

---

# M7 — Catalyst Feed Platform

Create modular catalyst/news infrastructure.

Preferred module family:

```text
app/catalysts/
    feeds/
    normalization.py
    entities.py
    symbol_mapping.py
    dedupe.py
    classification.py
    relevance.py
    storage.py
```

Normalize every event into a common structure:

- source
- source URL
- published timestamp
- received timestamp
- headline/title
- summary/body when source terms allow
- matched company names
- matched symbols
- sectors
- event category
- relevance
- urgency
- duplicate/event identity

Respect each source’s redistribution/content-retention terms.

---

# M8 — Official Free Catalyst Sources

## SEC / EDGAR

Monitor relevant filings including:

- 8-K
- 10-Q
- 10-K
- S-3
- 424B
- 13D
- 13G
- Forms 3 / 4 / 5
- Form 144

## Nasdaq trading halts

Track:

- HALTED
- RESUMPTION PENDING
- RESUMED

Halts on the active symbol should be visually high priority.

## White House

Monitor applicable official White House content including:

- Presidential Actions
- Executive Orders
- Memoranda
- Proclamations
- Fact Sheets
- major Statements
- major Remarks

Classify market-sensitive subjects including:

- tariffs
- trade restrictions
- sanctions
- export controls
- semiconductors
- AI/technology
- defense
- energy/oil
- banking
- crypto
- healthcare/pharma
- autos
- agriculture
- aviation
- infrastructure
- taxes
- government contracts
- antitrust
- environmental regulation
- fiscal policy

## Congress

Use official/free sources where possible.

Track progression such as:

- introduced
- House passed
- Senate passed
- final passage
- sent to President
- signed
- vetoed

Use official House/Senate vote information where applicable.

If a source requires a **free** user API key:
treat it as BYO/free credential rather than embedding a shared project key.

---

# M9 — Watchlist News Correlation

Prioritize event relevance in this order:

1. active Live Trader symbol
2. watchlist symbols
3. related sectors
4. broader market events

Implement:

- company-name → ticker mapping;
- aliases;
- sector mapping;
- duplicate suppression;
- event grouping.

## Relevance levels

- HIGH
- MEDIUM
- LOW

Optional directional classification:

- potentially positive
- potentially negative
- mixed
- unclear

This must be labeled as automated event classification, not price prediction or investment advice.

---

# M10 — Catalyst Sidebar

Add a right-side catalyst/news panel to Live Trader.

Example:

```text
HIGH — NVDA
White House export-control announcement
38 sec ago
Source: White House

Matched:
NVDA · AMD · AVGO
Semiconductors
```

Example:

```text
HALT — XYZ
Trading halted
12 sec ago
Source: Nasdaq
```

Example:

```text
SEC — ABC
New 8-K filing
2 min ago
Source: SEC
```

Each event should show:

- source;
- timestamp;
- matched symbols;
- event category;
- relevance.

Where appropriate, clicking should open the original source.

---

# M11 — Alert System

Add configurable alerts for:

- trade halt/resume
- SEC filing
- government catalyst
- watchlist news
- volume spike
- VWAP cross
- opening-range break
- new day high/low
- stale stream
- provider disconnect
- provider reconnect

Support:

- visual alert
- optional sound
- optional desktop notification

Prevent duplicate alert spam.

---

# M12 — Market Scanner

Scanner rules may include:

- unusual volume
- gap up/down
- VWAP cross
- opening-range break
- new day high/low
- volatility spike
- news catalyst
- SEC catalyst
- government catalyst
- halt/resumption

## Free-tier limitation rule

Do not claim full-market real-time scanning unless the active provider actually supports it.

Initial real-time scan scope should be:

- active symbol
- subscribed streaming universe
- watchlists

Wider-market scans may use slower/batched free data where allowed.

---

# M13 — Performance and Failure Testing

Simulate:

- network loss
- timeout
- provider throttling
- malformed messages
- WebSocket reconnect
- duplicate trades
- out-of-order trades
- market-open transition
- market-close transition
- halt/resume
- catalyst burst
- long watchlist
- provider subscription limit
- missing API credentials
- invalid API credentials

Verify:

- no UI freeze;
- no runaway thread;
- no request storm;
- no unbounded memory growth;
- no secret leakage;
- no silent mock fallback;
- no modal-error spam;
- clean shutdown.

Run long-session tests.

---

# M14 — Full Integration Regression

Regression-test existing v1.0.1 features:

- Market
- Charts
- Watchlists
- Notes
- Alerts
- Exports
- Comparison
- Settings
- local-data deletion
- Yahoo
- Mock
- installer/uninstaller
- U.S. market calendar

Then test new 1.1 systems:

- Finnhub
- Alpaca
- secure credentials
- streaming
- live candles
- indicators
- ticker ribbon
- catalyst feeds
- SEC
- Nasdaq halts
- White House
- Congress
- alerts
- scanner

---

# M15 — Final 1.1 Packaging and Release

Create the full **internal verification set**:

- `RangeScout_1.1.0_Setup.exe`
- `RangeScout_1.1.0_Portable.zip`
- `RangeScout_1.1.0_Source.zip`
- `SHA256SUMS.txt`

For the **public GitHub Release**, upload exactly two custom assets:

- `RangeScout_1.1.0_Setup.exe`
- `README.md`

The portable/source/checksum/QA artifacts remain internal or available through the repository/project-controlled source locations as appropriate; they do not clutter the public release asset list.

Generate and verify:

- SBOM
- third-party notices
- source/license obligations
- artifact inventory
- package manifest
- SHA-256
- release notes

Installer and Portable must contain the same approved runtime payload.

Final practical QA reviews the **exact immutable release artifacts**.

Only after final PASS:

Publish GitHub `v1.1.0`.

---

# Final product goal

RangeScout 1.1 should be a free-to-use Windows trading-analysis companion with:

- free/BYO streaming providers;
- live tick-driven candles;
- millisecond trade timestamps where available;
- local trading indicators;
- market OPEN/CLOSED;
- watchlist ticker ribbon;
- SEC alerts;
- Nasdaq halt alerts;
- White House policy monitoring;
- Congressional vote/action monitoring;
- watchlist-focused catalyst/news correlation;
- relevance classification;
- market scanner;
- proper Inno Setup installer;
- complete portable runtime;
- source package;
- exact hashes/manifests.

And it must preserve RangeScout’s existing:

- local-data behavior;
- privacy controls;
- historical analysis;
- watchlists;
- notes;
- alerts;
- charts;
- comparisons;
- exports;
- Yahoo provider;
- Mock provider;
- themes.

## Explicit prohibitions

Do not:

- add automatic trade execution;
- embed shared provider secrets;
- require a paid Dietrich AI Labs data subscription;
- pretend delayed data is exchange-tick realtime;
- claim full-market streaming when free provider limits do not support it;
- turn the application into one monolithic module;
- ship an EXE alone without its required runtime;
- bypass milestone gates.

---

# Codex working instruction

Work through this roadmap **in order**.

For each milestone:

1. implement only that milestone;
2. run focused tests;
3. run the required regression slice;
4. commit;
5. record exact changed files;
6. record exact tests/results;
7. produce a milestone handoff/checkpoint;
8. wait for the next milestone instruction before broadening scope.

Do not skip ahead.

Do not take unrelated side quests.

Do not modify the frozen v1.0.1 release.

M0 release-engineering foundation is accepted as the first completed 1.1 milestone. Continue from M1 without reopening M0 unless a later integration regression proves a specific M0 defect.
