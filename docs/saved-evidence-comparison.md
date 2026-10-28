# Saved original-policy values

Issue42 adds `reproduce_original_policy(values: dict) -> dict` in
`cloudyfan_jev.saved_policy_comparison`. One call reapplies **one saved decision
point**. It does not execute a task, validate an answer, refresh evidence, qualify
a candidate, authorize dispatch, or reconstruct an unobserved later branch.
The host retains actual routing evidence separately and compares it with the
computed decision. There is no `actual_selected_tier` input.

The trusted host resolves identities and reads the original binding, registered
configuration, manifest, checkpoints, receipts and historical gate evidence.
It verifies their original scope and hashes before constructing these values.
Hash-shaped strings in this interface are references, not authentication. A
browser-supplied dictionary cannot establish authority by passing validation.
The comparator accepts JSON primitives only; it has no service, runner, client,
path, factory, clock callback, network, ledger, tool or validator dependency.

## Input contract

The exact top-level keys are `schema_version`, `policy_version`, `mode`,
`source`, `context`, `candidates`, `economics`, `classifier`.

- `schema_version`: `saved-policy-values-v1`.
- `policy_version`: `hierarchical-sequential-v1`, `hierarchical-qcs-v1`, or
  `qcs-backup-policy-v1`. CategoryCore uses its saved `hierarchical-qcs-v1`
  descriptor; its category/matrix binding is verified by the host.
- `mode`: `cloudyfan`, `fixed_high`, `low_first`, `open_source_first`.
- `source`: exactly `binding_sha256`, `request_sha256`, `registered_sha256`,
  `policy_sha256`, `checkpoint_sha256`, `candidate_set_sha256`. The first five
  require original lowercase 64-hex references, or null to express unavailable
  original evidence. Candidate-set hash may be null only for an empty list.
- `context`: exactly `stage`, `previous_tier`,
  `previous_configuration_sha256`, `previous_public_passed`, `previous_stop`,
  `quality_phase`, `generation_calls`, `receipt_sha256`, `validation_sha256`.
  Stage is `initial`, `same_pool`, `low_mid`, `mid_high`, `after_validation`.
  Previous tier is null or one of `open_source`, `low`, `mid`, `high`;
  passed is strictly true/false/null. Stop is `none`, `validation_passed`, `validation_failed`,
  `validation_unknown`, `provider_rate_limited`, `provider_unavailable`,
  `unknown_charge`. Quality phase is integer0..2; generation calls integer0..128
  (Backup retains its original global3/2 bound). Hashes are lowercase64-hex or
  null. Initial requires no previous context, phase/calls0 and stop `none`.
- `candidates`: at most12 records, each exactly `tier`, `config_id`,
  `configuration_sha256`, `generation_cost_usd`, `complete_cost_usd`,
  `flow_ms_upper`, `quality_score`, `gate`. IDs are ASCII identifiers up to160
  characters, not URLs/paths. Cost intervals have exactly decimal-string
  `lower`/`upper`. `quality_score` is a signed decimal string or null, used only
  by singleton QCS's original tie-break; Backup ignores it.
  For ordinary `hierarchical-sequential-v1`, cost intervals and flow may be null:
  its one frozen profile per tier is not chosen by forecasts. The host must not
  invent forecast costs from actual billed amounts. QCS/Backup eligible rows
  require all three numerical fields to reproduce their ordering.
- `gate`: exactly `state`, `reasons`, `evidence_sha256`. State is
  `eligible`, `excluded`, `unknown`; reasons are up to8 ASCII codes up to80
  characters. Eligible requires no reasons and a verified original gate evidence
  hash; excluded requires reasons and an evidence hash. Unknown or missing
  evidence prevents reproducing a choice. These are host-verified saved gate
  inputs, **not a new claim that dispatch is presently authorized**.

`candidate_set_sha256` is SHA256 of canonical JSON of all candidate identity
objects `{tier, config_id, configuration_sha256}`, sorted by those three values.
Canonical JSON uses UTF-8, sorted keys, no whitespace, and rejects NaN. The host
derives the expected set from the original complete relevant pool(s), not the
subset it happened to load. A missing candidate cannot silently alter the rank.
`candidate_set_digest(candidates: list) -> str` computes this hash after bounded
shape validation; its output does not verify that the host supplied a full set.
Selection points require this hash even for a genuinely empty original pool;
only a stop point which does not examine candidates may omit it.

All decimal strings are plain notation, at most128 characters, exponent no less
than-64 and magnitude below1e65. Monetary/time inputs are nonnegative;
probability/saving fractions are at most1. Arithmetic uses precision512.
Whole input is at most131072 UTF-8 bytes,8192 JSON nodes, depth12 and128 items
per JSON array. Output is at most16384 bytes. These structural bounds do not
claim a hard wall-clock deadline; the host owns comparison-level scheduling and
time limits without giving a callback to the pure comparator.

## Initial Backup example

This example shows the exact literal DTO structure. Its repeated example hashes
stand for verified original references; the candidate-set hash is the actual
digest of this example's identity list, not evidence of a real task.

