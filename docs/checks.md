# Unified offline checks

Run from this checkout (or pass its absolute script path):

```sh
./scripts/check                 # locked dev environment, dependency consistency, project records, Ruff, pytest
./scripts/check --integration   # also both mocked high-route demos and sdist/wheel build
./scripts/check --quick         # environment, dependency consistency, project records and Ruff only
```

All modes first run `uv sync --locked --offline --extra dev --no-python-downloads`.
Every mode then validates the [current handoff](handoff.md), release records and
shared-interface plan. Pending production gates are valid; fabricated or stale
qualification evidence fails. A record failure stops before Ruff, tests or demos.
Tests and Ruff execute with `.venv/bin/python -m ...`, never a global pytest binary.
The runner fixes the environment location to this checkout and removes inherited
PYTHONPATH, PYTHONHOME and VIRTUAL_ENV. Each failing command stops the gate and
returns nonzero; no pip/network fallback is attempted. Missing uv exits 127.
Python 3.12+ and uv must already be installed. Tests retain the socket guard.

JSON step/exit-code receipts are written to a fresh ignored `runtime/checks/<id>/`
directory. Integration demos and build artifacts stay beside that receipt, so
repeated checks do not reuse paid-looking demo results or overwrite evidence.
The runner does not delete prior outputs. Quick mode does not certify tests or
packaging. Console output retains the actual test summary.

## Dependency preparation versus verification

A fresh machine without cached dependencies fails explicitly in offline mode.
After permission to download ordinary development dependencies, prepare them:

```sh
uv sync --locked --extra dev
uv build --out-dir runtime/build-preparation
./scripts/check --integration
```

The first two commands may access package indexes; they do not call model APIs.
The build command warms uv's separate isolated build cache. Application and test
dependencies use uv.lock; isolated build dependencies follow `[build-system]`
constraints and are not locked by that file. Thus this gate is not a claim of
byte-identical builds. Cached build packages and runtime installation packages
are not interchangeable caches. Do not add a fallback download inside `check`.

The existing GitHub workflow prepares dependencies, then invokes the same
integration entry on macOS/Linux with Python 3.12/3.13 and uv 0.11.7. Local success
does not establish that remote CI ran. No workflow dispatch or source push is
part of installing this entry.

## Repository-local commit guard

```sh
python3 scripts/hooks.py install
```

Installation changes only this repository's Git config. It creates a managed hook
directory in the common Git directory. The previous pre-commit hook runs first;
its failure blocks the commit before quick checks. If a previously active hook
disappears, the new hook stops rather than silently omitting secret scanning.
Other currently present hook files are preserved by symlink. The global hooks
and global Git configuration are not edited. Repeating installation is a no-op.

The quick check scans the checkout, including unstaged changes; it does not prove
that an independently staged subset passes tests. Run the full gate for delivery
and inspect the staged diff before committing. Hook execution can be bypassed by
Git options, so it is a guardrail, not a security boundary.

The saved directory is `git config --local --get cloudyfan.previousHooksPath`.
If that directory later gains new hook filenames, review the managed directory
before adoption; existing symlinks track updates to existing hook files. The
managed pre-commit entry points to this checkout, so relocating/removing this
checkout also requires reviewing hook paths. Restoring the saved hooksPath is a
local config operation; deleting directories still follows the user's deletion
authorization rules. No global hook is replaced or weakened.

## Contract coverage and production gates

The full suite discovers the [transport contract matrix](transport-contracts.md).
The [release index](release-readiness.md) tracks actual-client, model, provider-cap,
hardware and deployment qualifications separately. An offline green gate cannot
change those states to `production_qualified`.
