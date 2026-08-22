# P4 — Additive SQLite expansion and instrument discovery

Schema version 2 is additive and transactional. Existing `instruments` and `ohlcv_bars` remain untouched; expanded tables use an `rs_` namespace to avoid competing with the released history schema. Foreign keys are enabled, migrations are versioned, and every DDL statement plus schema-version update runs in one rollback-capable transaction.

The expanded schema covers canonical instruments and aliases, provider/capability/symbol metadata, bounded health aggregates, discovery sources/runs/diffs, futures, crypto, options-ready metadata, bounded market cache, normalized company/fundamental storage, and maintenance scheduling. Credentials are not represented anywhere in SQLite.

Official-directory discovery supports on-demand import, a weekly due policy, nonblocking startup-compatible execution, normalized snapshot hashes, additions, inactivation instead of destructive deletion, metadata changes, symbol changes with historical aliases, parse-error accounting, and transactional rollback.

Focused gate: `24 passed`.

The gate proves populated schema-v1 preservation, idempotence, integrity/foreign-key checks, failed-migration rollback, initial/identical/new/inactive/symbol-change snapshots, alias retention, weekly due/not-due behavior, nonblocking scheduling, malformed row handling, Unicode/control-character safety, and futures/crypto/options-ready domain behavior.