```json
{
  "schema_version": "saved-policy-values-v1",
  "policy_version": "qcs-backup-policy-v1",
  "mode": "cloudyfan",
  "source": {
    "binding_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "request_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "registered_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "policy_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "checkpoint_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "candidate_set_sha256": "f8cfe0ce56085f45fb58cd0be6e156de96280fd891ca4ff31485d52e502782d3"
  },
  "context": {
    "stage": "initial",
    "previous_tier": null,
    "previous_configuration_sha256": null,
    "previous_public_passed": null,
    "previous_stop": "none",
    "quality_phase": 0,
    "generation_calls": 0,
    "receipt_sha256": null,
    "validation_sha256": null
  },
  "candidates": [{
    "tier": "open_source",
    "config_id": "example-oss-primary",
    "configuration_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "generation_cost_usd": {"lower": "0.01", "upper": "0.01"},
    "complete_cost_usd": {"lower": "0.01", "upper": "0.02"},
    "flow_ms_upper": "1000",
    "quality_score": null,
    "gate": {
      "state": "eligible",
      "reasons": [],
      "evidence_sha256": "1111111111111111111111111111111111111111111111111111111111111111"
    }
  }],
  "economics": null,
  "classifier": null
}
```

For the first real TaskFeaturesHTTP fixture, project all original OSS pool rows
and their saved health/capacity/qualification gate evidence from
`backup-selection-0.json`. The comparator recomputes minimum complete-cost
upper, then flow upper, then config ID. It does not copy checkpoint `pair`.
An independently saved public pass is a second `after_validation` point with
the original receipt and validation hashes; it computes stop without loading
any alternative answer or another candidate pool.

## Economics and classifier values

For a QCS repair stage, `economics` has exactly `calibration`, `contract_sha256`,
`expected_contract_sha256`, `direct_tier`, `paths`, `classified_paths`,
`classifier_cost_usd`, `lower_probability`, `classify_flow_ms_upper`,
`max_flow_ms`, `minimum_saving_usd`, `minimum_saving_fraction`.
Calibration is `missing`, `present`, or `unknown`. The two contract hashes are
null or original64-hex values. `direct_tier` is a stage tier or null. Each path
is exactly `{cost_usd: {lower, upper}, flow_ms_upper: decimal}`. Classified paths
are a path mapping or null; classifier fee/probability are intervals or null;
classified time is a decimal or null. The three policy limits are decimal strings.
`paths` may be null for a Backup policy's missing exact variant: that original
branch uses each candidate's saved complete cost and flow upper instead.

Singleton QCS preserves its original cost/flow/preference/tier ordering,
single-eligible and missing-estimate reasons, conditional-cost evidence,
strict saving threshold and classified-flow check. Backup preserves its exact
variant requirement and cost/flow/tier ordering; absent original variant means
`exact_classifier_calibration_missing`, not proven uneconomic. Contract
mismatches or unknown source availability produce unknown, never a fallback
manufactured from current prices or active policy.

`classifier`, when supplied, has exactly `stage`, `contract_sha256`,
`expected_contract_sha256`, `request_sha256`, `expected_request_sha256`,
`response_sha256`, `receipt_sha256`,
`context_sha256`, `expected_context_sha256`, `cost_known`, `parse_status`,
`reported_choice`, `probabilities`, `abstain_reason`, `probability_decimals`.
The host verifies the original receipt's model/provider/identity and request
scope before extracting these fields. Stage must match the economic decision;
All eight hashes must be present and paired hashes equal. `cost_known` is a
strict boolean. Parse status is `valid` or `invalid`; reported choice is null or
a stage tier; probabilities are exactly the two stage-tier decimal strings or
null. Abstain reason is null, `distribution_missing`, or
`explicit_abstention_choice`, matching the original Decisions parser.
Probability decimals is null or integer0..18, taken from the original verified
wire contract. The original normalization tolerance is `0.000001` plus
`10 ** -probability_decimals` when that precision was declared; it is not an
extra economic threshold or a correctness probability.

Invalid parsed response, abstention, or chosen probability<=0.5 reapplies the
original upper-tier fallback. Valid probability>0.5 uses the reported choice.
This is **reapplication of saved classifier-output rules, not rerunning Jev**.
Missing receipt/context binding leaves the selected tier unknown even when the
economic classify decision can be reproduced. Original unknown fee stops;
comparison never infers a zero fee or retries it.

## Result and missing-data semantics

Result fields are `schema_version`, `computation_version`, `status`, `scope`,
`execution_authorized`, `policy_version`, `input_sha256`, `stage`, `action`,
`selected_tier`, `selected_configuration_sha256`, `reason`, `policy_reason`,
`net_saving_lower_usd`, `classifier_interpretation`.
Status is `reproduced`, `partial`, or `unknown`; scope is always
`one_saved_decision`, execution_authorized always false. An economic classify
gate with missing parsed evidence is partial; no invented effective selection
is emitted. `input_sha256` binds the bounded copied JSON values. Original costs
remain the host's separate authoritative budget/billing values; forecasts here
cannot become billed cost, savings, correctness probability or a hard fee cap.

Stable unavailable reasons include `saved_policy_values_invalid`,
`unsupported_policy_version`, `original_binding_unavailable`,
`original_context_unavailable`, `candidate_scope_incomplete`,
`candidate_gate_evidence_unavailable`, `original_economics_unavailable`,
`classifier_evidence_unavailable`, `classifier_binding_mismatch`,
`policy_inputs_inconsistent`, `original_unknown_charge`.

At original expiry, the host supplies only legally retained safe metadata.
Without safely retained original policy/gate values it returns unknown; it must
not reopen prompt, answers, intent payloads or expired checkpoints to fill this
DTO. Existing actual traces and original known/unknown financial values may
still be reported separately. Report GET/poll reads saved results without
calling this function again. No Issue43/44 alternative-policy/result matching
is part of this module.

Current handoff: the standalone source is written in the isolated policy-values
candidate. Main loopback acceptance and affected full gates remain required.
This worker starts no product tests, collection or services; the normal commit
uses the unchanged shared secret/quick hooks after the UI worker has quit.
Hook receipts record their own limited checks. The interface example and a
candidate commit are not a completed Issue42 acceptance or qualification.
