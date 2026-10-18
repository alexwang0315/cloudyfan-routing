# Read-only experiment comparisons

`cloudyfan_jev.reports.build_report(plan, evidence)` consumes the frozen T10 plan
and versioned accounting/outcome evidence. `render_markdown(report)` returns a
compact table; JSON is the full audit output. These functions perform no network
I/O, create no experiments, invoke no grader, and modify neither input nor source
artifacts. They use `verify_plan_integrity`, **not** dispatch freshness checks, so
an expired plan can still be reported. A valid archive hash grants no execution
permission and proves neither truthful evidence nor semantic absence of ambiguity.

## Evidence schema v1

The caller's trusted extraction process supplies the following envelope. Unknown
fees/timing/outcomes stay null. USD values and milliseconds are decimal strings,
never binary floats.

```json
{
  "schema_version": "cloudyfan-report-evidence-v1",
  "plan_id": "hash from the frozen plan",
  "source": {"id": "private-ledger-reference", "version": "extractor-v1", "kind": "synthetic"},
  "annotations": {
    "posthoc": false,
    "caching": "disabled",
    "zero_model_fee_scenario": false,
    "limitations": []
  },
  "runs": [{
    "run_id": "cloudyfan-task-1", "arm": "cloudyfan", "task_id": "task-1", "cohort": "holdout",
    "binding_sha256": "report_binding(plan, arm, task_id)[binding_sha256]",
    "first_pass": false, "final_pass": true, "status": "passed",
    "accounting_complete": true,
    "timing": {"end_to_end_ms": "500", "validation_ms": "20", "queue_ms": "30"}
  }],
  "events": [{
    "event_id": "cloudyfan-task-1:classification-1", "run_id": "cloudyfan-task-1",
    "kind": "classification", "attempt": null, "retry": false,
    "cost_usd": "0.001", "receipt_id": "provider-qualified-receipt-id", "verified": true
  }]
}
```

This schema fragment illustrates fields, not sufficient evidence for a successful
run. Passed/failed/unverifiable runs need actual generation events. Outcome
statuses are `passed`, `failed`, `unverifiable`, `engineering_interrupted` or
`unknown`. `final_pass` must agree with the status. First/final checks come from
the independent public-contract evidence; the reporter never derives pass from
nonempty output, spend, score or provider success. The report has no feedback path
to selection or repair and accepts no private answer/grader payload.

`report_binding` hashes the plan, task/cohort input identity, routing configuration,
limits, public validation, diagnostic and repair contracts. Its return is an
**archive identity**, not an execution binding/permission. The extractor must
verify the actual run manifest against these fields before supplying its hash;
merely copying the expected hash cannot prove that execution used the settings.
Invalid binding/outcome evidence becomes unknown and is excluded from paired
quality claims while its spending remains visible.

Every run appears once for each `(arm, cohort, task_id)`. Repetitions require their
own frozen task/repetition plan; silently picking one of multiple runs is refused.
Unexecuted tasks remain in pass-rate denominators, and their unknown flow costs
are not filled with zero. Calibration costs are separate from holdout. Calibration
rows record observed calibration work only; an empty calibration list is not proof
that no tuning cost occurred. Missing calibration inventories make calibration
and overall complete totals null, while observed/verified fees remain visible.
Holdout costs and CDR remain separately scoped holdout metrics, never a total
experiment cost including calibration.

### Calibration inventory and nonexecution

An optional `calibration_accounting` object supplies a declaration **per arm**:

```json
{
  "calibration_accounting": {
    "fixed_high": {
      "status": "not_run", "run_ids": [],
      "evidence_ref": "extractor-audit-reference-for-nonexecution",
      "binding_sha256": "calibration_binding(plan, 'fixed_high')"
    },
    "cloudyfan": {
      "status": "complete", "run_ids": ["cloudyfan-calibration-1"],
      "evidence_ref": "complete-calibration-inventory-reference",
      "binding_sha256": "calibration_binding(plan, 'cloudyfan')"
    }
  }
}
```

`calibration_binding(plan, arm)` binds the plan, arm and every frozen calibration
task's configuration/input/contract identity. It does not impose a calibration
task-by-arm Cartesian product. The trusted extractor must audit **all earlier
calibration work attributed to this plan and arm** and enumerate its actual run
IDs, including failed work. Each run still needs its own valid `report_binding`,
events and `accounting_complete` assertion. The reference points to the audit
supporting the inventory or confirmed nonexecution; it is not fetched, and the
hash/reference is neither authentication nor proof the declaration is true.

- Omitted arm declarations mean unknown calibration coverage, even if no rows
  exist or all supplied rows have known prices. No cost-zero inference is made.
- `complete` requires a nonempty unique run inventory. Missing inventory rows or
  observed rows omitted from that inventory make the arm's complete fee unknown;
  observed fees remain counted. Partial/incomplete run accounting or unknown
  event fees also keep the complete total unknown.
