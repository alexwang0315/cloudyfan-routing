# Operator historical routing observation

The controlled `assemble_monitor` installs `TaskDecisionObserver`; an older host
that constructs `App` without this observer keeps its original trace and gets an
explicit `not_provided` observation. Only authenticated `/monitor/snapshot`
adds `task.operator_observation = {schema_version: "mrc-observation-v1",
task_decision: ...}`. The existing `task.observation` busy/identity marker is
unchanged. No new fields are stored in common traces or returned by `/v1/monitor`.

The observer resolves the original server binding, registered frozen job and
controlled receipt root. It validates the manifest, result identity/hash and
public trace correspondence. It does not call `read_completed` again: the
existing Gateway already verifies a completed result once. Supplemental reads
take an existing Core run lock in nonblocking shared mode. Running, interrupted,
expired, inactive or unavailable histories keep safe binding/accounting metadata
and unknown detail instead of waiting, executing or resuming.

For the versioned backup runner, historical checkpoints must match the frozen
pool, candidate order, actual previous attempt, tool-action prefix, economic
decision and actual dispatched profile. Health observations are checked against
their named immutable snapshot using a read-only SQLite connection; the snapshot
hash, original policy and observation membership must match. This never asks for
the latest health observation, assesses present health, records an event or
recovers a circuit. A damaged supplemental stage is unknown; previously verified
stages and the existing call trace remain visible.
If one named historical health row cannot be verified, independent rows stay
visible with their saved health evidence while selection/economics are unknown.
That does not reconstruct the missing choice or imply dispatch eligibility.

Economic amounts come only from the exact frozen calibration and saved decision.
Historical arithmetic is checked for consistency with that calibration. Only
explicit decimal interval bounds and identity fields are projected; this does
not establish that synthetic/research calibration is real model qualification.
Complete workflow estimates, actual API charges, customer billing and enforceable
fee bounds are separate. Missing evidence stays unknown or not provided. No
public price lookup, model/source call, fee reconciliation or new fee estimate
runs on observation. Original deadlines, authorizations, reservations and
receipts are unchanged. Jev distributions are not correctness probabilities.

`tests/test_history_observation.py` exercises real loopback HTTP with actual
Core/BackupCategoryCore, BudgetedCore, TaskService and HealthStore. The model
boundary is MockTransport. `tests/history_fixture.py` adapts the prepared Core's
existing synthetic evidence at its original timestamp; it creates no real
qualification. `scripts/qa_history_desktop.cjs <new-output-directory>` uses the
installed Chrome and the matching `qa_history_http.py` host. Set
`CLOUDYFAN_CORE_ROOT` to the approved prepared Core. Each run needs a fresh output
directory. Model-call records, screenshots, accessibility snapshots, source
hashes and receipts distinguish this local synthetic acceptance from deployment
or actual supplier/quality/savings evidence.

See [resilient observation](resilient-observation.md) for combined failures,
long-task responsiveness, access boundaries and desktop polling behavior.
