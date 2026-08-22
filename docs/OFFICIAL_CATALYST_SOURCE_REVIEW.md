# Official catalyst source review (M8)

Reviewed 2026-08-17 against official sources.

- SEC EDGAR data is free to access/reuse. Automated clients must declare a contact User-Agent and remain below 10 requests/second: <https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data>.
- Nasdaq Trader's free halt RSS feed covers Nasdaq and other exchange-listed securities, updates once per minute, and must not be queried more frequently: <https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS>.
- Nasdaq's RSS terms prohibit modifying or combining the feed in a way that misrepresents it. RangeScout retains explicit source attribution/status and only correlates normalized events without presenting them as Nasdaq-combined data: <https://www.nasdaqtrader.com/content/administrationsupport/agreementstrading/THRSSFeedTermsCond.pdf>.
- White House items are retained as official title/category/link metadata from <https://www.whitehouse.gov/feed/>; RangeScout does not republish page bodies.
- Congress.gov explicitly permits public retrieval and reuse of machine-readable data, requires a free BYO API key, and documents a 5,000 requests/hour limit: <https://github.com/LibraryOfCongress/api.congress.gov>.

All sources are fetched locally. No shared project API key or central proxy is used. Feed
errors remain source-specific, timestamps and source URLs remain visible, and the UI must
not imply guaranteed immediacy or completeness.

IR1 production configuration declares `RangeScout/1.1 dietrichailabs@gmail.com` as the
SEC contact User-Agent. SEC submissions and the official ticker/CIK mapping are collected
in bounded background workers, with a 0.2-second per-client request floor and a 15-minute
poll schedule. Only mappings needed for the active/watchlist universe are retained locally.
Nasdaq is scheduled no faster than once per 60 seconds. White House and Congress.gov use
conservative 15-minute schedules; Congress.gov is omitted entirely when no BYO key exists.
Previously valid events survive a source failure and persisted data is deduplicated and
bounded to 1,000 sanitized events.
