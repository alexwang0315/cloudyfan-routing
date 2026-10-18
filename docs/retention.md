# Explicit retention lifecycle and approved maintenance

`RetentionPolicy(version, ttl_seconds)` enables a private TaskService lifecycle.
It creates no timer, server or automatic cleanup. `None` preserves existing
unmanaged testing behavior, but cannot disable an already-created retention
namespace. RG-05 remains pending until the embedding application qualifies its
identity, maintenance fence, ownership, storage and operator approval process.

## Lifecycle and access

```python
from cloudyfan_jev.http_api import TaskService
from cloudyfan_jev.retention import RetentionPolicy

service = TaskService(
    private_task_root,
    contracts=registered_contracts,
    allowed_principals=approved_principal_ids,
    principal_active=identity_store.is_active,
    retention_policy=RetentionPolicy('customer-retention-v1', 7 * 24 * 3600),
    retention_protected_paths=(identity_store.path,),
)
```

The explicit server allowlist and `principal_active` callback are checked before
create, execution, status and result. Callback failure denies access. Credentials
and principal lifecycle are composed separately; changing a credential cannot
change a task namespace or grant another principal access.

The lifecycle database is private 0600 SQLite under
`<private_task_root>/.retention/lifecycle.sqlite3`, outside the task directories.
It stores only the internal task ID, creation/expiry timestamps, policy version
and TTL, queue marker, retirement state and safe billing audit. It contains no
prompt, output, workspace content, raw request or credential. Permanent lock files
coordinate operations on the same task across TaskService instances/processes.

A valid create records the clock once; invalid payloads/contracts do not start a
TTL. `clock` can be injected for offline tests and must return finite Unix seconds
as int/float. A persistent clock floor rejects backward time with 503, preventing
an accidental rollback from extending access. Reaching the exact expiry timestamp
makes every task lookup, result read, explicit resume and duplicate create return
410. The expired decision persists across restarts, even if the clock subsequently
moves backward. A retired task always returns 410. Neither state permits a paid
retry or reuse of that principal/task ID.

Existing jobs without lifecycle metadata are rejected when retention is enabled.
A changed policy is rejected; there is no silent TTL extension, implicit migration,
metadata reset or tombstone expiry. A missing lifecycle database is not recreated
inside an already-populated metadata directory. Back up its prompt-free audit and
recover it through a separately reviewed procedure if lost.

Expiry removes access, not evidence. A running call is not forcibly cancelled;
its budget/receipt evidence remains, and a completed write may be followed by 410
when the response is read. Interrupted or unknown-accounting workflows must retain
all original evidence. There is no automatic storage deletion on a request path.

## Exact preview

`service.retention_preview(principal, run_id)` is an operator-only Python seam;
there is no HTTP deletion endpoint. It performs no model POST, reconciliation GET,
queue erasure or file removal. The owner must still be admitted by the service.
Administrative cleanup for a deactivated owner requires a separately scoped
operator identity/workflow; callers must not reactivate the account just to purge.

The returned versioned plan contains:

- Internal task ID and the pinned lifecycle/policy.
- Exact TaskService task directory, Core run directory and matching cloud/local
  call directories, each with its allowed root and full relative file inventory.
- Per-file content SHA256, bytes, mode, device, inode and modification timestamp;
  directory identities, target aggregate digest, total bytes and open-file state.
- Safe billing event/receipt IDs, verified costs and a queue request digest/size.
- Eligibility, all blocking reasons and a SHA256 over the whole immutable plan.

It never returns raw prompts, answers or receipt content. Private workspace file
names and absolute paths are operator data; do not publish preview plans.

A supported purge requires all of the following: expiry (or prior retirement),
a completed task and reservation, every owned event fully verified with a receipt,
no shared unknown fee/violation/unbounded history, matching ledger/call inventories,
a safe pinned contract and no open or unknown targets. Provisional known response
cost is insufficient. A local no-fee assertion alone is not independently verified
billing evidence; such an event remains ineligible under the current budget model.

Symlinks, hardlinks, special files, path traversal, missing receipt evidence,
overlapped targets and protected paths block the operation. Shared BudgetStore,
lifecycle database and explicitly registered identity/auth paths are protected.
Pass every external auth/storage database through `retention_protected_paths`;
the service cannot discover arbitrary private application databases. Backups and
copies outside its managed roots are not inventoried or deleted.

`service.retention_open_files(path)` returns a list of open paths (empty means
confirmed closed), or `None` when inspection is unavailable/partial. The default
uses a bounded local `lsof` scan and fails closed. The permanent top-level `.lock`
handle is excluded because it is held and preserved by maintenance. A fixture
callback returning `[]` is test evidence only, not proof of real closed files.

