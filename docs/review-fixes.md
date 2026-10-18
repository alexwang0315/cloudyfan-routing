# Full implementation review: five repairs (2026-10-18)

Scope: five confirmed findings from the full implementation review. Repairs are
local and uncommitted on `codex/implement-routing-spec-1`, above baseline
`d8af729668a0e4a2ebe6572d7f50d26ff1db8efa`. Existing retro 3–5 work is preserved.
No paid API, deployment, source push, hardware operation or model download was
performed. Current status is recorded in [handoff.md](handoff.md), rather than
in the historical review results.

## Repairs and regression evidence

| Priority | Problem | Repair | Regression coverage |
| --- | --- | --- | --- |
| P1 | A later above-bound charge rolled back the shared stop | Persist violation and unresolved event in SQLite before raising; retain observed/prior cost and receipt identity | Cross-client stop, restart, pre-reserved workflow, first receipt breach and stale lower replay |
| P1 | Completed provisional charges released their entire risk reserve | Retain `max(bound − provisional, 0)` until verified; recheck global exposure before dispatch | Upward/lower reconciliation, legacy over-admission, genuine pre-migration database and unbounded imported history |
| P2 | Newly created local call directory lacked a durable parent entry before POST | fsync the `calls` parent before writing intent and dispatching | Actual fsync ordering, parent/intent fsync failure stops POST, existing intent never resent |
| P2 | Jev did not receive the pinned execution model version | Require and project `model_version`, validate optional provider identity, bump policy version | Both selector stages change with version; missing/invalid identity rejected; unrelated private fields excluded |
| P2 | Missing calibration rows became a known zero total | Require bound per-arm inventory or explicit nonexecution evidence; preserve unknown totals otherwise | Missing declaration/run, unlisted run, declared nonexecution, binding mismatch and incomplete fee accounting |

Budget failure tests were run before the repair: **6 failed / 1 passed**. The local
and routing regressions initially produced **21 failed / 39 passed**; calibration
report regressions initially produced **5 failed / 15 passed**. Focused green
runs passed, with the final budget subset at **26 passed**, local/routing at
**60 passed**, and report subset at **24 passed**. Additional legacy-database and
calibration cases are covered by the final complete run.

An independent read-only review found no additional blocking defect in the P1
repairs. Its separate SQLite-connection concurrency probe reconciled a provisional
charge while starting already-reserved work: final synthetic spend USD1.0,
available USD0.0, no deadlock or extra reservation. This is accounting logic
validation, not evidence that a real provider enforces a price cap.

## Compatibility and remaining limits

- Policy version is now `hierarchical50-binary-v2`. Frozen v1 workflows are
  rejected before a new POST under this implementation; receipts are preserved.
  This repair does not silently migrate or resend old runs.
- Imported unverified history has no enforceable bound and blocks new dispatch
  until independently reconciled. This includes local zero-API-fee historical
  receipts; local backends cannot use cloud GET reconciliation. Managed zero-bound
  local calls remain supported. See [budget-admission.md](budget-admission.md).
- A recorded cap violation remains a shared stop. There is no automatic recovery
  or reset API; a separately reviewed account-recovery procedure is still needed.
- Missing calibration inventory means unknown calibration/overall API cost. Known
  charges remain visible; holdout-only paired metrics keep their explicit scope.
  `evidence_ref` identifies an extraction assertion, not a signed proof of truth.
  See [comparison-reports.md](comparison-reports.md).
- fsync ordering is checked with fault injection; actual sudden-power-loss/storage
  behavior is not qualified. Real provider caps, model quality, third-party
  clients and the ten production gates remain unverified.

## Evidence locations

The final complete offline check is summarized in
[evidence/review-fixes-offline.json](evidence/review-fixes-offline.json), including
the executable/test/build source digest and private command-receipt hash.
[verification.md](verification.md) records the observed final test count.

Private workspace evidence remains under `../.scratch/review-five-fixes/`:
`budget-red.log`, `budget-green.log`, `budget-integration.log`,
`local-routing-red.log`, `local-routing-green.log`,
`local-routing-integration.log`, `report-red.log`, `report-green.log`,
`budget-review.md`, and `integration-check.log`. The independent concurrency probe
and final source inventory are kept there too. Historical review and pre-fix
manifests remain under `../.scratch/all-implementation-review/`; they are not a
claim that the repaired tree still has those defects.

## Local commit follow-up

The historical repair verification above was recorded before commit. The user
subsequently authorized a local Git commit of the repairs and their reviewed
retro 3–5 verification dependencies. A fresh complete check again passed 699 tests
with the identical source digest; see
[evidence/review-fixes-precommit.json](evidence/review-fixes-precommit.json).
Git history identifies the local commit. Original private runtime evidence stays
outside Git; no push, deployment or paid API was performed.
