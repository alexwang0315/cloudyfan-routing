# Bounded queue and principal isolation

The bounded entry point is `QueueHTTPApp(QueueScheduler(...), authenticate=...)`.
It is a WSGI application and starts no server, worker or background thread. An
embedding operator explicitly calls `dispatch_next()` from controlled workers.
The original synchronous `HTTPApp` is a separate entry point and **does not pass
through this queue**. A deployment claiming bounded admission must expose only
`QueueHTTPApp` for these jobs; do not concurrently expose direct TaskService
execution or the synchronous submit endpoint as an alternate bypass.

`TaskService(..., allowed_principals={'alice', 'bob'}, ...)` accepts a trusted
server-side principal allowlist. Its existing `allowed_principal='admin'` form
remains supported; the two forms cannot be combined. Caller identity comes from
the injected authenticator, never the request body. Principal/run identifiers
map to separate hashed internal jobs, private workspaces, core runs and receipt
IDs. All user status/result/cancel operations resolve that pair. Shared budget
history, internal receipt paths and other users' costs are not exposed.
The server and its Python contract factories remain trusted; this is not an OS
sandbox against a malicious server operator or an approved production tenancy
system.

## Configuration and lifecycle

```python
limits = QueueLimits(capacity=32, concurrency=2, per_principal=4,
                     max_wait_seconds=30)
queue = QueueScheduler(service, private_root / 'queue.sqlite', limits=limits)
app = QueueHTTPApp(queue, authenticate=trusted_authenticator)
# Operator-controlled execution, no implicit retry loop:
queue.dispatch_next()
```

`capacity` bounds waiting jobs, `concurrency` bounds running plus unresolved
worker-interruption slots, and `per_principal` bounds that caller's queued,
running and unresolved jobs. Admission/claim/cancel changes are SQLite immediate
transactions, so concurrent callers cannot oversubscribe the configured counts.
FIFO uses arrival time then insertion order. These settings, allowed principals,
service root and measurement provenance are frozen in the queue database; a
restart cannot silently relax them.

Submit freezes the registered TaskService request and adds a queue row. It makes
no model request or budget reservation. Full model/tool bounds, current payment
authorization and accumulated unknown/history costs are checked by the existing
`BudgetedCore` at actual dispatch; there is no parallel budget implementation.
Admission denial is `queue_full` or `principal_quota_full` (HTTP 429), recorded in
per-principal admission history. Expiry is `queue_deadline_expired`, checked before
claiming work. A deadline-expired queued request has no paid call. The persisted
clock rejects nonfinite/backward readings rather than extending deadlines;
production clock policy remains an operator concern.

HTTP subset:

| Method and path | Behavior |
|---|---|
| POST `/v1/tasks` | `{run_id, contract_id, prompt}` only; HTTP 202 while queued; no execution |
| GET `/v1/tasks/{id}` | Own queue state, per-run costs, cancellation flag and timing |
| GET `/v1/tasks/{id}/result` | Own independently validated TaskService result, when available |
| POST `/v1/tasks/{id}/cancel` | Empty JSON object only; no caller configuration accepted |
| POST `/v1/tasks/{id}/resume` | Unsupported; no automatic resubmission |

A cancelled queued job is terminal and unpaid. Reusing its identical submit ID
returns its current status, not a fresh paid run; changing its request is a
conflict. For a dispatched job, cancel sets `cancellation_requested` only. It does
not terminate the provider call, erase intent/response, refund a reservation or
claim that later pipeline calls have stopped. The active bounded Core workflow
can finish; its outcome and full costs remain available. A safe future provider
cancellation protocol requires separate design and verification.

## Restart and interrupted work

Opening the same queue preserves jobs and their slots. `dispatch_next` never
selects running/interrupted jobs and never retries a paid request. Only after all
old workers are known stopped may an operator call
`recover_inflight(workers_stopped=True)` to mark running jobs
`interrupted_pending`. This marks evidence; it neither releases their slots nor
changes shared budget reservations. Unknown model/tool charges remain unknown.
The explicit boolean is a trusted operator assertion, not a process-liveness
probe. A crash after dispatch but before queue completion is deliberately held for
operator inspection; this prototype has no automatic lease expiry or recovery
resubmission endpoint. Reconciliation/manual recovery remains separate from
queue admission, and must not manufacture a refund.

## Measurement evidence and pending qualification

Each status exposes queue wait (claim minus submit) and end-to-end time
(completion minus submit). Incomplete timings remain null. Tests set
`measurement_kind='synthetic'`; normal configuration defaults to `unqualified`.
`production_slo_validated` is always false. The fixed two-job synthetic load has
three seconds initial wait and two seconds per model: queue waits 3/5 seconds,
end-to-end times 5/7 seconds. This is clock arithmetic, not throughput or latency
measured on real infrastructure. With the current fail-closed shared budget,
a pending unknown paid event can also prevent another paid dispatch even when
queue concurrency allows another worker; configured slots are not guaranteed
provider parallelism.

`tests/test_scheduler.py` uses authenticated WSGI clients, MockTransport, blocked
real sockets, synthetic identities and bounded thread synchronization. It covers
same-ID isolation, overlapping submit/dispatch, capacity/quota/expiry, cancelling
queued and sent requests, a simulated process death with durable unknown intent,
restart limits and budget preservation, authorization expiry and fixed-load
latency. No actual deployment, load test, model API or hardware was used. Formal
identity integration, retention, production concurrency, SLOs, multi-host
scheduling and automatic post-crash recovery remain unqualified.
