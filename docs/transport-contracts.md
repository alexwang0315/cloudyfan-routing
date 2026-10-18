# Transport contract matrix

`tests/test_transport_contracts.py` runs the same registered public task and upstream
fixtures through HTTP, Ollama `/api/generate`, Ollama `/api/chat`, and QueueHTTPApp.
It exercises real `TaskService`, `Core`, `BudgetedCore`, receipt storage and scheduler
code. Only the upstream HTTP connection and the public task environment are injected.
All inbound requests use `httpx.WSGITransport`; upstream calls use `MockTransport`.
The repository-wide socket guard rejects accidental real network access.

## Shared checks

| Scenario | HTTP | Ollama generate / chat | Queue |
|---|---|---|---|
| First public pass | One generation, no classifiers | Same, plus native answer shape | Submit 202 without generation; explicit dispatch |
| Tool then routed repair | Three generation turns, two classifiers, one billed tool | Same full-cost and provenance fields under `cloudyfan` | Same after dispatch |
| Unknown charge | Null complete cost, held reservation, no repeat POST | Native 409; queryable task state | Unknown state retained; no automatic redispatch |
| Caller isolation | Other approved caller sees 404; own identical run ID has distinct call identity | Same on task endpoints; native IDs remain caller scoped | Same, with independent queue namespace |
| Changed task | Reject same run ID with changed prompt | Native 409 | Reject changed request |
| Changed frozen configuration | Reject reuse/result/resume | Native reuse and shared task endpoints reject | Duplicate submission only looks up state; result rejects changed configuration and dispatch does not run again |
| Unavailable validation | `unverifiable`, not success despite HTTP 200 | Native 422 with no answer/message success shape | `unverifiable` after dispatch |
| Validation interruption | Explicit resume reuses paid answer | Explicit shared task resume reuses paid answer | Resume endpoint intentionally unsupported (404); no automatic redispatch |

The eight scenario rows across four protocol paths produce **32 tests**. Existing
`test_http_api.py`, `test_ollama_api.py` and `test_scheduler.py` retain protocol-specific
coverage including unsupported inputs, queue limits, cancellation and crash recovery.
The matrix supplements them; it does not replace or duplicate their entire setup.

The provenance assertions cover actual model/provider, unique call/generation IDs,
frozen model configuration and effort, snapshot/policy/config IDs, score source,
source version and metric. Classifier calls are checked separately from generation.
Capability-card prose, request prompts, tool receipt identities and another run's
accounting information must not appear in results.

The tool-plus-repair fixture's expected complete API cost is calculated independently:
`3 × 0.01 + 2 × 0.0001 + 0.04 = 0.0702`. A separate historical `0.08` account event
must not inflate this task's cost. Unknown charges stay `null`; unmeasured host and
human costs stay unknown. All these values are synthetic fixture amounts.

## Running and regression sensitivity

The repository's unified validation entry includes these tests. For focused work:

```sh
uv run --locked --offline --extra dev python -m pytest -q \
  tests/test_transport_contracts.py tests/test_http_api.py \
  tests/test_ollama_api.py tests/test_scheduler.py
uv run --locked --offline --extra dev ruff check tests/test_transport_contracts.py
```

On 2026-10-18 the focused run passed **126 tests** and Ruff. Three process-local
negative probes confirmed that the new assertions detect specific regressions:

| Temporary mutation | Observed result |
|---|---|
| Remove `actual_provider` from projected provenance | All four first-pass protocol tests fail |
| Convert unknown complete cost to `"0"` | All four unknown-charge protocol tests fail |
| Issue another mocked model request while reading a completed result | All four first-pass protocol tests fail on request count |

The probes were loaded only into isolated pytest processes. No runtime source file
was modified, no mutation plugin is loaded by the normal suite, and the unmodified
focused suite was rerun successfully afterward. Probe logs live in the workspace's
`.scratch/retro-improvements/contract-mutations/` evidence directory.

## Limits

This shared matrix itself uses protocol fixtures. The separate
[pinned official SDK suite](ollama-sdk.md) now verifies synchronous Python
`ollama==0.6.3` against the same application and mocked upstream, including its
chat empty-tools serialization and typed metadata limitations. It does not turn
every fixture row or other client into SDK-qualified evidence.

These checks establish offline adapter contracts, not general Ollama compatibility,
network deployment behavior, throughput, production authorization/retention, real
provider price enforcement or model quality. QueueHTTPApp is the bounded queued
entry; HTTPApp and OllamaApp remain synchronous. The queue's unsupported resume
operation is asserted explicitly, rather than treating it as a successful resume.
Score IDs in this fixture prove propagation only; they do not qualify model pools.
