# Qualified complete component bounds (RG-04)

`ComponentBoundVerifier` is an alternative to the [supplier USD ceiling adapter](provider-bounds.md).
It computes an exact complete upper bound from independently qualified resource
and rate guarantees, using the existing `BudgetedCore` admission/dispatch hooks.
It does not require a supplier to implement CloudyFan's HMAC protocol. The original
verifier is unchanged; there is no automatic downgrade between mechanisms.

**No real supplier configuration is qualified by this delivery.** No production
fee schedules or reviews are bundled. Catalog prices, key credit, `max_tokens`,
source hashes or a reviewer name alone do not establish enforceability. Synthetic
fixtures demonstrate code behavior, not provider compliance.

## Scope and trust boundary

Supported scope is a pinned `Client`/HTTP object/credential, exact configuration
and endpoint, pure text, no tools, plugins, advisor, media, BYOK, implicit paid
features or provider/model fallback. Router prefixes, `~`/`*` aliases and all `:`
variants are rejected, including `:online` and `:free`.

CHAT allows only text system/user/assistant messages, positive operational `max_tokens`, exact
effort, optional string session ID and one exact provider with
`require_parameters: true`. Decisions allows a text JSON state and exactly one
binary choice question; nested media/tool/plugin structures are rejected. This
restrictive scope can also reject otherwise inert domain fields.

The embedding operator supplies trusted evidence/reviews outside task/HTTP input.
Software checks artifact identity, consistency, coverage, expiry and request
conformance; it cannot authenticate the named reviewer or prove supplier behavior.
Real-world proofs still require independent approval before formal qualification.

## Complete fee calculation

All eight components must remain explicit. Schema v1 requires integer
`max_units` and a nonnegative plain decimal USD string `unit_usd` for every
component. Schema v2 additionally accepts an independently guaranteed zero rate
without a quantity cap; an ordinary zero price does not select this route.

| Component | Quantity/control requiring independent evidence |
|---|---|
| `input` | Maximum billed tokens including provider serialization, schema overhead and rejected requests; a context limit on accepted input alone is insufficient. |
| `output` | CHAT's provider-enforced `max_tokens`, or a separately qualified Decisions endpoint output cap. |
| `reasoning` | Included in output budget/rate (zero separate quantity/rate), or separately enforced native cap and rate. |
| `cache_read`, `cache_write` | V1 bounds each by the full input cap. V2 carries each paid dimension's independently qualified cumulative cap; a zero-price input does not make cache free. Reserve both dimensions. |
| `request` | One complete client POST including its maximum fixed fee. |
| `classification` | One binary decision fee for Decisions; explicit zero for CHAT. No duplication of fees already included elsewhere. |
| `failure` | One maximum additional failure surcharge covering all failure/cancellation/upstream-attempt paths; ordinary token/request fees remain reserved. |

```text
U_call = I*P_input + O*P_output + R*P_reasoning
         + I*P_cache_read + I*P_cache_write
         + F_request + is_decision*F_classification + F_failure

V2: U_call = sum(max_units * unit_usd for bounded_quantity components)
             + 0 for each independently guaranteed_zero_rate component
```

Summing exclusive cache branches is deliberately conservative. This is a bound,
not double billing: accounting records provider aggregate fees once. Overlapping
fees need qualified zero separate rates; missing dimensions are never silently zero.

Rates must be **binding effective ceilings**, covering tiers, caching, price
changes, provider defaults and already dispatched requests. Quantity guarantees
must include rejected input and every upstream attempt. A request-price-filter
adapter is not bundled. A catalog price snapshot alone cannot qualify. Unknown
advisor/plugin/other fees invalidate the inventory; an arbitrary multiplier is
not proof of an upper bound.

Component arithmetic is exact. Shared budget sums/products, subtraction,
provisional holds and available balances also ignore the caller's Decimal
precision. Impractical precision fails closed rather than rounding exposure down.

## Artifact and independent review

Each exact schema-version-1 artifact contains:

- `mechanism: qualified_components_v1`, exact `model_version`, CHAT/DECISIONS URL;
- domain-separated `credential_sha256` and full `configuration_sha256`;
- positive `max_payload_bytes` (at most 1 MiB), `output_control`
  (`request_max_tokens` for CHAT; `qualified_endpoint_cap` for Decisions),
  `reasoning_mode` (`included_in_output` or `separate_endpoint_cap`);
- all eight `components`, each with exactly `max_units` / `unit_usd`;
- every `REQUIRED_CLAIMS` assertion once: complete inventory, rejected input,
  enforced output/reasoning, effective price ceilings, no implicit services,
  all upstream attempts/failures, and guarantees surviving expiry/withdrawal
  for previously dispatched requests;
- four source digests: `quantities`, `rates`, `inventory`, `failures`;
- finite Unix `issued_at` / `expires_at`.

### Schema v2: explicit all-path zero-rate evidence

