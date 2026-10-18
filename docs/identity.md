# Operator-managed service identities

`IdentityStore` provides a concrete local API-token registry for HTTP, Ollama text
and queued task entry points. It replaces a fixture authenticator when embedded by
the service operator. It does not start a server, create a public enrollment
endpoint, integrate OIDC/SSO, or establish production identity qualification.
The existing server principal allowlist and task-owner isolation still apply.

## Registry and operator lifecycle

```python
from cloudyfan_jev.identity import BearerAuthenticator, IdentityStore

# Load >=32 unpredictable secret bytes from operator-controlled secret storage.
# The pepper is not the API token and must not be stored beside the registry.
store = IdentityStore(private_registry_path, pepper=operator_secret_bytes)
store.create_principal('client-one')
credential = store.issue('client-one', scopes={'tasks:read', 'tasks:write'},
                         expires_at=explicit_expiry_unix_seconds)
authenticate = BearerAuthenticator(store)
```

`IdentityStore(path, *, pepper: bytes, clock=time.time)` creates a SQLite registry
with mode `0600` in a private owned directory (`0700` for a newly created path).
Existing broad permissions are rejected rather than silently repaired. Ancestor
symlinks, database/companion symlinks, non-regular database files and hard links
are rejected. Checks repeat before each database transaction. This assumes the
operator and other processes of the same OS user are trusted; it is not protection
against a privileged attacker replacing directories during filesystem operations.

The operator APIs are:

| Method | Behavior |
| --- | --- |
| `create_principal(id)` | Register one enabled account; duplicate registration fails. IDs are bounded opaque service identifiers, not email addresses. |
| `principal(id)` | Return safe ID, enabled state and creation time. |
| `set_active(id, bool)` | Disable or re-enable the account. Every authentication checks this current state. Re-enabling restores otherwise valid unrevoked credentials. |
| `is_active(id)` | Return the current enabled state, or false for an unknown/invalid ID. This is suitable for the service's worker dispatch activity check. |
| `issue(id, *, scopes, expires_at)` | Issue an opaque random credential for an enabled account with explicit supported scopes and a finite future Unix expiry. |
| `revoke(credential_id)` | Revoke persistently; repeated revocation preserves the original time. |
| `rotate(credential_id, *, expires_at, scopes=None)` | Atomically issue a replacement and revoke the active old credential; preserve scopes unless explicitly changed by the operator. Invalid replacement inputs roll back the entire change. Two competing rotations cannot both succeed. |
| `credentials(id)` | Return credential IDs, principal, scope names and lifecycle times, without tokens or their stored digests. |

Issuance/rotation returns an `IssuedCredential` with `id`, `principal`, `scopes`,
`expires_at`, and `token`. Deliver the token once through a protected channel.
Its `repr` hides the token, but explicitly reading `.token` or serializing all
dataclass fields can reveal it: do not log or publish those values. The registry
stores an HMAC-SHA256 digest, never the plaintext token; comparison is constant
time. Keep the pepper separate, back up both under controlled access, and use the
same secret when reopening. A mismatched pepper is rejected. Loss of the pepper
requires an operator recovery process and credential replacement; no automatic
secret recovery or pepper rotation is provided.

Transactions use SQLite `BEGIN IMMEDIATE`, parameterized statements, foreign keys
and rollback. No global process cache can override a committed expiry,
revocation or account disable. The registry transactionally persists a maximum
observed wall-clock floor during authentication and timed lifecycle operations.
A backwards or nonfinite clock denies authentication, including after reopening;
an observed expiration cannot be revived by rolling the clock backwards. A
credential is also denied before its creation time. A forward clock jump can
hold access until the trusted clock catches up to the persisted floor; no HTTP
or automatic floor-reset bypass exists. A trustworthy OS clock and reviewed
backup/restore operations remain deployment requirements. A request already authorized before a lifecycle
change is not retroactively cancelled. Disabling an account must also be wired to
the worker dispatch check; it does not cancel an already sent provider request.

## Bearer authentication and server-owned scopes

Pass `BearerAuthenticator(store)` as the existing `authenticate` callback to
`HTTPApp`, `OllamaApp` or `QueueHTTPApp`. It returns a trusted principal string or
`None`, never client-provided roles. Operators grant scopes out of band; there
are no HTTP account, grant, issuance, revocation, or administrative endpoints.

| Request | Required scope |
| --- | --- |
| GET task status or result | `tasks:read` |
| POST task submit, explicit resume, or queue cancel | `tasks:write` |
| POST Ollama single-text generate/chat | `tasks:write` |
| GET Ollama tags | `models:read` |

The authenticator derives the scope from the method and exact supported route,
ignores body/header role declarations, and denies unsupported routes. The app
still decides which routes it implements: authorizing a scope never enables an
unsupported resume or native protocol feature. There is no wildcard scope or
implicit read permission in `tasks:write`. Unknown, malformed, expired, revoked,
disabled-account and insufficient-scope credentials are denied. Invalid input
and token values are not logged. Existing app error/audit projection remains
redacted; registry or clock failures return no principal rather than falling back to anonymous
or fixture authentication.

HTTPS is required through trusted WSGI `wsgi.url_scheme`. Forwarded headers are
ignored. A TLS proxy must provide an independently trusted scheme through its
WSGI server configuration; do not copy arbitrary `X-Forwarded-Proto` into it.
`allow_insecure_loopback=True` is an explicit development/test opt-in requiring
HTTP and an IP-literal loopback `REMOTE_ADDR`. Host headers, `localhost` strings,
and remote addresses do not establish loopback permission. This opt-in is not a
production network security qualification.

For queue services, use the registry's `is_active` callback in the service's
principal activity guard as well as `BearerAuthenticator` at the HTTP boundary.
Construct the registered service with `principal_active=store.is_active` and
the existing explicit `allowed_principals` allowlist, then wrap it in the
scheduler. Changing an account's registry state never expands that allowlist.
This rechecks an account disabled after enqueueing. Credential expiration or
revocation stops new client requests; it does not implicitly revoke an already
accepted job. Request-scoped roles, running-job cancellation and provider billing
remain separate contracts.

## Offline evidence and remaining RG-05 scope

`tests/test_identity.py` exercises the public registry/operator API and real WSGI
HTTP/Ollama/Queue boundaries with the existing mocked upstream. It covers restart,
scope separation, expiry, clock rollback/restart, revocation, disabled accounts, rotation races, rollback,
transport policy, private paths and redacted storage. These are synthetic local
tests, without real account credentials or provider calls.

This module does not delete identities or credentials, run retention cleanup,
configure backups, rate-limit a network edge, or sandbox contract tools. Those
are explicit operator/application integrations. Formal RG-05 still requires a
named identity/retention policy owner, actual deployment access controls and
backups, approved data handling, lifecycle review, and the applicable retention
integration evidence. See [HTTP task API](http-task-api.md),
[bounded scheduler](bounded-scheduler.md), and
[release readiness](release-readiness.md).
