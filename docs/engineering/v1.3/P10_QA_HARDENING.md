# P10 — Full-system QA hardening

Focused provider-fabric/performance gate: `37 passed`.

This includes a 120-symbol concurrent burst with eight bounded provider workers, 24 calling workers, exact per-request symbol identity, 120/120 success, bounded health history, bounded cache, and clean executor shutdown. Existing long-session streaming/reconnect/failure tests also passed.

Complete Windows regression command:

`RANGESCOUT_RELEASE_ROOT=<exact accepted 1.2 portable ZIP> QT_QPA_PLATFORM=offscreen python -m pytest -q`

Result: `322 passed, 1 skipped, 10 subtests passed in 179.83s`.

The skip is the existing interactive Windows Credential Manager session proof when the test process lacks the necessary logon-session capability; deterministic credential storage/redaction tests passed.

Security review found no embedded production credential values. Test-only redaction sentinels remain confined to tests. Legacy credential-shaped setting names remain only in the stripping/migration denylist so old plaintext fields are removed, never enabled or retained. Public deterministic providers remain test-only, and the production registry has no retired provider construction path.
