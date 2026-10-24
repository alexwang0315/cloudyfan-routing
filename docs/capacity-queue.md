# Bounded capacity queue

This is an opt-in, server-registered `capacity-queue-v1` suspension protocol.
It reuses Core's `QueueScheduler`, `TaskService`, original task identities,
`BudgetedCore`, supplier capacity and receipt stores. Registration does not
qualify a model, prove arbitrary Python code pure, or authorize a paid run.
The immediate Issue35 policy and disabled synchronous service keep their
original behavior. Existing policy/receipt serialization is not rewritten.

The host supplies a versioned `QueuePreparation`, fixed tool schemas and explicit
deterministic local replay contracts. Original Core, CategoryCore and
BackupCategoryCore support common request preparation and continuation;
Monitor's RunBoundCore delegates to the original pinned factory. Unsupported
external/nonreplayable effects are rejected before acceptance or any POST.
Actual environment schemas are checked before accepting work and again during
execution. A registration marker is not a general purity checker.

All four limits are explicit and frozen: pending capacity, execution concurrency,
per-principal pending count and maximum wait. Acceptance returns HTTP202 with
no model POST, paid intent, fee event or monetary reserve. A durable accepting
journal bridges the queue, original job and adoption stores without claiming
a cross-store transaction. Duplicate submissions return the original state;
changed content conflicts. Every ingress uses the same queue, including native
aliases, service execution and resume. Queries never dispatch work.

Only an explicitly invoked `QueueScheduler.dispatch_next()` executes a next
boundary. The common original payload builder, actual local payload rewrite and
HTTPx serialization produce its exact frozen next wire. Before a slot is claimed,
the worker checks original access and credential references, contract, current
qualification/health/location/deadline, applicable physical groups, trusted token
proof, response windows and original fee guards. Final POST gates repeat these
checks. Original fee authority can still refuse otherwise disjoint capacity.

FIFO applies to overlapping physical groups; eligible disjoint work can proceed
with its original independent fee authority. Waiting takes no execution slot and
creates no second reserve. Claiming counts against the principal limit. The
claim journal and atomic all-group allocation retain uncertain work after a
crash; PID disappearance or elapsed time is never proof of an unsent request.

The first next call after a physical response saves a private immutable boundary
and suspends before its paid intent. Continuation reuses original receipts,
checkpoints, accepted deadlines and one workflow reserve. Backup/category
acceptance covers real three-generation/two-classifier continuation; global
counts do not reset. Unknown original fees stop automatic accounting GETs and
fallback. Ordinary receipts and all prior known charges remain authoritative.

Own-client `POST /v1/tasks/{run_id}/cancel {}` cancels waiting work before its next
POST. After earlier paid work, the reason is `cancelled_before_next_dispatch`;
only the unused monetary reserve can finish. Running/claiming cancellation is a
request flag, never confirmation of supplier cancellation, refund or safe retry.
Read-only status projects expiry from the original deadline; an explicit worker,
submit or cancel may persist the terminal transition. Waiting/resume never
extend the original thirty-day access deadline. Retention erasure still requires
the existing separate explicit procedure; this feature deletes no evidence.

`GET /monitor/queue` is operator-only and read-only. Jobs are active-first, then
newest, bounded to100 with exact total/effective-active/retained-active counts and
completeness flags. Rejections have their own latest100/count/completeness
projection. Unknown costs remain unknown. Frozen job details contain the saved
mode, category, pool/contract/preparation/capacity versions, access expiry and
customer tariff/hash; current controls never substitute for accepted bindings.
Physical-group details stay in the operator projection. Worker transitions update
only Monitor's derived public observation and original version-use metadata.
The desktop polls every5s, preserves focus/disclosures, clears old views on
authorization loss, and does not offer operator impersonation of client cancel.

## Native text compatibility

The existing native text API keeps its original interface and uses the same
`capacity-queue-v1` pending envelope for enabled queue jobs. `/api/chat` and
`/api/generate` return honest HTTP202, `done:false`, `cloudyfan.pending:true`
and the own-task query reference. Pending responses contain no fabricated
`message`, answer or completion. Clients must support202 and subsequent own-task
queries. No hidden retry, background worker or automatic scaling is started.

Actual loopback validation pins Python Ollama SDK0.6.3: `GenerateResponse` can
represent pending as `response=None, done=False`; `ChatResponse` requires a
message and raises Pydantic `ValidationError` on this honest pending response.
This is a material compatibility limit, not full asynchronous SDK compatibility.
Such clients must query the own-task endpoint and wait for completion. A duplicate
native call after an explicit worker completes returns the original answer with
no additional model POST.

## Verification boundary

The HTTP and desktop fixtures use real Core/BudgetedCore/TaskService/Identity,
HealthStore/CapacityStore/QueueScheduler and real loopback sockets, with all model
traffic handled by MockTransport and non-loopback connects denied. They retain
original ledgers, exact wires and immutable receipts, including initial failures.
Independent processes reopen the same queue/service/identities; distinct original
receipt roots prevent the Client lock from masking physical-group admission.
Saved429 response tests use the actual CapacityWindows backend, not a fake UI
state. Queue and workflow timings describe these synthetic runs; no production
supplier throughput, capacity qualification, SLO, deployment or paid run is
established by this evidence.
