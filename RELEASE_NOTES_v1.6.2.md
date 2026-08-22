# RangeScout 1.6.2 Engineering Notes

RangeScout 1.6.2 is an Engineering candidate routed to Independent QA. No QA or release approval is claimed.

This candidate preserves the RangeScout 1.6.1 reserved quote lane and strict forced-provider behavior. It adds local ranked company/instrument search, active-versus-unavailable provider grouping, immediate provider credential synchronization, semantic financial formatting, source-attributed catalysts and eligible Finnhub company news, watchlist action feedback, human-readable market alerts, progressive scanner aggregation and filters, cached-first chart range switching, provider-aware analyst states, previous-close fusion with provenance, and reliable local Notes CRUD/reload/category/persistence behavior.

No public simulated-data provider or deferred brokerage provider is exposed. No shared credential is embedded. Network work remains off the Qt UI thread.
