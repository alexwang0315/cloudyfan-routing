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

## S01-S08 semantic review ownership

root owns documentation/evidence preparation only for the active semantic review plan.
Runtime diagnostic methods are retained privately. Executable/test/build inputs, frozen
catalog and formal gate records are not edited; formal owners/reviewers remain unassigned.

## Delegated RG-01 scope decisions

root owns documentation and the prospective policy under the active plan, preserving
the preceding uncommitted semantic review. The workspace root ADR is an external
documentation artifact, not a new standalone-package runtime dependency. Formal
identities and source/runtime implementation are not changed by this decision work.

## RG-01 remaining-ticket implementation

root is the sole editor of SQL contract/policy/catalog/isolation interfaces, public-boundary tests, qualification tooling and current records. Core.run, Client, budget and routing policies are consumed unchanged. Two independent read-only reviews will follow implementation under the code-review skill. Original uncommitted semantic/decision documents and historical evidence are preserved. Real Linux qualification and human signoff are separate from this offline implementation.

## Scout dedicated environment preparation

root owns preparation evidence/docs and the operational scripts retained privately under runtime. No application executable inputs change. Host provisioning is explicitly authorized by the user, but SSH sudo authentication is pending. Existing Scout project, containers and service configuration are outside the edit scope; no stress/attack suite or production deployment in this step.

Preparation completion: the user executed both reviewed sudo bootstraps. Root-owned
v2 read-only artifacts and a fresh delegated transient user scope passed actual
strict-worker startup, known public SQL validation and empty-leaf cleanup. Root
retains sole ownership of these preparation records. Original startup failures,
old assets and receipts are preserved. No further sudo action is pending for
this preparation; RG01-06 adversarial evidence and RG01-08 formal review remain
blocked separately. See [environment record](rg01-scout-environment-20261019.md).


## RG01-06 controlled target evidence

root owns the qualification CLI, fixed trusted OS probes, mock-only Core.run interruption/resume checks and new evidence. Preserve the completed preparation documents and artifacts. No runtime application interface changes are planned. All target probes run serially within the prepared worker leaf; no global host policy, existing services, model API or formal review/registry change.

Completion: root integrated the opt-in target CLI and four regression tests.
Independent read-only AI review found one cancellation-evidence P2, now fixed
and regression-tested; evidence re-review found no remaining blocker. Normal
8/8 and target 7/7 passed on Scout; final offline gate passed 1,102 tests.
RG01-06 is technically complete. RG01-08 requires an actual named formal
reviewer; none is assigned or signed by the AI. No source commit/push occurred
during that target-evidence turn.


Precommit scope: the user subsequently authorized checking and committing this
RG01-06 change locally, including its linked preparation records. root owns
staging and the local commit; no remote write, deployment or paid API. Runtime
receipts remain untracked; only sanitized summaries and hash references enter Git.


## RG01-08 existing-evidence technical review

root owns integrity checks, review synthesis and current documentation. Independent read-only semantic and control-plane reviews may inspect existing artifacts; no source edits, Scout execution, model calls, signatures or qualification-state changes. Formal-review task remains blocked and unassigned.

Technical-review completion: both read-only AI review lanes found no new blocker
within the finite claim; root independently replayed trusted reference SQL on
existing public data and rechecked indexed/raw hashes. Review actors are AI,
not named formal approvers. Current output separates conditional scope, explicit
limitations and three human confirmation categories. Formal task remains blocked;
registry/qualification fields and executable source are unchanged.

Subsequent user authorization: root reviews and commits only these RG01-08
documents and metadata locally. Recheck source/evidence hashes, links and formal
status before staging; preserve private receipts outside Git. No signoff or
qualification-state change, paid API, deployment or source push.

## Direct RG01 execution testing

User directs no further S01-S08 review and immediate routing-core tests. root
owns focused SQL execution checks, the full offline integration gate and four
route demonstrations. Formal RG01-08 is preserved in the prior plan and ticket
as deferred/nonblocking for tests; no signature or registry promotion. No paid
API, deployment, source push or new host operation.

## Live RG01 integration fix

Authorized live pilot reached seven completed workflows; S08 saved answer hit
an unrecognized compound ORDER BY compiler error and stopped as engineering.
root owns the narrow compiler classification/regression fix. Preserve the v1
plan, stopped run and paid answer; use a separately versioned continuation for
repair only, never relabel v1 as an uninterrupted successful cohort. No new
initial answer, deployment, source push or formal signoff.

## SQL live-integration local commit review

The user authorizes reviewing and committing the SQL compiler fix plus linked
offline/live records locally. root owns explicit-path staging and preservation
inventory. No new API calls (including receipt refresh), deployment, source
push or qualification changes. Inspect raw local receipts without publishing
prompts/answers; retain original interruption, v2 continuation and GET404 history.

## Fresh 50-task four-arm trial — 2026-10-19

User authorizes 50 new tasks, frozen CloudyFan/fixed-high/low-first/open-source-first
flows and all classifier/repair/failure costs. No S01-S08 re-review, deployment
or push. root owns the live runner, immutable plan, accounting, held-out grading
and report. A delegated dataset worker owns only runtime/fresh50-four-arm-20261019/
dataset.py and test_dataset.py, without API or credential access. No core policy
change or holdout-based tuning; actual public pass gates the sole repair.

Fresh50 completion: dataset and report subtasks are finished. The final report
and sanitized evidence are docs/fresh50-four-arm-20261019.md and
 docs/evidence/fresh50-four-arm-20261019.json. All 50 x four outcomes and costs,
including 70 policy-zero upstream429 cases, are preserved; runtime remains
private/ignored. Source, profiles, tasks and scoring contracts stayed frozen.
No implementation, deployment, push or formal qualification follows automatically.

## Cost-resolved provider429 recovery — 2026-10-19

The user authorizes bounded provider429 recovery plus offline regression tests.
root owns the Core.run interface and the client rejection recognizer under
`provider-429-bounded-escalation`; there are no delegated or concurrent source
edits. Generation-only, explicit no-answer429 may consume the current attempt
only after existing fee/unique-ID checks succeed and provider/request evidence
matches. CloudyFan uses the existing binary selector, low/OSS baselines use high;
fixed-high never retries the rejected configuration. Existing budget bounds,
unknown-charge stops and classifier failure handling are unchanged. Preserve the
fresh50 runtime and report; use new engine-version manifests for future work.
No paid API, deployment, push or formal qualification change is authorized.
