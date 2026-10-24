# Versioned immediate capacity admission

This is an explicit host opt-in on the same synchronous TaskService. It is not a
queue, retry policy, supplier spending cap, model qualification or token estimate.
The source-only view in [capacity-sources.md](capacity-sources.md) remains useful
without enabling execution. All bundled demonstrations use MockTransport and
synthetic evidence; none qualify a real supplier, tokenizer, model or machine.

## Host authority and activation

The host creates `CapacityController(CapacityStore(...), credential_active=...)`
and registers immutable policy versions with exact `CapacityRoute`s, a captured
source configuration version, and a task lifetime of 1–86400 seconds. Assembly
uses `assemble_monitor(..., capacity_observer=..., capacity_controller=...)` with
the same private CapacityStore for source evidence and dispatch accounting.
Registered routes bind the actual transport, endpoint, model, upstream credential
alias and applicable shared groups. The complete registered task descriptor and
the authenticated original caller credential reference/scope are adopted at task
acceptance. No caller token or credential digest is saved in capacity records.
Private in-memory equality checks pin actual transport credentials and defaults;
these values are never in policy descriptors, logs or API projections.

The operator uses `POST /monitor/capacity/activate` with
`{"revision": 0, "version": "host-registered-version"}`. A null version selects
legacy behavior for new adoptions. Neither browser nor task body can register a
policy, edit a cap/key/group, provide token counts or bypass admission. Changes
that could relax live adoptions, held allocations or unexpired windows fail with
409. Source configuration changes use the same guard. Renaming a group or policy
cannot reset physical usage. This protocol supports one group per exact physical
scope; incompatible windows for that scope are refused explicitly.

## Request and fee ordering

Receipt replay is resolved before preparation, token calculation, capacity or new
paid work. Fresh controlled requests are built once using the supported standard
httpx client and then send that same prepared Request. Exact serialized **body**
bytes have a separate `wire_sha256` and `httpx-prepared-json-v1` version. URL,
model, binding and complete task descriptor are separately bound in the scope
hash. The older canonical-object `request_sha256` remains the original replay and
fee-lease identity; it is not a hash of HTTP framing, headers or packet bytes.

Opt-in rejects custom client subclasses, instance send/build overrides, auth
flows, hooks, mounted transports, redirects, transport retries and changed final
URLs. It rechecks supported configuration before POST. The disabled path keeps
its original `.post(json=...)` behavior. This is a supported-adapter contract,
not a Python sandbox or a guarantee about arbitrary monkeypatched host code.

Every applicable local RPM, TPM and concurrency dimension must be fresh and known
or explicitly not applicable with evidence. Unknown never means unlimited.
Required supplier evidence must match the frozen scope and remain fresh. A TPM
route needs a trusted versioned adapter returning `TokenUpperBound` for those
exact bytes and scope, including all applicable system/tool/schema/input and
output/reasoning overhead. A reference string alone does not prove such an upper
bound. There is no default chars/token, average-token, price or dollars conversion.
Absent, invalid, mismatched or expired proof refuses before paid intent/event.

The SQLite transaction acquires **all** groups or none. Capacity and supplier fee
reservations remain separate. Original complete-workflow bounds, monetary
authorization/unknown-fee stops, privacy, qualifications, health and task deadlines
still apply. The original fee post-guard runs before the durable capacity send
intent. Backup generation, classifier and local calls recheck the original exact
health/policy/deadline gates at that final boundary. Known refusal after intent
has an immutable `not_dispatched` response and verified zero fee; pre-intent
refusal creates no model/tool fee event. A terminal capacity refusal releases only
unused complete-workflow monetary reserve when all of its own fees are known;
actual/provisional fees, global unknown-fee stops and capacity holds remain. An
own unknown fee keeps accounting interrupted and its reserve. Current original credential validity is
checked again before each controlled POST.

Local calls bind the transformed local request (including `stream:false`), pinned
backend configuration and original local receipt identity. No-model-fee evidence
does not make local capacity unlimited. External tools require a registered
`CapacityToolTransport` and a host environment's `CapacityToolRequest`. Its
preparation occurs before the Core action intent and tool fee event. Arbitrary
external effects without that single-request protocol are refused; they cannot
fall through to the old tool call. Local deterministic tools retain their
explicit original local/zero-fee/privacy policy.

## Conservative windows and unresolved state

Prepared, sent-intent and uncertain quantities do not expire based on a marker,
PID, process exit, heartbeat or elapsed wall time. The marker precedes HTTP send
and does not prove physical send timing. At the send-intent transition, all
applicable groups are rechecked atomically. A trustworthy completed response
anchors a full conservative rate window from completion, which is no earlier
than physical send. Known sent RPM is never refunded. TPM retains its upper bound
when actual quantity is missing/invalid/mismatched; only exact-scope validated
actual usage may replace it. Usage above a supposedly trusted upper records the
actual quantity, invalidates that policy and keeps an uncertain hold.

A proven unsent request can release even if the clock became invalid/backward;
its event uses an unknown time instead of inventing a timestamp or lowering the
clock floor. Incomplete/uncertain dispatch requires later explicit evidence work;
this feature has no automatic cleanup or unverified release operation. Capacity
refusal is terminal in this immediate protocol. Duplicate submit and explicit
resume return the original task, and changed content remains409. Waiting and new
fallback behavior belong to later specifications.

## Safe observations and limits

`GET /monitor/capacity` adds `dispatch` when configured: current revision/version,
host-registered choices, physical group counters, allocation states and safe
reason events. The counters describe local reservations/holds, not supplier
remaining quota or fees. Latest allocation/event arrays are bounded to100 with
`allocation_count`, `allocation_history_complete`, `event_count` and
`event_history_complete`; full hot totals remain correct. Indexed live accounting
has a default1000-allocation ceiling and fails closed at that ceiling. Historical
records are not deleted to meet these bounds. Ordinary reads open capacity SQLite
read-only and perform no model/source/accounting RPC or dispatch work.

Own task state exposes only adopted policy/config/protocol, acceptance/deadline
and a safe refusal code. Other clients cannot read the task. Operator projections
exclude prompts, answers, raw provider errors, tokens and actual credential values.
Capacity DB, sidecars and parent directory must be private/owned, and symlink/path
checks repeat on use. Existing public task/tariff/category/pool/mode bindings and
both fee ledgers retain their original authority.

## Offline acceptance

`tests/test_capacity_admission_http.py` uses actual loopback HTTP and the real
Core/BudgetedCore/TaskService/Store/Identity/HealthStore/capacity stack. Independent
process tests, the desktop QA scripts and ordinary repository checks complement
that seam. External calls are MockTransport only and non-loopback sockets are
denied. RED/failure/partial proofs and source-pinned receipts live outside the
repository in the implementation evidence index. A passing synthetic test is not
production qualification or deployment evidence.