## Explicit approval and maintenance apply

Review the exact preview, its size, risk, eligibility and open-file state before
obtaining the human operator's item-specific approval. A policy configuration or
API credential is not deletion approval. `retention_apply` requires an explicit
reference to that approval and the digest of the whole plan:

```python
preview = service.retention_preview(owner, run_id)
# An actual operator reviews and approves this exact immutable preview first.
result = service.retention_apply(
    owner, run_id, preview,
    approval={'plan_sha256': preview['sha256'], 'reference': approved_reference},
)
```

This code is an integration example, not authorization to erase current data.
Changes to any item, hash, queue request, lifecycle, billing or open-file state
invalidate approval and require a new preview. No plan is auto-approved.

Before apply, the embedding application must inject
`service.retention_maintenance(internal_id)`: a context manager that actually
stops/fences all application writers for the owned roots and yields
`{'workers_stopped': True, 'evidence_ref': 'reviewed-maintenance-reference'}`.
A mere callable or false/unconfirmed assertion is rejected. The reference is an
operator attestation; Python cannot authenticate it. The fence must remain held
throughout apply. QueueScheduler supplies its own persisted pause fence and
rejects running/interrupted workers; a broader application must fence all of its
additional writers as well.

Apply holds lifecycle, task, Core run and cloud/local client advisory locks,
rechecks the entire inventory and accounting, and commits a permanent lifecycle
tombstone before irreversible erasure. It also writes a prompt-free Core
`.retired.json` sentinel; Core rejects subsequent run/result recovery for that ID.
Task/Core lock inodes are preserved, preventing creation of a second lock while
another process still holds the old one.

The billing audit retains verified event/receipt IDs, costs, receipt target hashes,
approval/maintenance references and plan/billing digests. Shared budget and auth
databases are not removed. Only approved regular files are unlinked; approved
empty directories are removed with parent-directory fsync. Unexpected late files
are preserved and make the operation stop. An interruption after retirement keeps
the task permanently inaccessible and never resets or resends it. Partial cleanup
requires a new operator investigation/approval; it is not silently retried.

Advisory locks and the trusted maintenance fence qualify only application-owned
namespaces. Arbitrary trusted code that ignores those locks, external root access,
filesystem substitution attacks, storage snapshots, backups, SSD remanence and
secure forensic erasure remain outside this implementation's qualification.

## Queue request copies

A queued task calls `service.mark_queued(principal, run_id)` before dispatch.
Its plan is ineligible unless both hooks are configured:

- `retention_guard(internal_id)` returns exactly `terminal`, `request_erased`,
  `request_sha256`, `bytes`, `evidence_ref`. It must confirm a terminal queue row,
  retain the original request SHA256 even after erasure, and report bytes 0 only
  after the sensitive request has been removed. Additional fields are excluded
  and invalidate the evidence, preventing accidental prompt projection.
- `retention_erase_queue(internal_id, expected_request_sha256, approval_reference)`
  erases only that terminal matching request under the maintenance fence.

Queue erasure occurs after the durable tombstones and before exact file cleanup.
The service confirms terminal state, original digest and erasure afterward.
A missing hook, active queue row, changed request or failed erasure blocks or stops
cleanup. The shared queue database is kept; application SQL cleanup is logical
row erasure, not a claim that backups or storage remanence have disappeared.

## Offline evidence

Regression tests use disposable synthetic fixtures, HTTP WSGITransport and mocked
upstream. They cover expiry/restart/no resend, principal revocation, policy changes,
missing metadata, rollback, accounting stops, exact approval, open-file uncertainty,
unsafe links/protected targets, queue copies, writer locks, plan drift and durable
retirement after an interrupted cleanup. No current user data is deleted by these
tests and no deployed identity/storage compliance is established.

## Confirmed never-dispatched terminal tasks

A cancelled/deadline-expired queue job may still have service status `ready` and
no paid workflow reservation. It can be purged with explicit zero-fee evidence
only when there are no budget events/reservations, no Core run path, no matching
cloud/local call artifacts, safe known namespaces and a confirmed terminal queue
row. Shared unknown accounting still blocks it. An orphan file, partial Core run,
unknown directory or active reservation defeats this absence proof.

The preview marks `never_dispatched` and identifies the permanent Core marker
creation path. Approved maintenance retains `verified_api_usd: "0"`, creates the
Core retirement marker and erases the exact task/queue copies. It never invents
an API generation ID, reservation or paid receipt to make the plan complete.
