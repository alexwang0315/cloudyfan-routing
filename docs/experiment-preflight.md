# Experiment preflight and frozen plans

This module prepares reviewable experiments. It does not run paid experiments,
choose a new sample size, approve real models, or grant spending authority.
`paid_execution_authorized` is always `false`, including for a reviewed plan.
All fixture approvals are synthetic and cannot become real qualification.

## Public API

`cloudyfan_jev.experiments` provides:

- `build_plan(spec, qualification_pack, review, now=...)`: returns a canonical
  `cloudyfan-experiment-plan-v1` document with `plan_id`, stable blockers, and
  `status` of `blocked`, `synthetic_ready`, or `reviewed`.
- `verify_plan(plan, now=..., allow_synthetic=False)`: checks integrity, readiness,
  synthetic restrictions, routing implementation version, snapshot validity and
  authorization expiry. It does not reserve money or dispatch requests.
- `write_plan` / `read_plan`: private immutable JSON storage with the same checks.
  A different plan cannot replace an existing file. A revised review requires a
  new plan ID/path; original evidence remains unchanged.
- `execution_binding(plan, arm, task_id, now=..., allow_synthetic=False)` returns
  frozen Core inputs for a reviewed holdout task and an existing mode.
  `verify_binding(plan, binding, ...)` checks that the actual selected inputs have
  not changed before dispatch. Caller execution must still use the budget,
  privacy, registered validator and transport controls.
- `verify_plan_integrity(plan)` is for historical reporting. It checks only
  schema/hash integrity, with no start permission or freshness claim. This lets
  expired plans remain auditable without allowing new dispatch.

Malformed input structures raise before execution; review gaps and unqualified
pools produce a blocked plan with stable reason codes. The module has no network
or model execution path.

## Inputs and required review

`spec` freezes these fields:

| Field | Meaning |
|---|---|
| `synthetic` | Explicit boolean; synthetic qualification requires this flag |
| `tasks`, `calibration_tasks` | Ordered rows with `task_id`, difficulty `stratum`, public `task`, `input_sha256` |
| `requirements` | One domain, capability/context/provider constraints |
| `baseline_config_id` | Explicit highest-score eligible model configuration |
| `decision_profile` | Pinned Jev model, version and provider identity |
| `system`, `contract_version`, `diagnostic_contract` | Common prompt and versioned public validation/repair inputs |
| `limits`, `max_generation_attempts` | Frozen Core limits and exactly two generation stages |
| `repair_policy` | `one-public-diagnostic-repair-v1` |
| `arms` | Explicit selected subset of the five supported plan labels; includes fixed-high baseline |
| `budget` | Decimal-string cap/block upper reserve, authorization ID, expiry, enforcement evidence reference |
| `quality_gate` | Explicit decimal-string `required_final_pass_rate` |
| `design_owner`, `calibration_owner`, `grader_owner` | Independent grading responsibility; grader differs from both other owners |

Task input hashes use `scores.catalog.digest(task)`. Task payloads contain public
inputs or input fingerprints, never hidden answers, private checks, or other-arm
answers. Cohorts need distinct task IDs and cannot contain identical task hashes.
Equivalent reworded tasks, data leakage, and semantic ambiguity require human
review; exact-hash checks cannot detect all such cases.

`review.input_sha256` binds `digest({"spec": spec,
"qualification_pack_id": qualification_pack["pack_id"]})`. Each of `plan`,
`isolation`, `grading`, and `decision_profile` requires an approved status,
reviewer, nonfuture review timestamp, and nonempty evidence references. Every
holdout **and calibration** task additionally requires a matching input hash and
an empty list of unresolved ambiguities. The application must ensure the named
reviewers actually completed the review; these are attestations and hashes, not
authenticated signatures or automatic proof of unambiguous questions.

The qualification pack is promoted using T02 `qualified_snapshot`; stale,
blocked, or improperly promoted synthetic qualification cannot start a plan.
The derived snapshot and exact model/effort/provider profiles are embedded in
and hashed with the plan. The highest baseline is selected by the **maximum
native score among eligible qualified configurations**. Explicit ties are
allowed. It remains separate from CloudyFan's cost-ranked candidate within the
high pool, which can be cheaper and lower scoring.

`budget.next_block_upper_usd` must be positive and no greater than the stated
cap, and expiry/enforcement references must be present. These are reviewed plan
facts, **not proof of an enforceable provider cap or an actual reservation**.
T04 must independently validate current authorization, unknown charges,
remaining budget and an enforceable full-block bound immediately before any
paid action. Estimates, tiers and token limits never become caps here.

## Arms and attribution

| Plan arm | First generation | Failure path | Executable binding |
|---|---|---|---|
| `fixed_high` | Explicit highest baseline | Same baseline, one repair | Existing Core mode |
| `low_first` | Qualified low candidate | Explicit highest baseline | Existing Core mode |
| `open_source_first` | Qualified open-source candidate | Explicit highest baseline | Existing Core mode |
| `cloudyfan` | Same qualified open-source candidate | Low/mid then optional mid/high Jev; one repair | Existing Core mode |
| `open_source_fixed_low` | Same qualified open-source candidate | Fixed low candidate, no Jev | **Plan only** |

The last arm is listed in `planned_only_arms`; requesting its execution binding
raises instead of fabricating execution evidence. Its executor and any real
comparison require separately scoped work. Recording this planned control does
not establish Jev's individual gain. Attribution requires matched input,
first-answer configuration, diagnostic/repair contract and full billed attempts.

## Downstream report linkage

Each actual result should reference `plan_id`, `task_id`, `arm`, input hash, run
ID, and all ledger events. The plan preserves order, calibration membership,
strata, quality gate, snapshot and qualification identifiers. Missing results
remain not executed; engineering and accounting interruptions are not scored as
correct or wrong. Historical reporting must preserve failures, unknown costs,
calibration and unpaired expense. A planned-only arm must not be reported as
executed merely because it appears in the plan.

`tests/test_experiments.py` supplies a fully synthetic review example and a
`Core.run`/MockTransport matrix for open-source pass, low/mid/high repair,
engineering stop and unknown-accounting stop. These cases prove offline
orchestration and preflight checks only, not real model qualification, savings,
accuracy, ambiguity-free human tasks, or production readiness.