Use `schema_version: 2` with `mechanism: qualified_components_v2`.
Top-level fields, credential/configuration/endpoint/time binding, four global
source digests and whole-artifact independent review are unchanged. Each of the
eight components must have **exactly one** of these shapes:

```text
bounded_quantity:
  {"basis": "bounded_quantity", "max_units": <integer>, "unit_usd": "<decimal>"}

guaranteed_zero_rate:
  {"basis": "guaranteed_zero_rate", "unit_usd": "0",
   "guarantee": {"source_sha256": "<retained supplier guarantee SHA256>",
                 "claims": [<every ZERO_RATE_CLAIMS assertion exactly once>]}}
```

`guaranteed_zero_rate` **forbids** `max_units` (including null or zero): omitted
quantity means no billed-unit bound is claimed, not that usage is zero. Its
plain-decimal rate must parse to exactly zero. A nonzero rate, missing guarantee,
unknown/extra field, incomplete scope or ordinary price snapshot cannot waive a
quantity cap. Conversely, `bounded_quantity` still requires its numeric cap even
when its rate is zero; there is no inference or automatic conversion between
evidence bases. All eight dimensions remain mandatory even when all are free.

Each zero-rate guarantee must explicitly assert every `ZERO_RATE_CLAIMS` item:

- `zero_rate_binds_exact_configuration_endpoint_account_and_validity`;
- `zero_rate_covers_all_usage_success_rejection_failure_cancellation_and_upstream_attempts`;
- `no_separate_or_reclassified_component_charges`;
- `zero_rate_survives_price_changes_expiry_and_withdrawal_for_dispatched_requests`.

These assertions cover all usage/hidden processing for the named component,
all internal/duplicate/child attempts and all failure/cancellation paths. They
must be supported by the retained source and the independent review; writing
the strings and a SHA256 is not proof. `source_sha256` identifies a guarantee,
not a catalog export or the fictional examples in the tests. The whole review
binds every component guarantee; changing one source requires a new review.
The shared registry and artifact expiry still apply, including zero-only proofs.

The global v2 claims are `REQUIRED_CLAIMS_V2`, each exactly once: complete billed
input accounting including serialization/rejection; enforced cumulative caps
for every nonzero-rate dimension; binding effective rates; nonduplicated
classification fees; complete inventory; no implicit features; all upstream
attempt charges; failure/cancellation/in-flight coverage; and guarantees for
already-dispatched requests after expiry/withdrawal. The four global source
digests (`quantities`, `rates`, `inventory`, `failures`) remain mandatory even in
a zero-only artifact. For zero-only evidence, the quantities source can record
why unit caps are unnecessary under the qualified zero-rate guarantees; it does
not require an invented numerical cap. A real reviewer must verify the sources
and all these scope statements; the parser cannot certify their real-world truth.

For bounded components, input/output caps stay positive; request/failure units
stay one and classification units stay one for Decisions, zero for CHAT.
V2 cache caps are independently proved, not inferred from a missing free-input
cap. Separately billed reasoning still needs its own cap/rate. Included reasoning
retains the explicit zero separate quantity/rate convention.

If output uses `guaranteed_zero_rate`, set `output_control: binding_zero_rate`;
otherwise use the v1 endpoint-specific control. If reasoning uses it, set
`reasoning_mode: binding_zero_rate`; otherwise retain `included_in_output` or
`separate_endpoint_cap`. Free output does not imply free reasoning. There is no
cross-component exemption or unlisted fee allowance.

CHAT still sends a positive integer `max_tokens` (at most 2**31), exact reasoning
effort and text-only payload. The token parameter is an operational limit: a
zero-price output does not require proof that the provider enforces a monetary
token cap. For bounded output, dispatch additionally checks `max_tokens` against
the proved cap. Decisions still has no inherited Chat `max_tokens` control.
The 1 MiB local payload limit, fixed provider and all feature restrictions remain.

V1 artifacts and reviews retain their original exact shapes and validation;
zero-rate waivers cannot be smuggled into v1. Mixed v1/v2 schedules are allowed.
The verifier descriptor remains `qualified-components-v1` for v1-only schedules
and becomes `qualified-components-v2` when any v2 schedule is present. Changing
artifacts/reviews changes the frozen descriptor and reservation identity: never
rewrite an old run or resend paid requests to migrate its proof.

Retain source documents in the reviewed evidence package. The verifier does not
download them or infer truth from hashes. Do not invent a source or sign a local
estimate to fill a missing guarantee. Sources may be existing contracts/endpoint
documentation; no supplier-specific new wire protocol is required.

A trusted independent review has exactly `evidence_sha256` (canonical artifact
SHA256), `reviewer`, `reference`, `reviewed_at`, `expires_at`, and
`withdrawal_registry_sha256`. Exactly one review must match; it cannot outlive the
artifact. Unreviewed, changed, incomplete, ambiguous or expired evidence cannot
produce a bound. `bound_for(version)` returns `EnforcedBound(U_call,
"component-cap:<artifact-sha256>")`; admission again rejects a manually supplied
lower bound or different evidence reference.

