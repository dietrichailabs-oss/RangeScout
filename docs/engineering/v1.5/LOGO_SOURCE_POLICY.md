# RangeScout 1.5.0 logo source policy

Ordered source architecture:

1. project/user-controlled or license-cleared local logo;
2. Finnhub company profile when the user's account is entitled;
3. Twelve Data logo when the user's account is entitled;
4. preserved Logo.dev BYO publishable-key ticker lookup;
5. Wikimedia Commons/Wikidata only with per-asset license and attribution metadata;
6. Simple Icons only where applicable, with source/trademark metadata;
7. project-rendered ticker monogram fallback.

No Google, Yahoo, MSN, company-site, arbitrary image-search, login bypass, anti-bot bypass, or consumer-page scraping is permitted. No shared Dietrich AI Labs provider credential is embedded. Automated tests use deterministic fakes and consume no provider quota.

Downloaded provider image bytes remain bounded session-memory data unless the exact source's current license/terms explicitly permit a local persistent copy. Every successful or failed lookup retains a source identifier, lookup identifier, source URL without credentials, content hash where available, license/source metadata, timestamps, next eligibility, and retry state.

The production resolver checks licensed local files before any network source. Local paths must remain beneath the RangeScout `logos` directory, must not traverse or escape through links/reparse points, must match their recorded SHA-256 when supplied, and must pass bounded image/SVG validation. Finnhub Profile 2, Twelve Data `/logo`, and Logo.dev use only user-supplied credentials and never persist returned image bytes. Twelve Data `/logo` is a two-stage integration: bounded JSON metadata supplies a stock `url`, then only an approved HTTPS Twelve Data host may return a bounded, signature-validated image; the UI shows `Logo: Twelve Data` whenever that source wins. Wikimedia/Simple Icons files are renderable only after a separate license-aware acquisition path records the safe local file and attribution metadata.
