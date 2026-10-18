# Controlled admission and complete cost accounting

`BudgetedCore` is the application entry point for budget-controlled execution. The
underlying `Core` remains usable with mock transports; constructing a plain Core
is **not** a spending authorization. HTTP/scheduler integrations must compose the
budget wrapper, not bypass it.

## Trust boundary and safe default

An optional [supplier-bound verifier](provider-bounds.md) is implemented. No
qualified supplier ceiling contract/control-plane integration is bundled; ordinary
OpenRouter key budgets are insufficient. `verify_bound=None` rejects execution
before a paid POST. A verifier is an application-controlled function
`verify_bound(kind, identity, payload, bound) -> bool`. It must check an **effective,
enforceable** bound for the exact request, model, provider, configuration and tool.
It runs at whole-workflow admission and again before each model/tool dispatch.
The supplied provider adapter also consumes a fresh exact-request lease after
durable intent, immediately before POST; an unissued request is recorded as a
verified internal zero-fee event without inventing a provider generation ID.
Only literal `True` admits work. An evidence reference, model price, maximum token
count, average cost, `cost_tier`, provider marketing statement or caller estimate
alone is insufficient. Tests use an explicitly synthetic verifier and do not prove
OpenRouter or any other provider has a usable cap.

`EnforcedBound` stores a decimal USD amount and evidence reference. At preflight
the verifier receives the profile (or tool execution policy); at dispatch it
receives the actual request. The integration owns verifier correctness, provider
credentials/limits, and cannot let untrusted request bodies supply a verifier or
replace approved bounds. A bound breach commits a durable shared-account violation before raising, leaving
the event unresolved. The violation preserves the observed charge, receipt ID,
verification flag and prior provisional amount. A local accounting layer cannot
undo an upstream overcharge. Reopening the store, replaying a stale lower receipt
or using another client does not clear the stop. No automatic reset/recovery API
is provided; explicit account review and an independently approved recovery
procedure are required. Original client receipts remain unchanged.

## Public API

```python
from decimal import Decimal
from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore

store = BudgetStore(private_directory / "budget.sqlite")
store.authorize(Authorization(
    reference="existing-user-authorization-reference",
    total_usd=Decimal("10"),
    expires_at="2026-10-19T00:00:00+00:00",
))
app = BudgetedCore(core, store,
    model_bounds=approved_exact_model_bounds,
    tool_bounds=approved_tool_bounds,
    verify_bound=independently_verified_enforcement)
result = app.run(run_id, public_task, fresh_environment,
                 contract_version="public-contract-v1")
```

One SQLite file represents one shared cumulative budget. Authorization is
immutable and expiring; reopening it does not reset spending. Independent callers
must share this same store and namespace run IDs per caller. Existing evidence
must be imported before admission:

```python
store.import_history("historical-request-id", cost_usd=None)
# After independent reconciliation, replace that same event; do not append a fee:
store.import_history("historical-request-id", cost_usd="0.024",
                     receipt_id="verified-generation-id", verified=True)
```

`run` automatically imports existing intents/receipts in the configured cloud and
local clients, including interrupted runs. Other client roots, older experiment
ledgers and external tool histories require explicit `import_history` calls by
the application. Imported unverified history has no enforceable bound and blocks
new dispatch until independently reconciled (`verified=True`); known provisional
spending is retained, but available balance is unknown. This also applies to imported
local zero-API-fee receipts: independently confirm and import them as verified;
the local backend does not support cloud generation GET reconciliation. A fresh database must not
be presented as a lifetime total.

## Reservation and execution

Before dispatch reserve the entire finite workflow:

- first-stage `max_turns × first-model bound`;
- second-stage `max_turns × maximum reachable repair-model bound`;
- CloudyFan: two classifier bounds (other workflow modes: none);
- `2 × max_turns × max_tool_calls × maximum allowed tool bound`.

`max_tool_calls` is per model turn. Tool-free configurations add zero. Every
advertised tool requires an explicit bound before the first model POST. Bounds
cover **all** charges for that call, including embedded advisor/router charges;
those are not added again. Unreachable profiles are not charged or reserved.

SQLite immediate transactions prevent two processes reserving the same balance.
A run retains its whole reservation until successful completion of orchestration;
failed/interrupted orchestration retains the hold, even when its currently known
charges are small. Completion releases unused calls, but each unverified managed
call retains `bound - provisional cost` (minimum zero) as a separate risk hold.
Thus settled costs plus these holds equal the call upper bounds until independent
reconciliation fixes the final amount. The hold is computed from persistent
events, including completed runs written by earlier versions; it is not reset by
reopening the database. Every new dispatch also rechecks aggregate exposure, so
legacy over-admitted reservations cannot spend further. A pending/unknown event stops every new dispatch. This is
intentionally conservative and is not a throughput promise. Restart never
resends an unknown event. Receipt reconciliation can replace provisional cost
with verified total; unique event and receipt identities prevent double counting.
Completed runs can be read again without new model calls. Their event costs remain
spent after their workflow reservation is released; outstanding reconciliation
risk still reduces the available balance. Expiry is checked before every new
model/tool event, including repair and classification.

## Tools and local inference

`ToolBound(bound, external=False)` is for deterministic local tools with a proven
zero API fee. The wrapper forwards only their explicit `replayable_tools`
declaration to Core. Replaying them recreates the fresh sandbox without adding
fees. Other execution costs (host, electricity, labor) remain outside API totals.

`ToolBound(bound, external=True)` requires `environment.call` to return
`ToolReceipt(result, cost_usd, receipt_id, verified=False)`. The durable event is
written **before** dispatch and the receipt before returning to Core. Only
`result` is exposed to the model, never billing metadata. An exception, missing
receipt or unknown cost stops execution and preserves the unresolved hold.
External tools are never automatically replayed. Application-level reconciliation
uses `store.record_event` with the original event ID and independently checked
receipt identity; it does not execute the tool again.

Local inference uses the same wrapper around an injected local backend and its
separate receipt root. Zero is admitted only with the backend's explicit local
no-model-fee evidence and a verified bound. It does not make host or energy free.

## Outputs and limitations

- `result['costs']`: existing Core model/classifier API cost fields.
- `result['budget']['run_total_usd']`: complete controlled run model **plus tool**
  charges; null if any event is unknown.
- `store.report()`: authorization, active reservation, known, independently
  verified, unknown count, available balance and safe event identifiers.
  `provisional_hold_usd` separately reports the remaining reconciliation risk;
  `unbounded_history_events` reports imported history lacking a final fee.
  `dispatch_blocked` reports accounting/exposure stops (not a substitute for
  checking authorization expiry). `violations` retains over-bound observations.
  While a violation remains, `total_usd` and available balance stay unknown; the
  partial known total excludes that unresolved event, not its preserved receipt.
- `BudgetError.control_plane_error=True`: a control-plane stop, not model failure.

Amounts use decimal strings/Decimal end to end. Provider JSON decimal lexemes
are preserved before conversion. No subscriptions, customer charging, tax logic,
paid retry, live API availability test or deployment is included.