- `not_run` requires an empty inventory and no observed calibration runs for that
  arm; conflicting evidence raises an error. Only this explicit bound assertion
  permits a known zero when no work was performed. A false declaration cannot be
  discovered from missing records alone; the extractor is a trust boundary.
- Unknown arm names, invalid cohort bindings or missing evidence references are
  rejected. `coverage` retains declaration status, reference and missing/unlisted
  run IDs; `accounting_status` is complete only when all per-arm fees are known.
  The legacy `incomplete_workflows` counter includes one coverage-unknown marker
  per undeclared arm (or unlisted inventory), plus missing/unfinished run counts;
  it is not an estimate of how many unrecorded calls actually occurred.

Unlinked events still retain their costs and anomalies under `unattributed_cost`;
they cannot substantiate any calibration completeness assertion.

`accounting_complete=True` is a trusted extraction assertion that **all** model,
classifier, tool, failed, retried and repaired events for that run are present.
Absent/false means a complete fee is unavailable even if every included event has
a price. Tool absence cannot be inferred from a model-only ledger. `verified=True`
means independently reconciled evidence; provisional API usage costs remain known
but not independently verified. These flags are not authenticated signatures.

## Reconciliation and duplicates

A reconciliation is an event with `kind="reconciliation"`, its own `event_id`,
`replaces=<original event ID>`, the same `run_id`/receipt identity, verified true and
a decimal `cost_usd`. It replaces the original provisional amount; it is never a
second charge. Unknown original amounts may become known after reconciliation.
Conflicting verified receipts, orphan reconciliations and duplicate identities
remain anomalies. Identical event replays are idempotent. Distinct events with the
same receipt retain one known charge and an unknown duplicate instead of charging
the receipt twice. IDs must be stable and scoped to provider/tool source.

Unlinked fees appear under `unattributed_cost` and in overall costs; they block
complete savings claims. The input source ID/version and evidence audit retain
provenance without exposing prompts, answers, credentials or receipt bodies.

## Cost, quality and timing interpretation

- Per-arm rates use **all planned holdout tasks**, including missing evidence.
  Model failures, unverifiable outcomes, engineering interruptions, unknown and
  missing evidence are separate counts. Every failed/interrupted run's charges
  and available timing remain in its arm's report.
- Complete API USD includes every generation turn, classification, retry, repair
  and external tool charge, including failed calls. `cost_per_success_usd` divides
  this whole-arm total by final successes; null cost or zero successes is
  undefined. It never substitutes for the whole-batch pass rate.
- Pairing requires matching task identity and valid frozen evidence, regardless
  of success. Numeric CDR is **spending difference** until the quality gate passes.
  Missing/unmatched work, calibration and unattributed costs remain separate.
- Aggregate CDR = `100 * (sum baseline - sum candidate) / sum baseline` over the
  same paired full workflows. Per-task CDR uses each task's own baseline denominator.
  Its mean, median, minimum, maximum and expensive fraction are distinct metrics.
  Positive means lower candidate spending. Zero baseline/unknown cost is undefined.
  Expensive fraction divides by the number of defined per-task CDRs, not all tasks.
- A claim of quality-gated savings requires every planned task represented,
  both arms meeting the preregistered final pass threshold, no observed pass-rate
  decrease, no evidence anomalies, known paired costs and positive CDR. This is a
  descriptive gate, **not** statistical equivalence or a commercial proof.
- Synthetic, posthoc, zero-model-fee and cache-affected analyses retain explicit
  labels. A zero-fee scenario is a separately supplied counterfactual evidence
  envelope; it must not overwrite billed receipts or an actual-cost report.
  Synthetic results never become empirical savings. Caching is `disabled`,
  `enabled`, `mixed` or `unknown`; no cache correction is invented.
- End-to-end, validation and queue timing each have mean, median, observed sample
  count and missing count. Values cover the complete flow, including failed work.
  No missing queue or validation duration is assumed zero. Repair rate is runs
  with a generation event in attempt 1 divided by planned tasks; call counts also
  expose classifier, generation, tool and retry volume.

Schema v1 keeps `open_source_fixed_low` **plan-only** because T10 has no execution
adapter for it. `jev_attribution.identified` therefore remains false. Comparing
CloudyFan against full-high or an OSS→high fallback measures whole-workflow
differences, not isolated Jev gain. The future control must match first model,
inputs, public validation and repair contract, and include its full charges.

This implementation does not rewrite the historical 25-task analysis: three task
contracts were ambiguous; the common 22-task result was a nonblind posthoc
sensitivity analysis. It provides no automatic legacy-data adapter and makes no
new empirical cost or quality claim. Any imported historical envelope must retain
those limitations and provenance rather than reclassifying it as prospective.

Worked synthetic outputs (not paid experiment results): [Markdown](../examples/synthetic-comparison-report.md) and [JSON](../examples/synthetic-comparison-report.json).
