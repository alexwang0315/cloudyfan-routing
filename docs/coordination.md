# Shared interface ownership and integration

The machine-readable work plan is [work-plan.json](work-plan.json). It declares the
integration branch, full baseline commit, merger role, task paths and dependency
order **before** parallel edits start. `python scripts/work_plan.py` checks the
plan offline; `validate_plan(data, root) -> list[str]` is also available to the
shared project-state checker. An empty error list means declarations are coherent,
not that an implementation or release is qualified.

## What changed after spec #1

The original T01/T05 integration combined three Core conflicts, and T06 required
additional local-transport interaction validation. These are recorded in the
workspace's historical `.scratch/implement-spec-1/merge-log.md`; the historical
coordination note's “implementation work in progress” is not current status.
The new plan gives shared interfaces one accountable **role**, then names the
interface-producing task when a change is required. Roles such as
`integration-maintainer` are responsibility labels, not claims that a specific
person has accepted a production responsibility.

Existing interfaces retained unchanged in this round:

| Interface | Owning file | Accountable role |
| --- | --- | --- |
| `Core.configuration_descriptor()` | `src/cloudyfan_jev/engine.py` | integration-maintainer |
| Filesystem receipt store | `src/cloudyfan_jev/receipt_store.py` | integration-maintainer |
| Safe task result projection | `src/cloudyfan_jev/http_api.py` | integration-maintainer |

The retro 3–5 and five-review-fix plans are retained as historical evidence.
The RG-04/RG-05 plan is also preserved as historical evidence. The current G-02
plan has one integrator owning SDK dependency pinning, protocol adaptation,
public-boundary tests and documentation. It consumes the unchanged TaskService
interface; no independent workers are changing shared files in this scope.
The current RG-04 component-bound plan supersedes that G-02 plan, retaining its
records in `docs/evidence/rg04-components-prior-work-plan.json`. One integrator
owns the alternative verifier, exact shared-budget arithmetic, pinned classifier
dispatch, tests and handoff. The existing durable Client lease hook is consumed
unchanged. An independent agent performed read-only review, with no file ownership
or edits. The current plan, not older ownership tables, identifies active owners.

The subsequent `rg04-zero-rate` plan supersedes the component-bound work plan,
preserved as `docs/evidence/rg04-zero-rate-prior-work-plan.json`. One integrator
owns the versioned component-evidence interface, its public-boundary regression
tests and documentation. Budget and transport interfaces are consumed unchanged;
there are no delegated edits. Workspace-root glossary/ADR/README updates record
the same user-confirmed zero-rate decision outside the standalone Git repository.

## Before dispatch and merge

1. Record exact project-relative paths, owner role and task status. Do not assign
   the same active path to independent tasks. Directory overlaps count too.
2. For a shared interface change, set `provided_by` to its task, give that task
   the same accountable owner and files, and bind consumers through
   `consumed_interfaces` and `depends_on`. Existing baseline interfaces explicitly
   use `provided_by: null` and must exist on disk.
3. Review the interface signature, compatibility expectations and acceptance
   behavior before consumers integrate. For example, a future descriptor change
   needs an interface-producing task and dependent transport consumers; do not
   create those as active jobs until the work is actually authorized.
4. The merger role verifies producers and consumers together with
   `./scripts/check --integration`, reviews actual diffs/conflicts and records
   the evidence in the current handoff. Mark completed only after its checks.
5. Preserve unrelated edits. Update ownership before taking over another
   task's file, rather than using a dependency declaration to disguise
   simultaneous conflicting edits.

Schema version 1 requires every task to have `id`, one `owner` string,
`status` (`planned`, `in_progress`, `blocked`, `completed`, `cancelled`),
`owned_files`, `depends_on`, and `consumed_interfaces`. Active tasks include
planned/blocked work so conflicts are found before edits. Duplicate IDs/references,
unknown dependencies/interfaces, dependency cycles, unsafe paths, unordered active
path overlap, missing provider binding, and inconsistent interface ownership fail
validation. Planned files can be absent; existing baseline interface files cannot.

## Limits

This is a declaration checker, not a scheduler, lock service or workflow engine.
It cannot discover undeclared consumers, validate semantic compatibility, prove
that contributors obeyed an order, or judge whether changes preserve behavior.
The baseline hash is format-checked only: existence, actual branch ancestry and
merge inclusion require explicit Git checks. Historical implementation evidence,
current work status and production release gates remain separate. The checker
performs no API requests, dispatches no agents, creates no worktrees and merges
nothing.


## RG-01 SQL Environment 接入

目前 `rg01-sql-environment` 計畫接續 `rg04-zero-rate`，原 state／plan 已保存於 `docs/evidence/rg01-sql-prior-state.json` 與 `rg01-sql-prior-work-plan.json`。單一 integrator 擁有 `sql.py`／`sql_executor.py`、公開接入測試及文件；消費既有 Core.run，不改 engine、Client、BudgetedCore、diagnostic schema 或正式 registry。沒有平行改檔／代理人派工。唯一修正及原付費回應重用規則保留。

## RG-01 review-ready ownership

root owns sql.py, the worker/process boundary, public review catalog, regression tests and
review-ready evidence/docs under the active work plan. No routing/diagnostic schema/
provider/budget interface change or independent-human approval is implied. Preserve v1
receipts and failure evidence. Formal gate statuses are unchanged.
