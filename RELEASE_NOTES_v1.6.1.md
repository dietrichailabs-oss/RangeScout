# RangeScout 1.6.1 Engineering Notes

RangeScout 1.6.1 is an Engineering candidate routed to Independent QA. No release approval is claimed.

- Reserves interactive quote capacity inside the production provider fabric so blocked history work cannot starve a newly selected symbol.
- Coalesces rapid symbol changes and propagates cancellation for superseded quote requests while retaining the generation-based stale-result guard.
- Applies quote-specific network timeouts while preserving longer historical-data budgets.
- Preserves RangeScout 1.6.0 provider modes, local-first behavior, UI, storage, system tray, and security controls.
