# P13 — Independent QA R2 HOLD correction

- Reviewed HOLD build: `rs-v1.3.0-expanded-p11-eng2`
- Corrected engineering build: `rs-v1.3.0-expanded-p11-eng3`
- Scope: `QA-RS130-010` only plus directly required staged-source regression plumbing.
- Public release: blocked pending a new Independent QA verdict.

The stale Alpaca-removal integration test now resolves the current release notes from `PRODUCT.version`. Clean source staging continues to exclude superseded v1.1 notes. Packaging now fails closed when a shipped test reads a tracked repository file omitted from staged source, and the staged-source verifier can execute the complete supported suite from an extracted final Source ZIP with the exact packaged runtime supplied as its release fixture.

QA-RS130-001 through QA-RS130-009 remain closed and unchanged.
