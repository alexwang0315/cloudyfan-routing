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

## Scoped fee-policy integration and fresh50 — 2026-10-20

root owns the opt-in rejection-fee integration, tests, billing preflight and new
paired runner/report. The dataset worker owns only new runtime dataset.py and
test_dataset.py; no API/credential access or product-source edits. Prior trial
is read-only. New user authorization permits this separate paid comparison,
not deployment/push or a production qualification/signature.

Execution/hidden grading/account matching are complete; see
[the new50 report](fresh50-recovery-20261020.md). Source remained frozen, no
hidden-driven paid followup occurred, and no new429 was observed. General
public-validator false-accept coverage is the next technical gap, not a reason
to relabel failed answers. Keep the paid raw cohort ignored and preserve its
inventory; only sanitized summary and per-task metrics are documentation.

## Public SQL suite qualification — 2026-10-20

User authorizes offline F32/F48 diagnosis and generic public-validation
strengthening, preserving the paid cohort; no API, deployment or push. root
owns the new opt-in builder, synthetic regression tests, public-only offline
analysis and documents. A separate code-only reviewer read no runtime/hidden
data and found equal-valued bool/float input mutation; canonical type-sensitive
comparison and regressions address it. Native SqlContract/Core interfaces and
all previous routing/fee changes remain unchanged. Runtime post-hoc regression
is not a new blind experiment or an update to prior quality/cost outcomes.

## Fee-policy/report commit closure — 2026-10-20

root owns the explicitly authorized remaining fee-policy/report commit. A
code-only reviewer identified late-receipt blocking and embedded-derived-proof
bypass; root added five regressions and policy v2 fixes, then independent review
confirmed them. Current ownership covers this closure; prior completed plans
remain historical records. Original221-call study and private runtime are
immutable. This review performs no real API, deployment, push or supplier contact.


## First-pool tickets POOL-01 through POOL-05 — 2026-10-20

root owns the five approved local tickets, source-profile preparation, configuration/capability binding, controlled qualification-plus-budget entry, offline lifecycle and fee-gap inventory. Existing qualification, update, Core.run, Client and SQL interfaces are consumed unchanged. RG-01 source and formal gate registry are frozen. There are no delegated edits; code-review skill requires two independent read-only review axes before the local commit. Prior state/plan are preserved in docs/evidence/first-pool-prior-*. No model/source API, supplier contact, host operation, deployment or push.

Review refinement: a small POOL-00-binding prefactor owns the shared configuration-digest module; POOL-01, POOL-03 and POOL-04 explicitly consume it. POOL-01 and POOL-04 remain parallel after this shared identity primitive; no task waits for real supplier guarantees. The five user tickets are unchanged; the internal prefactor avoids multiple producers editing a shared interface.

## Independent SQL model qualification v2 — 2026-10-20

Root owns the new opt-in profile/policy adapter, SQL exam/grader, regression tests and evidence/docs. Existing catalog API remains compatible; v1 profiles retain exact-mapping/evaluation-date semantics. No parallel owner or shared edit. RG01 modules/tests/data and formal registry are frozen. No API, supplier contact, deployment or push.

## Sequential routing integrated with SQL qualification — 2026-10-20

Root owns the reviewed integration patch and overlapping current documentation, depending on the preserved completed SQL-MODEL-01 work. No parallel editors. The main checkout state and plan were archived locally and under docs/evidence; they were not replaced with isolated-worktree records. CloudyFan now permits three generation phases with validation between binary decisions; baseline modes remain two. SQL qualification modules, exam/grader, validator rules and historical receipts remain unchanged. Full integration checks must cover both work streams before recording completion. No API spend, hardware, supplier contact, deployment, push or local commit is authorized by this integration.

## Sequential integration local commit review — 2026-10-20

