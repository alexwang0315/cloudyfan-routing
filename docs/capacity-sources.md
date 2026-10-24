# Supplier capacity sources

Issue34 adds a source inspection path to the controlled host. It does not enable
capacity admission or replace the existing synchronous busy429 behavior. A saved
limit never qualifies a model, authorizes payment or grants a dispatch slot.

The host supplies `CapacityStore` and `CapacityObserver` to `assemble_monitor` as
`capacity_observer`. Configuration is a server action; the browser has no import,
credential, grouping or limit editor. Existing hosts may omit this argument and
receive an explicit unconfigured view.

## Controlled configuration

`cloudyfan_jev.capacity.CapacityStore(path, clock=...)` persists immutable versions
in a dedicated SQLite file. `configure(configuration)` atomically registers and
selects one version. Reusing a version with different content fails. The schema
has exactly `schema_version:1`, `version`, `bindings` and `groups`.

Each binding contains opaque `id`, `provider`, `account_ref`, `credential_ref`,
`endpoint_ref` and every applicable group ID in `groups`. References identify the
server's binding; they never contain a credential value or filesystem location.
Two bindings can share an account group while retaining separate key groups.

Each group contains `id`, `scope:{kind,ref}`, `source`, `supplier_limits` and
`local_limits`. Scope kinds are account, credential, endpoint and shared. Source
kinds are `controlled_import`, `openrouter_key` and `unsupported`; every source
has its own version. An OpenRouter key source also identifies the configured
credential reference and must use that exact credential scope. Other suppliers
can be imported explicitly; import is never described as automatic retrieval.

Every observation preserves dimension, status, value, original unit,
`window_seconds`, `observed_at`, `reset_at`, `valid_until`, `evidence_ref` and exact
scope. Dimensions are RPM (requests), TPM (tokens), concurrency (requests) and
USD. Quantities are exact nonnegative integers; dollar values are decimal strings.
Unknown, unsupported and not-applicable values are null; not-applicable still
requires evidence and an explicit validity period. Scope mismatch, expiry, future
observations and contradictory fresh observations remain visible with safe
reasons and original observations; they are not available capacity. Invalid or
non-finite configuration values are rejected before import.

Configuration is bounded to128 bindings,128 groups,32 observations per group and
dimension set, and256KiB serialized input. The public snapshot emits only this
allowlist; unknown nested fields are rejected. No original provider payload is
stored in this capacity source database.

## Explicit refresh and read-only views

- `GET /monitor/capacity`: operator-only saved snapshot. Queries are rejected.
- `POST /monitor/capacity/refresh` with `{}`: operator-only explicit refresh.
  Arbitrary keys, account URLs, paths, groups and settings are rejected.

The observer receives an explicit map from credential reference to the existing
`OpenRouterAccounting` instance. Its `key_capacity(time_budget=...)` method uses
the same allowlisted, size-bounded transport and decimal parser with exactly one
`GET /api/v1/key`; it does not request management credits or generations. Only
key-dollar limit, remaining amount, usage, reported reset period, BYOK inclusion flag and evidence hash
are retained. Missing RPM/TPM/concurrency remain unknown. A reported daily reset
does not invent an absolute reset timestamp. Customer credits, health and fee
reservations are never capacity evidence.
The original `include_byok_in_limit` flag remains explicit; this limit never
claims to cap separately billed external BYOK supplier fees.

OpenRouter key data has no supplier expiry promise. Its saved `valid_until` uses
the host's conservative source freshness TTL (default300 seconds), explicitly
labelled `valid_until_basis:local_freshness`. Controlled imports are labelled
`controlled_import`; the local freshness deadline never claims a supplier
guarantee or resets a supplier window.

Defaults are at most8 source requests and10 seconds per explicit refresh;
controlled hosts can choose smaller limits. Each request receives the remaining
time budget, and late replies are not adopted. Responses remain bounded by the
existing256KiB accounting transport limit. A host-wide file lock rejects another
simultaneous refresh without blocking saved reads. Source failures append safe
outcomes and preserve earlier successful observations with their original time.
Every attempt remains stored; the view shows at most the latest20 per group and
reports the complete retained count. Exhausted budgets do not create more GETs.

Ordinary GETs, polling and disclosure expansion use read-only SQLite connections.
They do not update sources, reconcile bills, recover health, change groups or
dispatch. The desktop page separates supplier limits from local conservative
caps, retains disclosure focus during polling, and marks partial failures and
expired evidence. Clients retain their own existing responses and cannot access
this global operator view. Logout removes the capacity view and invalidates
pending reads.

## Verification and scope

`tests/test_capacity_sources_http.py` uses the actual loopback host, task service,
identities, Core and BudgetedCore with synthetic upstreams. It checks actual
request records, original dispatch429, immutable versions, units, source failure,
clock handling, safe fields and unchanged saved bytes on reads.

`scripts/qa_capacity_sources_desktop.cjs OUTPUT` starts the private fixture using
actual BackupCategoryCore/HealthStore, controls Chrome on loopback only, checks
disclosures and focus, explicit refresh, partial failure, source expiry, saved
task costs and the30-day access boundary. Set `CLOUDYFAN_CORE_ROOT` to the matched
Core checkout; optionally select an already prepared Python interpreter through
`CLOUDYFAN_QA_PYTHON`. Credentials stay on the private fixture pipe. First failures
and screenshots remain in separate output directories.

These checks establish synthetic local behavior only. Source timestamps do not
establish supplier qualification, throughput, enforceable fee bounds or deployment.
The original30-day task access and at least365-day metadata/unknown-fee retention
rules remain in force; this feature does not delete or reopen expired text.