## Durable withdrawal and exact dispatch

Supply a private shared `withdrawal_root`. Its identity binds both the resolved
path and a durable random incarnation. Create the empty registry first, obtain
`descriptor()['withdrawal_root_sha256']`, then bind the independent review to it.
Despite the descriptor field's `root` spelling, it is an opaque registry identity,
not just a pathname hash. No registry means no qualification.

All processes reusing reviews must share the registry. `revoke(artifact_sha256)`
writes an immutable private withdrawal under a filesystem lock. Reconstructed
verifiers and POST leases check it; even a torn withdrawal record denies by its
presence. A different directory or an empty registry recreated at the same path
does not inherit reviews. Preserve registry history through backup/migration.
Restoring a deliberately older complete registry snapshot remains an operator
rollback risk; local hashes cannot detect it. Never reissue reviews without
checking withdrawal history.

Preflight compares the full frozen profile. Dispatch binds the actual client,
credential, endpoint and complete payload. A one-use exact lease is checked after
durable intent immediately before POST, including expiry after local registry
reads. Registry setup/verification is filesystem-only: no HTTP control-plane call
or model request is made by the verifier.

Denial after intent produces an internal verified `not_dispatched` zero without a
fake generation ID. An unknown POST outcome preserves the shared stop/risk hold.
Known HTTP error fees remain recorded and an interrupted workflow retains its
reservation. Failed-run replay reuses the saved response. An observed breach
creates the existing durable shared violation. Reconciliation replaces the same
event's provisional fee once; it never adds the corrected fee again.

## Workflow integration

```python
from cloudyfan_jev.component_bounds import ComponentBoundVerifier
from cloudyfan_jev.budget import BudgetedCore

verifier = ComponentBoundVerifier(core.client,
    configurations=exact_profiles_by_version,
    schedules=independently_qualified_component_artifacts,
    reviews=trusted_independent_reviews,
    withdrawal_root=private_shared_review_registry)
bounds = {version: verifier.bound_for(version) for version in exact_profiles_by_version}
controlled = BudgetedCore(core, shared_store,
    model_bounds=bounds, tool_bounds={}, verify_bound=verifier)
```

No real approval values or execution authority are supplied by this example.
Shared historical costs, valid authorization, unknown stops and complete workflow
reservation remain required. One-turn tool-free CloudyFan reserves the open-source
API initial generation, two classifiers and the maximum reachable sole repair.
Initial public pass skips classifiers. Higher allowed model-turn counts remain
reserved under the existing [budget contract](budget-admission.md).

Classifier dispatch now uses pinned `decision['version']`, matching admission,
instead of its potentially different alias. Selection policy and decision counts
are unchanged. A historical durable intent containing another alias payload must
fail hash matching; it is never rewritten or resent.

## Offline evidence and remaining qualification

Tests exercise `Core.run`, injected public validation, real budget/receipt stores
and `httpx.MockTransport`: complete selection/repair, proof omissions, arithmetic
beyond 28 digits and at ambient precision 3, text/provider restrictions, fixed
classifier version, withdrawal/recreation, expiry during intent/registry reads,
paid HTTP errors, unknown interruption, breach and idempotent reconciliation.
The suite's socket guard forbids real networking.

Actual final results are in [verification.md](verification.md) and
[offline evidence](evidence/rg04-components-offline.json). Real quantity/rate
commitments, native endpoint behavior, account defaults, actual reviewer identity,
failure billing coverage and live authorization remain unqualified. RG-04 stays
pending; this mechanism does not turn OpenRouter public pricing into those proofs.

The first supplier-qualification priority is pinned **Jev Decisions cumulative
billed input and complete effective fee coverage**: serialization/rejected input,
all upstream retry/failure/cancellation paths, and in-flight price guarantees.
Output/reasoning may use a binding all-path zero-rate guarantee instead of a
numerical token cap, but free output never implies no separate reasoning fees.
Do not inherit Chat's `max_tokens` or promote a catalog snapshot to either proof.
No actual supplier guarantee is supplied by the v2 implementation. Missing proof
for even one chargeable classifier component keeps the repair workflow
unqualified. Any live billing validation requires a separately authorized plan.

V2 regression evidence additionally covers mixed paid/free components, zero-only
workflows, missing guarantees, nonzero-rate waivers, v1 nonconversion, proof
tampering, expiry/withdrawal, precise reservations and receipt reconciliation.
Even for zero-only schedules an interrupted call remains unknown; an observed
positive charge is retained in the durable violation and stops shared dispatch.
This does not fabricate a successful free receipt or discard the breach amount.
See [v2 offline evidence](evidence/rg04-zero-rate-offline.json) for actual results.