The user authorizes review and local Git commit of the integrated sequential route, preserving the related SQL qualification work and all failure/final verification records. Root owns selective staging and commit; two independent read-only skill reviewers checked Standards and Spec, with no source edits. No blocking finding; one optional old test-name wording observation remains. Current executable inputs still match the 1,304-test full integration fingerprint, so no full-suite rerun is attributed to this commit review. The Spec reviewer additionally ran the22 sequential regressions successfully. The independent whole-document plain-language rewrite of release-readiness.md is preserved unstaged; a reconstructed blob includes only SQL-v2/sequential/state evidence updates. No runtime/private receipt or root-workspace-only document is staged; no API, supplier contact, deployment or push.


## SQL qualification capture runner and manual source operations — 2026-10-20

Root owns a new coordinator consuming unchanged Client, BudgetStore, SQL exam/grader and public validator interfaces, plus its boundary tests, mock-only demo and documentation. No concurrent editors. Existing RG01 modules/tests/data, qualification thresholds, model profiles, formal registry and paid-trial records remain frozen. Capture performs public-only repair; sealed grading is a separate post-capture operation. No paid/source API, supplier contact, deployment, push, qualification promotion or local commit is authorized in this scope.

Integration correction: the existing capture normalizer used compact score JSON hashing instead of the actual Client encoding. Root also owns this narrow SQL-model capture binding fix and its former synthetic fixture correction. SQL validator/exam, capability thresholds and formal gates remain unchanged; the previous normalizer and failed first Client integration receipt are retained.

Completion: root retained sole edit ownership. The read-only technical reviewer found same-model role binding and completed billing-seal defects, both reproduced and fixed; final targeted review found no remaining actionable defect. Root additionally fixed actual Client encoding and repair checkpoint JSON ordering. Full1,322 checks and complete synthetic50x4 capture/grade passed. Existing RG01/exam/profile/formal/history inputs remain frozen; one RG02 fixture changed to actual Client encoding. Formal qualifications remain pending; no external actions or local commit.


## SQL qualification runner local commit review — 2026-10-20

The user now authorizes review and local Git commit; earlier no-commit statements describe their capture time. Direct review found no new blocking issue. The current executable/test/build fingerprint exactly matches the preserved1,322-test full integration result; this review reuses that result, not a fresh full run. A fresh offline quick check passed. All621 files from the original failed mock and the original failure/fix receipts remain intact;136 of137 protected inputs are unchanged, with only the declared RG02 Client-encoding fixture correction. [Precommit evidence](evidence/sql-runner-precommit-20261020.json) records the checks and preservation. RG01 and formal gates remain unchanged/pending. Raw answers, prompts and receipts remain in ignored runtime; no API, supplier contact, deployment or push.

### 2026-10-21 category-qcs-v1 離線接入

23 類各自四池、類別資格、完整成本／速度 QCS 與 CategoryCore 已完成；類別由呼叫端明確指定。SQL 資格與 RG-01 保留；非 SQL 類別不要求通過 SQL 考試。開源候選可憑該類獨立資格入池，不偽造排行榜分數。價差不足不呼叫或預留 Jev；類別／矩陣／政策／驗收契約凍結，既有回答不重送。

最終現行測試 1,561 passed（明確排除保留的 `* 2.py` 舊副本，沒有新增全域忽略）；相關 226 項、Ruff、Git diff whitespace、23 類 mock 演練、既有 core/score 及兩種 QCS 示範、最終 sdist/wheel 皆通過。標準整體檢查診斷為 2,082 passed／70 failed，70 項皆是同批既有舊副本衝突；該診斷早於最後測試增補，與最終現行測試分列，不宣稱整體 gate 通過。原始收據、初次收集失敗與開源候選回歸 red/green 都保留。

[實作與使用](category-four-pool-routing.md)；[本輪證據](evidence/category-routing-offline-20261021.json)。沒有網路、模型 API、部署、推送、提交或刪除。正式模型／來源／完整成本與速度資格仍 pending；合成結果不是實測降本。


### 2026-10-21 本機提交前審查

審查補正候選隔離與身分核對邊界：非物件 SQL 資格資料只排除該候選，不中斷全矩陣或 QCS 接入／重播；未上榜開源候選的來源觀測不得用空模型 ID 通過身分核對。原始失敗及修正後 230 項回歸保留。

