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


## SQL backup candidate and multi-setting qualification scope — 2026-10-21

Candidate worker owns only the new offline candidate/evidence projection, its CLI, fixture and tests. Root owns the opt-in multi-setting runner and compatibility hooks in sql_runner.py, then integration and metadata. Client, BudgetStore, SQL suite, validator and grader contracts are unchanged. Workers preserve unrelated dirty research, old copies and all original paid records. No model/source network, supplier contact, deployment, push or local commit is authorized in this implementation scope.

Test worker owns only tests/test_sql_multi_runner.py and scripts/demo_sql_multi_runner.py, consuming the new opt-in runner contract after the producer; root retains sql_runner.py/sql_multi_runner.py. No worker edits current metadata or frozen SQL semantics.


## SQL backup precommit review — 2026-10-21

Root owns alias/model-version pool uniqueness fixes and regression files after the completed candidate, runner and integration tasks. A separate agent reviews read-only. Exact staged content is verified separately from preserved dirty research/old copies. Original failing evidence and SQL/experimental records remain; local commit only, no network, deployment or push.

Completed implementation ownership is archived in docs/evidence/sql-backup-multi-implementation-plan-20261021.json. The current plan contains only the subsequent root precommit task and its explicit interface bindings; historical producer/consumer edges are not reassigned retroactively.


## SQL low two-candidate offline preparation — 2026-10-21

Root owns only the new offline preparation module/CLI, input/frozen plan, tests and current-scope docs. No changes to SQL validators, suite, grader, runners, catalog/category research, provider enforcement or historical plans/results. One owner; read-only review may follow. No model/source API, supplier contact, deployment, push or commit in this scope.


## Low-two preparation local precommit review — 2026-10-22

Root reviewed only the frozen low-candidate preparation and its indexed Git subset. No executable changes in this review. Full 1,831-test integration evidence is reused only after exact source and example/data comparison; fresh focused and metadata checks are separately recorded. Original failures, SQL qualification and experimental receipts are preserved. Local commit authorized; no model/source API, supplier contact, deployment or push.


## 2026-10-22 四槽／託管 Jev 本機提交審閱

本次只擷取四槽並行、完整流程費用預留、增量核帳及託管 Jev 的研究程式／測試，來源見 [scripts/research_parallel](../scripts/research_parallel/README.md)；原始外部研究程式、付費答案、帳本及中斷保留，不加入 Git。審閱補上快取身分交叉比對、殘缺／不可讀證據停止與單一回應緩衝區，共10項新增回歸。新版使用獨立v2 harness／plan識別，不修改舊凍結版本或SQL驗收。

[實作與限制](research-parallel-four-arm.md)、[擷取對照](evidence/research-parallel-four-arm-extraction-20261022.json)、[提交審閱驗證](evidence/research-parallel-four-arm-commit-review-20261022.json)分開保存。30分鐘仍是條件式容量推估；沒有新的真實正確率或節費結果。正式資格pending、付費入口停用。使用者本次僅授權本機提交，沒有付費請求、部署、推送或供應商聯絡；未把其他SQL、模型池、監控與檢查入口的dirty變更納入。

提交快照完整離線整合 **1,962項通過**（含131項研究回歸）、Ruff、兩個既有Mock示範及sdist／wheel建置通過；獨立200流程／550次Mock、4槽、重播0新增呼叫。21,823份原研究／題庫／前輪資料雜湊不變。此為提交子集，不代表整個dirty工作區或正式服務通過。


## Jev selector research repair — 2026-10-23

Root owns the new opt-in selector_research module, Core.run/MockTransport regressions, a separate SQL research adapter, and offline activation/calibration documents. Existing provisional S50 adapters, frozen plans/answers/receipts, RG-01 and unrelated dirty SQL/Monitor work remain unchanged. The evidence reviewer only inventories pre-existing independent calibration sources under artifacts/jev-selector-repair-20261023; no shared code edits. Missing calibration must not be called economically unprofitable, and formal holdout answers cannot become calibration. The existing agreed Core.run plus injected validator/MockTransport seam is retained. No paid HTTP, credentials, deployment, push or Git commit is authorized.

2026-10-23: JEV-SELECTOR-RESEARCH-REPAIR implementation completed. Core and SQL adapter can run both parsed selectors with synthetic evidence; no compatible observed conditional calibration was found. Offline completion is separate from the blocked real-comparison preparation. No external authority changed.

Final scope verification:31 new regressions pass; full dirty workspace2637pass/70retained numbered-copy failures. Two existing mock demos and offline build pass. Evidence recorded; real conditional calibration remains0 and phase blocked only for activation, not a claim that the new implementation is unfinished.


## Jev 選擇器本機提交審閱 — 2026-10-23

依使用者授權僅提交選擇器、校準契約及可重建的必要支援程式；原始研究／付費結果／失敗紀錄不移入Git、不覆寫。保留工作區SQL資格、Monitor及舊副本。本次額外補上fixture客戶端覆寫防護；SQL合成回歸不再載入外部研究或正式題。實作與離線測試完成不代表實際校準：適用真實條件校準仍0，RG-01凍結、正式資格pending，沒有新付費／部署／推送。提交子集驗證與原dirty工作區70項舊副本失敗分開記於[審閱證據](evidence/jev-selector-commit-review-20261023.json)。

## Monitor 本機接入：2026-10-22

