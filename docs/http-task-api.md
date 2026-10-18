# Private HTTP task API (offline integration)

`HTTPApp` is a WSGI callable. Importing or constructing it does not start a server.
The delivered interface is a registered single-task text workflow, not OpenAI or
Ollama chat compatibility. Formal authentication, tenant operations, retention
approval and deployment remain separate release gates.

## Server composition

```python
from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService

service = TaskService(
    private_service_root,
    contracts={
        'sql-v1': RegisteredContract(
            version='public-sql-contract-v1',
            runner=budgeted_core,
            environment_factory=lambda fresh_workspace: SqlEnvironment(fresh_workspace),
            tool_names=('read_workspace', 'write_workspace'),
            mode='cloudyfan',
        ),
    },
    allowed_principal='operator',
    retention_policy=None,
)
app = HTTPApp(service, authenticate=trusted_authenticator, audit=redacted_event_sink)
```

`budgeted_core` must be `BudgetedCore` wrapping a Core with an explicit
`ExecutionPolicy` and a `DiagnosticContract`. Its authorization, enforceable bounds
and verifier must be configured by the administrator. `retention_policy=None`
means retention is not approved, not that data is deleted or production is ready.
This module intentionally includes no bearer-token database, real authenticator,
network listener, automatic cleanup, or deployment command.

The authenticator receives WSGI environ and returns the trusted principal or
`None`; only the registered principal is admitted. Never derive that principal
from the JSON body. The optional audit sink receives only event kind and HTTP
status. HTTP exception text, raw prompts, answers, authorization headers and
tracebacks are not logged by this adapter.

## HTTP contract

- `POST /v1/tasks`: JSON containing exactly `run_id`, `contract_id`, `prompt`.
  Creates the frozen task, then executes synchronously.
- `GET /v1/tasks/{run_id}`: current execution status and that run's cost snapshot.
- `GET /v1/tasks/{run_id}/result`: the owned answer, public pass flags, latency and
  complete model/classifier/tool API cost. A read-only Core result check verifies
  saved identity; local receipt synchronization refreshes reconciliation without
  a model POST or reconciliation GET. Shared-account events are never returned.
- `POST /v1/tasks/{run_id}/resume`: exactly `{}`. Explicitly continues an unfinished
  task using the registered frozen configuration and existing paid responses.

Statuses distinguish `ready`, `running`, `passed`, `failed`, `unverifiable`,
`engineering_interrupted`, `accounting_interrupted` and `policy_denied`.
`failed`/`unverifiable` may have HTTP 200 because HTTP completion is not task
correctness. Policy denial is 403, unresolved accounting is 409, engineering
interruption is 503. Missing results are 409; authentication failure is 401.
Errors expose stable codes, not exception text. There is no automatic retry.
A process killed mid-request can leave `running`; only explicit resume performs
recovery, with the core and budget unknown-intent gates still in force.

Bodies are bounded to 64 KiB, prompts to 32 KiB UTF-8. Only nonempty text is
accepted. Duplicate JSON keys, arbitrary validator/tool code, provider settings,
model settings, caller identity, images, streaming and multi-message requests
are rejected. Task IDs are bounded safe identifiers. No static workspace, receipt,
file download or unauthenticated result endpoint exists.

## Frozen jobs and scheduler seam

`TaskService.create(principal, payload)` persists the request and full registered
configuration before dispatch: exact model profiles, mode, limits, system prompt,
policy, public diagnostic contract, model/tool bounds, storage identity and tool
allowlist. It does not reserve or spend; `execute(principal, run_id)` invokes the
controlled budget wrapper. A future bounded scheduler can queue between these
methods. `status` and `result` are read views. Configuration changes reject
execution with 409 rather than silently using a new policy. A changed factory or
validator implementation requires a new registered contract version.

Run identifiers are namespaced by authenticated principal and external task ID.
Each execution uses a new 0700 workspace inside the private task directory so
explicitly declared deterministic tools can replay into a fresh environment.
Metadata and results are private 0600 files. Symlinked task paths are rejected.
The registered environment factory is trusted code: it must stay inside that
workspace, sandbox tools, and correctly implement the public validator. This
adapter does not sandbox arbitrary Python or certify the authenticator.

Tool names must be registered both in the contract and the corresponding budget
and execution-site policies. The controlled runner receives only the contract's
allowed tool bounds; model-invented tool names cannot acquire authority from a
more permissive shared runner. Unknown fees and interruptions retain receipts
and reservations. `complete_api_usd=None` means unknown, never zero. Host and
human costs remain unknown. Per-run cost snapshots exclude unrelated historical
or other caller charges; shared account unknowns can still block admission.

## Verified evidence and limits

Tests use `httpx.WSGITransport` at the HTTP seam and `MockTransport` upstream;
real socket attempts are blocked. They exercise submission, status, result,
response reuse, fresh-workspace resume, public failures, unknown costs,
configuration changes, authorization, allowlisted tools and sanitized errors.
They are not evidence of real clients, provider caps, network deployment,
production identity integration or approved data-retention operations.