暫存內容匯出成獨立目錄，`./scripts/check --integration` 全部 1,538 項通過，Ruff、兩個既有 mock 示範與離線 sdist/wheel 通過；未設測試忽略，也未將原工作區的未追蹤舊副本搬入提交。先前 1,561 項、70 項舊副本失敗及所有原始資料保持原紀錄。

[提交內容驗證](evidence/category-routing-tree-precommit-20261021.json)與[原工作區回歸](evidence/category-routing-workspace-precommit-20261021.json)分列；兩者來源指紋不同，工作區狀態索引與提交索引分別指向各自證據。必要 QCS／費用預留／來源正規化相依項目納入；SQL 計畫、廣泛來源工作及舊副本留在工作區，原始 runtime 不入 Git。正式資格 pending；本輪無模型 API、部署或推送。

## 2026-10-21 各類三候選備援整合

使用者授權實作根目錄延遲／備援設計。health_worker擁有健康正規化及獨立健康／冷卻儲存；wire_worker擁有明示省略推理參數契約與資格／費用核對；pool_worker擁有23類各池三候選準備／不足報告；root擁有新版opt-in執行、全路徑預留、整合與文件。精確檔案與依賴以work-plan為準，所有工作不得改其他人的dirty修改。修改前原source／state／plan在runtime/pool-backup-v1-20261021留存。RG01及原始50題結果、資格與付費請求不變；無模型API、供應商聯絡、部署、推送或提交授權。


## 三候選備援整合與審閱修正 — 2026-10-21

本輪範圍為`category-pool-backup-v1`，僅離線本地實作。健康、候選準備、請求參數及新版執行器分工已完成，由root整合預算與公開入口。23類92池具名表另列研究准入、待核、來源逾期及缺額；未批准任何新真實設定。

審閱曾重現兩項P1：改寫已存候選後可先派送、未完成流程續跑可在資格過期後新增請求。原Mock證據在`runtime/backup-runtime-readonly-review-f06a850a8c/`保留；修正後候選／經濟決策核對及逐新派送時效檢查均在POST前阻擋，獨立複驗在`runtime/backup-review-fix-confirm-ccaa9e0ff5/`。已有付費收據仍可過期後離線重播。

原始SQL/RG01、既有50題與所有付費答案／收據不變。151份整合前原始程式／測試／腳本及3份狀態記錄完整保留於`runtime/pool-backup-v1-20261021/`並核對雜湊；不修改原凍結研究的來源清單，也不以新版續跑。既有未追蹤舊副本保留、不刪除；標準檢查70個失敗與先前相同，另列現行測試結果。


### 2026-10-21 三候選備援本機提交前檢查

使用者已授權檢查與本機提交；先前「未提交」描述當時的擷取狀態。本次審閱另修正兩項P2：共用健康紀錄的事件身分加入收據根目錄，避免不同實際故障被誤判成重播；每次新分類派送前重新檢查候選健康，避免已知故障仍收分類費。新增2項回歸及獨立複驗通過，runtime／budget73項通過；原始重現保留。

實際提交內容匯出至獨立目錄，`./scripts/check --integration` **1754項全部通過**、Ruff、兩個既有Mock示範與離線sdist/wheel通過；未設定測試忽略，也未將舊副本納入提交。另跑原工作區現行檔案1771項，1770項先通過、1項工作依賴紀錄失敗；只補工作計畫依賴後，51項狀態／依賴回歸全部通過，沒有再改程式或掩蓋原始失敗。舊副本及先前70項失敗的原始紀錄保留。新版23類Mock備援46個生成請求，重播新增0；不是實際模型／速度／降本證據。

[提交內容驗證](evidence/category-backup-tree-precommit-20261021.json)與[原工作區驗證](evidence/category-backup-workspace-precommit-20261021.json)分開綁定各自來源指紋與狀態索引。SQL資格工作、研究版family-only規則、舊副本及1,944份原始實驗檔案保持原樣；研究資料與收據不入Git。正式資格保持pending；無模型API、來源GET、供應商聯絡、部署或推送。