使用者已確認十二項接入方案並授權本機實作、模擬模型驗收及一次有界來源GET。保留目前離線研究修正及所有不相關dirty內容，不改其帳本、原始收據、狀態scope或付費禁止。monitor-core-worker僅擁有retention.py與新增test_monitor_retention_concurrency.py，修正同程序任務鎖造成監控等待的問題；root整合受影響檢查，其他Monitor套件檔案的分工記於其docs/execution-checkpoint.md。不修改SQL契約、模型資格、BudgetedCore派送規則或原paid研究。

Monitor補充：monitor-core-worker追加擁有http_api.py及新增test_monitor_observation.py，向下相容提供結果讀取不刷新共享帳本的選項；Monitor查詢仍驗原結果與收據，帳本派送阻擋不變。先前retention非阻塞修正及其檔案所有權保留。

## 2026-10-22 Monitor 本機接入範圍完成紀錄

僅 MONITOR-RETENTION-NONBLOCKING 與 MONITOR-OBSERVATION-READ 兩項已完成，受影響核心回歸 71 passed；Monitor 最新完整套件檢查 76 passed，含真正本機 HTTP 與 CategoryCore 接線。桌機 8 項操作已通過，手機製作／測試依使用者新指示停止。詳見 [Monitor接入驗收](../../cloudyfan-monitor/docs/main-thread-integration-acceptance.md)。本次來源 metadata GET 為獨立 15 次嘗試，未呼叫付費模型；主線原未知費停止及其他 scope／blocked 狀態不變。核心一般全工作區與排除既有副本後的檢查另保留原始紀錄，不將 Monitor 通過冒充整個 core 全綠；未改 SQL 規則或其他研究結果。

2026-10-22 重開機後收尾：目前六個受影響核心模組共119 passed，兩個mock示範及離線建置通過（runtime/checks/2c9f1398b3ce46dbacc4c8d4d62224b5）；Monitor目前核心版本再次76 passed。舊兩輪大型檢查由重開機中斷且已出現失敗，保留為未完成；不把指定範圍通過當成整個dirty工作區全綠。目前routing-v2-s50-integration scope與其他任務不變，18767僅恢復合成桌機預覽。

## 2026-10-23 Ollama 客戶50題與 Monitor 模擬接線

使用者明示要求本機模擬客戶透過Ollama送50題，查看MRC路由、Jev與客戶帳務。monitor-ollama-worker僅擁有ollama_api.py及新test_monitor_ollama_submit.py，提供向下相容的明示submit callback；Monitor套件的Gateway／Store預期契約檢查由root整合，桌機trace與模擬fixture各自分工。保留現有jev-selector-local-review與blocked狀態、其他未提交變更及歷史研究；本次不讀真實憑證、不觸發真實模型或供應商GET、不恢復unknown525、不更改派送／費用資格。完整模擬成功不等於主線真實模型服務已部署。


2026-10-23 Ollama50實際HTTP首輪在第23題回401，已保留22題與瀏覽器中間進度。monitor-ollama-worker追加擁有identity.py及新test_monitor_identity_companion.py，只修SQLite正常COMMIT使sidecar消失的檔案檢查競態；先以公開身分驗證重現紅燈，保留不安全路徑與時鐘拒絕。App工程失敗須與真正401分開，不繞過驗證或自動重試；原401原因待實際證據，不把競態候選當成已證實原因。


Ollama50 R2已完成50題／170次合成呼叫，但桌機快照因每題重複驗證／授權而逾時，不能宣告完整驗收。monitor-ollama-worker追加擁有http_api.py與新test_monitor_task_observation.py：整合同一受授權／retention保護的host-only唯讀觀測，一次讀原Core結果，保留原收據驗證；並區分principal_active工程故障與真正拒絕。root擁有Gateway／integration接線及新公開HTTP回歸；UI worker只修app.js同時只讀一個快照。R1凍結檢查保留其原輸入與結果，修正後另建快照，不用延長timeout或跳過證據當修復。


## 2026-10-24 Monitor接入本機提交責任

使用者授權依Monitor／核心PR草稿選擇性提交。root單獨負責兩repo的候選index、共享文件逐段擷取、匯出驗證、Git提交與保留核對；commit_boundary_review只讀檢查並在.scratch產生候選，未改產品／index。既有桌機樣式六個class與style.css、兩張歷史截圖，以及其他研究／SQL／評分／安全依賴變更保留未提交；不重啟付費流程或擴張正式資格。


## Issues29-33 integration ownership

Root owns current metadata, source preservation and stage verification. capacity-source-worker owns Issue34 capacity.py and its public-boundary tests, in a separate branch/worktree based on this integration. Monitor source/UI/HTTP acceptance ownership is recorded in the external implementation context. integration-merger alone merges worker branches. Child tickets follow the native GitHub dependencies, including strict parent29→30→31→32→33 order; ownership is updated before each shared-interface change. Original canonical checkouts remain untouched.


Issue35 frontier: capacity-dispatch-worker owns capacity store/admission, transport pre-POST, BudgetedCore forwarding, TaskService controlled execution and optional pre-tool dispatch interfaces, as declared in work-plan.json. Monitor counterpart owns the vertical policy/ingress/safe-status/UI/actual HTTP+desktop acceptance. Root alone owns current metadata. Later queue36 and windows37 remain blocked until35 completes.
