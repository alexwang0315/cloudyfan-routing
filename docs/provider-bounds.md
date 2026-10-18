# Supplier fee enforcement (RG-04)

`OpenRouterBoundVerifier` is a fail-closed adapter for `BudgetedCore`. It verifies
an independently qualified supplier ceiling statement, current signed enforcement
state, exact client/credential/endpoint and request policy. No supplier contract,
issuer key, qualified control-plane probe or production credential is bundled.
**Ordinary paid OpenRouter execution remains denied by default.**

An independent [qualified component verifier](component-bounds.md) is also
available for fixed tool-free text endpoints. It derives a complete bound from
independently reviewed quantity/rate guarantees without inventing this HMAC
protocol upstream. It has no bundled real approvals and does not weaken default
denial or automatically switch between evidence mechanisms.

## Why key budgets are insufficient

OpenRouter's [official limits documentation](https://openrouter.ai/docs/api_reference/limits)
says billing settles after completion. In-flight holds estimate token costs and
exclude plugin/per-request/image components. Key/workspace budgets stop requests
when their spending window is exhausted. These checks do not by themselves prove
a strict all-charge ceiling for one in-flight call. `max_tokens`, price estimates,
`cost_tier` and `limit_remaining` are never converted into `EnforcedBound`.

`key_status()` explicitly reads the [current key endpoint](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key).
It accepts only a current, non-resetting, finite positive key allowance that
includes BYOK spend; disabled/expired/malformed/unavailable/redirected responses
fail closed. It returns a small projection, never raw responses, labels or keys,
and always marks `hard_per_request_cap: false`. This necessary credential check
cannot authorize a model call. Constructing the verifier performs no network I/O.

## Qualified supplier seam

The embedding operator must separately qualify a supplier's actual billing
mechanism and authenticated control plane. The current OpenRouter key API does
not supply the additional ceiling protocol described here. Do not fabricate its
fields or sign local price estimates to make the adapter pass.

```python
from cloudyfan_jev.provider_bounds import OpenRouterBoundVerifier
from cloudyfan_jev.budget import BudgetedCore

verifier = OpenRouterBoundVerifier(
    core.client,
    contracts=qualified_supplier_statements,
    issuer_keys=independently_trusted_supplier_verification_keys,
    configurations=exact_profiles_by_version,
    request_policies=qualified_request_policies,
    enforcement_probe=qualified_authenticated_supplier_control_plane,
)
controlled = BudgetedCore(core, budget_store,
    model_bounds=qualified_bounds, tool_bounds={}, verify_bound=verifier)
```

The probe receives a detached contract body and must return fresh supplier state;
it must not generate a response or infer a cap from remaining credits. Both
contract and state envelopes contain exactly `body` and `signature`. Signature is
HMAC-SHA256 over canonical UTF-8 JSON (`receipt_store._encoded`), verified against
a separately trusted issuer key of at least 32 bytes. Authentication proves the
statement's origin/integrity within this trusted integration; it does **not**
prove that the supplier's billing mechanism is effective. Operators possessing
the shared HMAC key can forge statements, so key control and independent review
remain mandatory. Tests use unmistakably synthetic statements and keys.

Contract body fields are:

- `schema_version: 1`, unique `id`, `issuer`, `credential_sha256` (domain-separated
  fingerprint from `credential_fingerprint`, never the credential);
- exact `model_version`, `configuration_sha256` of the complete pinned profile,
  `request_policy_sha256`, and exact `endpoint` (`CHAT` or `DECISIONS`);
- `mechanism: provider_all_charge_per_request_ceiling`, decimal-string
  `ceiling_usd`, and `covers` containing generation, reasoning, classification,
  advisor, plugins and failure charges;
- finite Unix `issued_at` / `expires_at`, independently reviewed upstream
  `enforcement_sha256` and source `evidence_sha256`.

`EnforcedBound.evidence` must be `supplier-cap:<id>`. Exactly one authenticated
matching contract is required. Its ceiling cannot exceed the admitted bound.
Request policy has exactly `endpoint`, `max_tokens`, `max_payload_bytes`, `tools`.
Chat requires bounded positive max tokens, the pinned effort/provider, fallback
false, supported fields and exact approved tool schemas. Decisions require their
own exact endpoint and provider policy. Additional plugins/auto-router fields are
rejected; the signed coverage still must include any embedded advisor fees.

Current state has exactly schema version, issuer, contract body hash, active flag,
credential/enforcement fingerprints, ceiling, all-charge coverage and observed/
expiry timestamps. It must authenticate to the same issuer, match the entire
contract and remain no more than 30 seconds old by default (configurable up to
60 seconds). There are no default trusted issuers or automatic approval paths.

## Exact dispatch and accounting

Preflight verifies the full frozen profile. Dispatch additionally binds the actual
Client object, HTTP client, credential, endpoint and exact payload. Contract/state
validity is checked again after the potentially blocking key GET. The resulting
one-use lease is consumed **after** client locking and durable intent, immediately
before POST, with no further control-plane I/O. An elapsed/changed lease cannot
send a request. Supplier enforcement must itself continue covering admitted
in-flight calls; local freshness is not that supplier guarantee.

A denial after intent is durable becomes `not_dispatched` transport evidence and
a verified internal zero-fee event; it has no fabricated provider generation ID.
Restart/sync preserves it and does not resend it. A transport exception after POST
may have been billed and retains the existing unknown-cost stop. A real cap
breach still invokes the durable shared-account violation logic.

Verifier configuration/credential/contract hashes are frozen in BudgetedCore and
registered HTTP descriptors. Changed enforcement rejects reuse of a frozen task.
Raw supplier statements, issuer secrets and credentials are excluded from public
result projections. This cloud adapter does not certify local tools/backends or
supply their separate fee guarantees.

## Verification boundary

Offline tests cover synthetic valid all-tree generation/classification/repair,
invalid signatures/issuers/coverage/state, key status, endpoint/key/provider
changes, fallback/plugins, expiry during key GET and intent persistence, lease
single use and interrupted accounting. They use MockTransport only. No actual
supplier statement, external probe, effective account configuration or paid
request was qualified. RG-04 remains pending.
