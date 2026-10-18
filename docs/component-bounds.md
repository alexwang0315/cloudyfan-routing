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

CHAT allows only text system/user/assistant messages, bounded `max_tokens`, exact
effort, optional string session ID and one exact provider with
`require_parameters: true`. Decisions allows a text JSON state and exactly one
binary choice question; nested media/tool/plugin structures are rejected. This
restrictive scope can also reject otherwise inert domain fields.

The embedding operator supplies trusted evidence/reviews outside task/HTTP input.
Software checks artifact identity, consistency, coverage, expiry and request
conformance; it cannot authenticate the named reviewer or prove supplier behavior.
Real-world proofs still require independent approval before formal qualification.

## Complete fee calculation

All eight components must explicitly provide integer `max_units` and a
nonnegative plain decimal USD string `unit_usd`:

| Component | Quantity/control requiring independent evidence |
|---|---|
| `input` | Maximum billed tokens including provider serialization, schema overhead and rejected requests; a context limit on accepted input alone is insufficient. |
| `output` | CHAT's provider-enforced `max_tokens`, or a separately qualified Decisions endpoint output cap. |
| `reasoning` | Included in output budget/rate (zero separate quantity/rate), or separately enforced native cap and rate. |
| `cache_read`, `cache_write` | Each conservatively bounded by the full input cap; reserve both fee dimensions. Zero rates require evidence of inclusion/exclusion. |
| `request` | One complete client POST including its maximum fixed fee. |
| `classification` | One binary decision fee for Decisions; explicit zero for CHAT. No duplication of fees already included elsewhere. |
| `failure` | One maximum additional failure surcharge covering all failure/cancellation/upstream-attempt paths; ordinary token/request fees remain reserved. |

```text
U_call = I*P_input + O*P_output + R*P_reasoning
         + I*P_cache_read + I*P_cache_write
         + F_request + is_decision*F_classification + F_failure
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

The first supplier-qualification priority is the pinned **Jev Decisions billed
quantity envelope**: native output/reasoning limits, billed input including
serialization/rejected requests, and cumulative units across internal retries,
failure and cancellation. Its current request does not carry Chat's `max_tokens`;
do not inherit a Chat cap. Obtain a binding guarantee covering in-flight requests
and a complete fee inventory before attaching effective rate ceilings and doing
any separately authorized live billing validation. An unbounded classifier keeps
the whole repair workflow unqualified, even if generation endpoints are bounded.
