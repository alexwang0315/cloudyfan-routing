# Public diagnostics and bounded repairs

`Core(..., diagnostic_contract=DiagnosticContract(...))` binds the diagnostic
schema to a run alongside the application's `contract_version`. The default
`public-diagnostics-v1` accepts `passed` (strict `True`, `False`, or `None`) and
optional `checks`. Custom schemas declare a tuple of additional allowed field
names, a nonempty version and public producer `source`, and a UTF-8 byte ceiling
(default 4096 bytes including generated provenance). Values are JSON scalars or
flat scalar lists; nested objects and undeclared fields are rejected.

```python
from cloudyfan_jev.diagnostics import DiagnosticContract

contract = DiagnosticContract(
    version="sql-public-v1",
    source="registered-sql-contract-validator-v3",
    allowed_fields=("code", "message", "checks"),
    max_bytes=4096,
)
```

The injected validator returns only allowed fields. Core adds
`diagnostic_version` and `diagnostic_source`, saves that public projection, and
uses the latest attempt's projection for the next selection and repair. Under
[sequential routing](sequential-routing.md), CloudyFan permits at most three
generation stages and two selections; baseline modes retain two generations.
A strict `False` produces `failed` if the final repair does not pass; `None`
produces `unverifiable`. Neither permits unbounded retries. Changing schema, provenance, size, or contract version
rejects reuse of the same run. Identical completed runs retain their answers and
make no new model requests.

The application is responsible for the correctness and public provenance of
validator values. A schema and forbidden structured-field checks **cannot detect
all secrets embedded in allowed prose**. Do not derive diagnostics from hidden
answers, hidden grader outputs, or another experiment arm. Structured private
fields in diagnostics and tool results are rejected before downstream model
requests. Runtime receipts and tool checkpoints remain private evidence.

## Tool failure and replay boundaries

- Malformed invocation structure stops orchestration with retained model receipt.
- Invalid JSON/non-object arguments produce a public `invalid_arguments` content
  error without executing the tool. Application-specific argument semantics
  remain the registered tool's responsibility.
- Execution exceptions stop as `ToolExecutionError` with a redacted message;
  the persisted intent records that the effect may already have occurred.
  Trusted application control-plane errors marked `control_plane_error = True`
  propagate unchanged for budget/privacy status handling.
- External and undeclared tools are never automatically replayed after a prior
  intent, including when their result was saved before a later interruption.
  `ToolReplayBlocked` requires application/operator recovery; it does not claim
  a rollback, cancellation, or refund.
- A trusted environment may explicitly declare `replayable_tools =
  frozenset({"write_workspace"})` **only for deterministic local effects in a
  freshly isolated workspace**. Its declaration is frozen for incomplete-run
  recovery, and replay results must equal saved results. Payments, notifications,
  remote state changes, and other external effects must never be declared here.

Existing deterministic local tool integrations must add the explicit replay
allowlist. Tool-less validators need no migration. The schema addition changes
the run manifest; old run directories without this schema must not be silently
migrated or replayed. Original receipts remain available for separate audit.

## Evidence boundary

Tests exercise `Core.run`, injected public validators and `httpx.MockTransport`.
They establish schema propagation, bounded repair, field rejection, retained
answers, and replay protection on synthetic tools. They do not establish actual
validator accuracy, arbitrary-text secret detection, external delivery recovery,
or production deployment.
