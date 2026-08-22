# RangeScout company master database

`resources/RangeScout_Company_Master.sqlite` is a small versioned baseline of stable issuer and listing identity. It contains no quotes, historical prices, analyst payloads, credentials, or proprietary provider responses.

The initial seed uses public factual identity from SEC issuer metadata and official exchange listing references. On startup RangeScout additively merges missing rows into the writable AppData database. It never replaces a newer user database or overwrites an existing normalized instrument row. Weekly/monthly discovery remains the incremental source of additions, inactivations, symbol/alias changes, and venue changes.

The seed exists to make common symbols searchable and identifiable offline on first launch. Network providers refresh changing data asynchronously.
