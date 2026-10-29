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

Each new run writes a private `checks.json` before starting its first child.
The receipt starts with `status: running`, `passed: false` and a null
`returncode`. Each step's command, start time, running status and relative `log`
path are recorded before launch. Completion adds the step's finish time, monotonic
elapsed `seconds` and exit code, including failure and interruption. Only completion
of every required step sets the overall `passed` to true.
Receipt replacements are atomic, so a hard-killed runner leaves a readable
running/incomplete record instead of implying success.

Step logs (`01.log`, `02.log`, ...) contain the exact combined stdout/stderr bytes
and are flushed while the child is running; files and receipts use mode `0600`
inside a `0700` run directory. Console output is decoded incrementally as UTF-8,
with invalid bytes replaced only for display. Partial output remains available
after a failure. Ctrl-C signals only the child's newly created process group,
first with SIGINT so nested verification runners can save their own receipts and
cascade interruption. After a five-second grace period it escalates to SIGTERM,
then after five more seconds to SIGKILL if needed, and reaps its own child.
Unrelated processes are not signalled. It records `interrupted` with exit code 130
and starts no later steps. Existing step
`command`/`returncode` and overall `mode`/`steps`/`passed` keys remain available.
Do not treat a running record or an older completed run as current verification.

If the signal-zero existence query is denied, cleanup keeps the same bounded
wait and termination escalation on its owned group. Query denial does not prove
that descendants have exited. Errors sending actual termination signals still
fail the step; they are not converted into successful cleanup or interruption.

The core, Monitor and frozen acceptance entries share this public helper in
`scripts/check.py`:

```python
run_step(report, *, command, cwd, env, log_path, receipt_path,
         log_reference=None, step_fields=None, exit_key='returncode') -> int
```

It appends and atomically saves a running step before launching the child, then
closes that step exactly once with its status, exit code, finish time and elapsed
seconds. The default `log` is a POSIX path relative to the receipt directory;
`log_reference` preserves an entry's existing log convention. `step_fields` adds
metadata such as the acceptance step's `name`; lifecycle fields remain owned by
the helper. `exit_key` accepts only `returncode` or Monitor's existing `exit_code`.
The entry still owns report-wide completion and whether to raise after failure.

A caught interrupt returns 130 with step status `interrupted` after child cleanup.
A log or launch `OSError` returns 127 with status `failed`; its type and message
stay in the private step's `error`, and the console prints a fixed diagnostic.
Other child exits retain their raw return codes and stop the entry's later steps.
Acceptance re-raises interruption only after the helper saves the closed step.

The lower-level public `run_logged(command, *, cwd, env, log_path) -> int` helper
still streams exact bytes and owns process cleanup. Pass a fresh log path: it
refuses to overwrite a previous log. It returns the child's raw exit code,
propagates errors, and re-raises `KeyboardInterrupt` after child cleanup. It
installs no signal handlers and never signals the caller's process group.

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

雲端測試環境一律使用 uv 管理的 CPython（`UV_PYTHON_PREFERENCE: only-managed`）。
原因：`actions/setup-python` 在 macOS 提供的是 python.org framework 版，其
`sqlite3` 沒有 `enable_load_extension`，SQL 執行器會對每個查詢拒絕執行，第一次
雲端矩陣（run 40839825871）因此在 macOS 兩格各有 165 個失敗。`setup-python`
只用來執行 `scripts/check` 這個入口本身。

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
