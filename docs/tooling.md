# Verification and delivery tools

The primary local checkout is `cloudyfan-monitor/`. Its Git history continues the
accepted Issue20 integration; no remote is added by these tools. Parent project
instructions still apply. Product runtime, paid research and qualification
records keep their existing authority boundaries.

## Current status

```sh
python3 scripts/acceptance.py status
```

This reads the single `runtime/tooling/current.json` pointer, validates its sealed
delivery receipt and every preserved file, then compares current Monitor and core
inputs. `verified` means the recorded offline verification inputs still match;
`stale` exits2 and preserves the prior result as history. A missing receipt exits
nonzero. Markdown-only changes receive a separate local file-link check, without
running the full suite or changing its original verification timestamp. Status
reports full-file matches and verification-input matches separately. JSON
records, examples, build files and policies remain verification inputs. The
Markdown check does not check external URLs or anchors. New or changed root
pytest, conftest, Ruff, uv and build configuration files are verification inputs.
Older deliveries without an output-evidence seal remain historical results and
cannot establish a fresh `verified` state.
Tracker state is a timestamped observation, not a live GitHub query. Current
payment, model qualification and deployment authority never follow from this
status. Older Issue20 documents describe their recorded89/1921 scope.

## Quick checks and chained hook

```sh
python3 scripts/check --quick
python3 scripts/hooks.py install
python3 scripts/check
```

Quick mode runs Ruff (including the extensionless runner) and all product/QA
JavaScript syntax checks. Full mode also runs every Monitor test and an offline
sdist/wheel build. Both record running steps before launch and stream private
logs immediately, sharing core's `run_step` lifecycle and `run_logged` implementation.
All three entrypoints close an interrupted active step with its exit status,
finish time and elapsed time before recording overall interruption. Quick success is
not a completed delivery. A terminated process leaves an unfinished receipt;
Ctrl-C records interruption after reaping its own child.

The hook installer changes only this repository's local Git config. It runs the
existing secret hook first and refuses to skip a missing prior guard. It then
runs the quick checks. Install is idempotent. Inspect staged changes before a
commit; quick checks inspect the working tree and cannot certify a staged subset.

## Frozen acceptance

From the Monitor checkout, choose a new private output directory each time:

```sh
python3 scripts/acceptance.py snapshot --core-root ../cloudyfan-mrc --output runtime/acceptance/run-name
python3 scripts/acceptance.py run --snapshot runtime/acceptance/run-name
python3 scripts/acceptance.py finalize --snapshot runtime/acceptance/run-name
python3 scripts/acceptance.py status
```

Snapshot validates inputs before copying, detects concurrent edits, saves modes
and hashes and excludes actual `.env`, runtime and caches. Its core scope retains
current tests and the exact17 historical hashes already approved in
`config/acceptance-scope.json`; historical originals remain untouched. A new or
changed numbered test stops preparation, rather than adding a silent ignore.
This policy records the existing human decision and grants no new exclusion.
Its17 path/hash pairs are pinned independently in the command. An alternate
`--historical-policy` location must contain the identical approved pairs; adding
or changing a row is rejected before creating output. A future scope change
requires an explicit human decision and a reviewed code/policy update.

Run verifies the snapshot, performs locked offline dependency sync, saves both
actual collection manifests, invokes the core's original integration command
and the Monitor full check, and compares passing counts with collected scope.
Collection includes every configured pytest test path, including directories
outside `tests/`; the identifier count must match pytest's reported total.
It clears inherited pytest filters, pins `UV_PROJECT_ENVIRONMENT` to the frozen
core's `.venv`, and uses only the frozen source paths. Caller environment
redirection cannot select another environment for dependency synchronization.
Dependency preparation must already be cached; no fallback download is made.
Build isolation follows declared build constraints, not a byte-identical build
claim. Before recording success, run seals the generated output paths, sizes
and hashes, including required raw step logs and collection logs/manifests.
Collection logs can contain private parameter data; keep runtime local.
Security tests deliberately leave symlinks in their evidence. By human decision
(2026-10-29), run records each link's path and target relative to the snapshot
without following it; a target outside the snapshot fails the run. FIFOs are
recorded by type and path only and are never opened. Finalize requires the same
links and FIFOs, copies only regular files and keeps their records in the
delivery receipt; status compares them with the sealed run.

Finalize verifies the successful run and matching destination verification inputs
(with separate checks for changed Markdown), then
checks that sealed outputs still exist and match their recorded hashes, then
preserves every non-cache snapshot file, including intermediate failures, under
`runtime/deliveries/<run-name>/`. Existing different artifacts are rejected.
Repeating finalize validates and reuses the immutable receipt. It updates only
the current pointer; it does not overwrite product source or delete data.
Missing or altered step logs, collection evidence or seals fail before publishing
current status. Every status read rechecks the preserved seal. An older unsealed
run must be preserved and replaced by a new verification run, not relabelled.

To also validate historical indexed artifacts or record a tracker observation,
pass `--evidence-index <index.json>` and `--tracker-receipt <observation.json>`.
Use relative artifact paths within the destination; escapes are rejected.
The indexed historical hashes are sealed into the delivery receipt and checked
again by every status read; their earlier failures remain failures in history.
Cleanup remains a separate exact-target approval process.

## CI configuration

The workflow uses the same snapshot/run commands on a hosted Ubuntu runner.
Before enabling it, configure repository variables:

| Variable | Required value |
| --- | --- |
| `CLOUDYFAN_CORE_REPOSITORY` | Approved `owner/repository` containing core source |
| `CLOUDYFAN_CORE_REF` | Exact40-character commit, not a moving branch |
| `CLOUDYFAN_CORE_INPUT_SHA256` | `core_input_sha256` from a snapshot of that exact checkout |

For a private core repository, its already-authorized read credential may be
provided as `CLOUDYFAN_CORE_READ_TOKEN`. Otherwise checkout uses the existing
GitHub token. These tools do not create credentials or broaden access.

`python3 scripts/acceptance.py ci-config` fails before core checkout when the
configuration is missing or mutable. Snapshot rejects an input digest mismatch.
CI dependency preparation may access normal package indexes before the offline
gate, matching the core workflow. Runtime evidence is not uploaded as an artifact.
A local pass and workflow file do not establish that remote CI ran. No push or
workflow dispatch is performed by these commands.
