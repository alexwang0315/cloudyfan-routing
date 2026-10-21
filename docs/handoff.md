# Current handoff and evidence

Start with `./scripts/project-state show`. It reads the current checkout and
prints both recorded state and the Git HEAD observed now. Use
`./scripts/project-state check` for a nonzero exit on inconsistent records;
`./scripts/check` includes the same check in every mode.

## Sources of truth

| Record | Purpose |
| --- | --- |
| [agent-state.json](agent-state.json) | Current scope, phase, constraints, baseline and verified evidence references |
| [work-plan.json](work-plan.json) | Tasks, accountable roles, shared interfaces and dependencies |
| [release-gates.json](release-gates.json) | Production gate status, owner, reviewer and evidence |
| [release-readiness.md](release-readiness.md) | Meaning and acceptance requirements of each release gate |
| [verification.md](verification.md) | Append-only narrative of historical checks |

The baseline commit identifies a historical verified starting point. It is not a
claim about the current HEAD or uncommitted tree. Current offline completion
requires a separate evidence record matching the executable/test/build inputs,
plus a completed work plan and consistent metadata. Earlier test counts, closed
Issues and old `.scratch/implement-spec-1/` notes do not establish current status.

## During authorized work

1. Preserve earlier evidence. Set the current phase to `in_progress` (or `blocked`)
   and clear `current_verification` before changing executable inputs. Record the
   real scope and an appropriate verified baseline; do not relabel a dirty tree as
   a verified commit.
2. Update the [ownership plan](coordination.md) before dispatch or shared-interface
   edits. Owner roles describe responsibility, not authenticated human approval.
3. Run `./scripts/check --integration` after integration. Keep its private local
   receipt under `runtime/checks/`. Test fixtures and mock responses are never
   production evidence.
4. After success, record a sanitized offline evidence summary in `docs/evidence/`:
   schema version 1, kind `offline`, result `passed`, scope, observed `tests_passed`,
   `source_digest`, and the actual check receipt hash. The fingerprint comes from
   `scripts.project_state.source_digest(Path('.').resolve())`. Preserve the
   baseline and identify the tested tree by its digest when changes are uncommitted.
5. Mark verified work-plan tasks completed, reference the evidence path and SHA256
   in `current_verification`, set phase `offline_verified`, then run
   `./scripts/project-state check` again. Any executable change requires fresh
   verification. Documentation/metadata-only changes require the relevant record
   and link checks, not an invented new full-suite result.

The source digest covers `src/`, `tests/`, `scripts/`, `.githooks/`, `.github/`,
`pyproject.toml` and `uv.lock`, excluding Python caches. It excludes mutable docs
and evidence to avoid self-reference; those JSON records are checked separately.
It does not establish environment reproducibility, artifact signatures or the
truth of a manually written result. Preserve and inspect the actual receipts.

## Boundaries and next qualification

A valid set of records can have every release gate pending. It still passes the
consistency check, with `production_qualification_recorded: false`. Qualified or
out-of-scope gates require scoped, hashed, unexpired evidence and named reviewers;
the checker cannot authenticate those people or prove real-world observations.
The output never authorizes execution.

This offline scope keeps paid API, deployment, source push, hardware and model
download authorization false. A JSON edit cannot grant permission; future work
must use actual user authorization and an appropriate reviewed workflow.

The [G-02 SDK evidence](ollama-sdk.md) now pins official synchronous Python
`ollama==0.6.3` to `cloudyfan-ollama-text-v2`, with mocked upstream, actual scoped
identity, error handling, response-loss recovery and unsupported-feature rejection.
Review that exact scope before formal qualification. Async/JS/product clients and
real TCP/TLS/proxy behavior remain untested. SDK response models omit CloudyFan
metadata; the accompanying task GET API is part of the qualified client workflow.
Model/domain qualification and enforceable provider cost bounds remain separate
gates; no live API or deployed-client qualification follows from these tests.

The current RG-04 [component-bound scope](component-bounds.md) adds an independent
qualified-resource/rate mechanism, exact shared-budget arithmetic and pinned
classifier-version dispatch. Reviews bind the complete artifact and a shared,
durable withdrawal registry incarnation; no supplier HMAC endpoint is fabricated.
Real quantity/rate/inventory/failure evidence is not bundled. The original
supplier-ceiling verifier remains available and RG-04 stays pending. Do not
reinterpret historical price snapshots or ordinary key limits as qualifications.

The subsequent RG-04 zero-rate extension accepts schema v2 component evidence:
an independently reviewed binding all-path zero rate can replace that component's
numerical quantity cap. V1 remains strict; no price snapshot is converted into a
waiver. Complete fee inventory, paid-unit caps, current review/withdrawal and
exact dispatch restrictions remain required. Unknown outcomes are still unknown,
and positive charges against a zero-only bound create a durable shared violation.
No real supplier guarantee or production approval is supplied. Current scope and
verification are identified by project-state and the [v2 contract](component-bounds.md).

The [release index](release-readiness.md) now separates implemented scope,
offline-verified evidence and pending formal qualification for every gate.
Use its first-release evidence priorities and activation conditions; optional
sources/clients/streaming/multi-host features are not unfinished core work.
Release scope and any not-applicable decision still require named approval.
This is a documentation clarification, not a registry/schema change or a new
944-test run; the current source verification remains the v2 evidence above.

First-release evidence preparation now has a [scope draft](release-scope-draft.md),
[RG-01 validator review package](rg01-validator-qualification.md) and
[RG-04 gap matrix](rg04-evidence-gaps.md). SQL is a proposed domain backed by
legacy workspace material; at that preparation baseline no standalone SQL Environment
was implemented or release scope approved. The original workspace Python environment failed to
import pydantic_core; isolated replay of its two stdlib SQL test files passed
93 tests using the core environment without that unrelated conftest. Core-focused
mock tests passed 141 tests. These are distinct focused checks, not a new full
944-test integration pass or supplier/domain qualification. See the
[preparation receipt](evidence/release-evidence-preparation-20261019.json).

The subsequent authorized [SQL Environment](sql-environment.md) now provides
standalone tool-free Core.run integration, public-only flat diagnostics and
engineering error classification. Its copied helpers contain no task data,
reference queries or hidden oracles. Runtime/fixture preflight runs before POST;
validation interruption retains answers and budget holds, and explicit resume
does not resend paid requests. New versions separate these semantics from legacy
business-sql results. Current source/plan and the actual full offline receipt are
in [rg01-sql-offline.json](evidence/rg01-sql-offline.json).
Named domain/expected/coverage review, OS isolation and real model/provider evidence
remain pending. No release gate is promoted by this delivery.

## RG-01 review-ready engineering (2026-10-19)

The v2 [review package](rg01-review-ready.md) adds disposable stdlib workers, bounded pipes,
external kill/reap deadline, CPU/SQLite heap caps, and strict optional address-space admission.
Darwin AS/DATA setting failed locally: strict mode stops before POST, without fallback.
SQLite allocator caps are not total Python RAM or OS security sandbox qualification.
Eight public task templates have independent Python/hand expectations and mutation evidence.
Current actual checks are recorded separately from the historical 991-test v1 receipt.
Formal scope/expected/coverage review and target-environment qualification remain pending.
No paid requests, supplier contact, deployment or push are authorized by these records.

## S01-S08 semantic review (2026-10-19)

The [technical semantic review](rg01-semantic-review-20261019.md) rechecks all 40 original
expected rows against new explicit calculations, Python oracle and SQLite reference.
The original 14 mutants still fail. Thirteen selected post-hoc wrong queries pass the
frozen examples but fail valid counterexamples; no population false-accept rate follows.
The int64 domain also needs an explicit final-versus-intermediate accumulation decision.
Original catalog, source, tests and historical scores remain unchanged. No model calls,
formal signoff or supplier/deployment authority was created by this documentation work.

## Delegated RG-01 choices adopted (2026-10-19)

The user delegated the four open choices. The [decision record](rg01-first-scope-decisions-20261019.md)
adopts S01-S08 as the first qualification slice, preserves existing NULL/order/text
semantics, zero/negative IDs and orphan references, and specifies a new amount domain
(abs<=1e9, at most 1000 orders, all reference partial sums bounded by 1e12).
Linux isolated SQL worker is the formal qualification target; the current Darwin
portable worker retains only its existing offline scope. Codex is the AI technical
decision/evidence actor, not a forged human signatory. These choices are settled;
input-domain enforcement, new-version counterexamples and target evidence are pending.
No source edits, gate promotion, spending, supplier contact or deployment occurred.

## RG-01 remaining tickets (2026-10-19)

Current implementation and limits are in [the new bounded-SQL packet](rg01-implementation-20261019.md) and [ticket status](evidence/rg01-ticket-status.json). The source/catalog/strict Linux adapter are under offline verification. Real Linux attack evidence and actual named formal approval remain blocked; do not interpret implementation or mocked launch tests as target qualification. Historical semantic and decision evidence above remains unchanged. No model API, provider contact, provisioning, deployment or source push is authorized.

## Authorized Scout environment preparation (2026-10-19)

The user subsequently authorized dedicated Scout system packages and permissions,
and completed both private-terminal sudo steps. [Preparation is now complete](rg01-scout-environment-20261019.md):
pinned root-owned read-only v2 ARM64 rootfs, bwrap and a fresh systemd delegated
user scope passed real strict-worker SQL preflight/validation and leaf cleanup.
Original cgroup-migration and loader-mode failures remain in the evidence.
The earlier missing-Linux/sudo blocker is resolved; no further preparation sudo
action is pending. Ephemeral cgroup paths must be recreated and recorded per run;
there is no persistent service or automatic remount after reboot.
RG01-06 still needs actual resource/attack/interruption evidence, and RG01-08
still needs named formal review. Registry status stays pending. No executable
source change or new full pytest run occurred; see the separate scoped
[preparation receipt](evidence/rg01-scout-environment-preparation-20261019.json).

## Scout controlled target evidence (2026-10-19)

Subsequent user-directed RG01-06 work completed [scoped target evidence](rg01-scout-target-20261019.md):
normal S01-S08 flow 8/8 and seven bounded isolation/resource/interruption exercises
7/7. The first six exercise trusted probes in the unchanged sandbox envelope;
the last SIGSTOPs the actual immutable SQL worker after a mock answer is saved,
then resumes the same answer without another POST. Active reservation is released
on completion, but a 0.02 synthetic provisional billing hold remains. No real
reconciliation or supplier/model qualification follows. Initial provider-fixture
mismatch and all not-run rows are preserved in the earlier independent run.

Final integration: 1,102 tests, Ruff, both mock high-route demos and packaging
passed; [offline receipt](evidence/rg01-target-offline-20261019.json). The runtime
package source is unchanged; qualification scripts/tests changed. [Target receipt](evidence/rg01-linux-target-runtime.json)
pins the effective artifacts, ephemeral cgroup identity, raw hashes and limits.
Seven of eight RG01 tickets are now complete; RG01-08 actual named formal review
is the remaining blocker. Project-state remains blocked because of that explicit
task, not because implementation or RG01-06 is missing. Formal registry unchanged.
No model API, deployment, supplier contact or push occurred.

Precommit review confirmed the target source fingerprint still matches the final
1,102-test integration evidence. The [RG01-08 checklist](rg01-formal-review-checklist-20261019.md)
collects the already-decided scope, expected/coverage review, exact sandbox build,
interruption/accounting limits and named approval/expiry/withdrawal requirements.
It is a review input, not a formal signoff. Commit-time checks are preserved in
[the precommit record](evidence/rg01-target-precommit-20261019.json).

## RG01-08 technical evidence review (2026-10-19)

The user subsequently authorized reviewing existing evidence, with no formal
signoff or qualification-state change. [Technical review](rg01-technical-review-20261019.md)
found no new blocker within the finite public S01-S08/pinned sandbox claim.
It rechecked 26 indexed references and 96 copied files; trusted local in-memory
SQLite replay confirmed 53 case/sample pairs, 40 original manual/oracle pairs,
14 old mutants and 13 post-hoc wrong-query probes. These are not new holdout
tasks, model results, a new full pytest run or new Scout execution.

The review explicitly limits cancellation evidence to a ready trusted probe plus
injected KeyboardInterrupt; actual immutable SQL evidence is one saved-answer
SIGSTOP/deadline/resume path. Product-client cancellation, host shutdown,
supervisor SIGKILL, unseen-data correctness and hostile same-UID host isolation
are not qualified by these observations. [Review record](evidence/rg01-technical-review-20261019.json)
preserves inputs/hashes and the human decisions still required. Adopted business
semantics are not reopened. The technical-review subtask is complete; the actual
named approval task stays blocked. Formal registry, owner/reviewer and evidence
remain untouched. No source edits, API, SSH, deployment, push or local commit occurred during that technical-review stage.

The user subsequently authorized local review and commit of this documentation.
[Commit checks](evidence/rg01-review-precommit-20261019.json) preserve fresh offline
record/link/hash checks. Formal registry and named-review requirements remain
unchanged; no API, deployment or push is authorized.

## Direct RG01 testing — user direction 2026-10-19

Do not add further S01-S08 semantic or formal review as a prerequisite to
development/testing. The user explicitly directs routing-core execution tests.
Formal RG01-08 remains recorded separately; it is excluded from the active
execution-test plan without creating a signature or changing release gates.
Run the focused SQL tests and full integration entry, then report execution
results; mocked upstream is not live model-quality or provider-cost evidence.

Execution complete: focused RG01 139/139, final full integration 1102/1102,
Ruff, core/score demos and sdist/wheel passed. Four explicit route/replay
pairs passed with zero additional requests on every replay. An initial full
run failed one newly edited plan-ownership check (1101 other tests passed);
the current plan was narrowed to execution work, 31 metadata tests passed and
full integration was rerun successfully. [Current execution receipt](evidence/rg01-routing-execution-tests-20261019.json)
preserves both runs. No runtime source change, real model API, Scout execution,
deployment or push. This scope is offline-tested; no formal review blocks it.

## Live integration completed — 2026-10-19

[Actual API results](rg01-live-integration-20261019.md): six open-source first passes,
S07 low-tier Jev repair, S08 saved-answer recovery after a narrowly versioned
compiler-classification fix. Preserve v1 seven completed/one interruption and
the separate v2 repair; do not call this a clean eight-case cohort. Three model
endpoint probes passed; second selector branch not naturally triggered. Fifteen
POSTs, USD0.060291624, all generation receipts and key delta match, zero new
unknown costs. Full source-fingerprint verification is now 1104 tests. No
deployment, source push, signature or further S01-S08 audit.

The user subsequently authorized local review and commit of the SQL fix and
linked execution/live records. [Precommit checks](evidence/rg01-live-precommit-20261019.json)
preserve the original interruption and separate continuation, full receipt
chain and a private runtime inventory. Raw runtime stays outside Git. No
new API calls, deployment, push or formal qualification change.

## Authorized fresh50 paired trial in progress — 2026-10-19

Frozen plan c0427ac5d4cba63634efbb84cd7023da6def518e4bfd7494243574f19c8b6354
is in ignored runtime/fresh50-four-arm-20261019/plan.json. Fifty new SQL specs
across ten families, 15/20/15 strata, four independent Core modes, fixed
model versions/efforts/cards, one public-only repair and balanced position
rotation. Hidden grading only after all 200 terminal workflow records.
Resume the same benchmark.py run; no edits to sealed source, new attempts,
answer sharing, S01-S08 review, deployment or push. Progress/status, intents,
responses and receipt GETs are durable in that directory.

Fresh50 execution addenda: the first BaseTen OSS call returned upstream429 with
no answer/usage and GET404. Official OpenRouter pricing page was captured; pure
text requests without auxiliary services qualify for policy-backed zero model
fees, recorded separately from generation receipts. Successful calls are never
resent. Addenda v2/v3 retain the frozen plan and original raw records; v2 hit a
nested receipt lock before any additional POST, and v3 fixes only that execution
lock. Ten offline policy/unknown/duplicate/new-mock-dispatch checks passed.
Current continuation command is the ignored runtime `resume_policy_v3.py`; do
not run the old runner to bypass the addendum. Preserve all versions, failures,
policy evidence and unknown stops; no hidden grading before 200 terminal flows.

Fresh50 completed: all 200 workflow terminal records precede hidden grading.
[Final comparison](fresh50-four-arm-20261019.md) and
[sanitized evidence](evidence/fresh50-four-arm-20261019.json) retain the full
50-task cohort and execution versions. High and low-first both 50/50 public+
hidden pass; low-first API USD0.4089475 vs high USD2.3646875 (82.71% lower),
median workflow 2.415s vs 5.781s. CloudyFan 10/50 plus 40 upstream429;
OSS-first 20/50 plus 30 upstream429, including two successful high repairs.
CloudyFan classified zero times; do not claim Jev incremental benefit or
same-quality CloudyFan savings. Total USD2.8850731, 132 generation receipts
plus 70 separately policy-evidenced zero-fee rejections, no unknown costs or
unattributed calls. Final key delta USD2.885073100 matches exactly; immediate
posting delay USD0.114122 is preserved and resolved by the later snapshot.
Prior smoke remains separate. Raw runtime is ignored, product source/qualification
unchanged; no commit, deployment, push or further S01-S08 review. Next engineering
priority is bounded escalation after a confirmed no-answer provider rejection;
that behavior was not silently added to this frozen trial. No additional paid
request, new cohort or implementation is part of this completed comparison.

## Bounded generation429 escalation — 2026-10-19

User authorizes the local fix and offline regression only. [Recovery behavior](provider-429-recovery.md)
and [offline evidence](evidence/provider-429-offline-20261019.json) distinguish
cost-resolved explicit no-answer429 from unknown/partial/other failures. CloudyFan
uses its existing two-stage binary selection and one final generation; low/OSS-first
use high once. Rejection consumes the attempt; a second rejection is unverifiable.
Fixed-high never retries the rejected high, classifier429 stops, unknown fees/IDs
still block new POST. No default zero-fee rule or provider qualification was added.
Engine descriptor 0.1.1 prevents applying new semantics to old frozen runs.

Final full integration: 1,138 tests, Ruff, both mock demos, sdist/wheel passed.
An initial pass attempt had 1,137 passes and one historical-work-plan dependency
failure; the preserved prior plan was separated from the current task, 65 focused
state/rejection tests passed and full integration was rerun. Original 2,369 fresh50
files match before/after SHA inventories exactly; the 50-task result and all fees,
429s, stops, continuations and original answers remain unchanged. No paid test,
real API receipt refresh, deployment, push, local commit or formal gate change.
Real provider recovery rate and any new savings remain untested.

The user subsequently authorizes local review and commit of the 429 fix and linked
sanitized fresh50 summary. [Precommit review](evidence/provider-429-precommit-20261019.json)
records fresh 34-test regression, matching 1,138-test source fingerprint, receipt
hash verification, unchanged 2,369-file trial inventory and explicit staged scope.
Private runtime remains excluded. No new real API, receipt refresh, deployment,
push or formal qualification change is part of this local commit.

## New fresh50 recovery trial — 2026-10-20

User authorizes checking429 fees then running fifty entirely new tasks across
CloudyFan/high/low-first/OSS-first, with frozen rules, bounded attempts and all
classifier/repair/failure fees; no deployment or push. An opt-in archived,
exact-scope text429 fee policy now reaches Client/Core/ledger; policy evidence
is separate from generation receipts, actual fees win, unknowns still stop.
[Policy integration](rejection-fee-policy.md) and [offline evidence](evidence/rejection-fees-offline-20261020.json)
preserve the boundary. Three previous actual429 records were copied to a new
read-only billing preflight, GET404 retained, policy attribution confirmed with
zero new paid POSTs. Original fresh50 directory is unchanged. New dataset,
plan, source seals, runner and fee evidence live under ignored
runtime/fresh50-recovery-20261020/. Do not mutate sealed files after dispatch.

Fresh50 plan frozen as 4938ed408f5c715ac1000f00f32365cddf5bde5ce4cc726bbdc143b9f2a3c075.
New projects/events/members cohort passes350 oracle/SQLite and core checks,
50 mutants; public2/hidden5, ten families, strata15/20/15. All24 arm orders
occur twice plus a permutation/reverse, with position12/13. Source digest and
all task/runner/policy/report files are sealed. Native Core uses bounded429
recovery and the same fee evidence policy in all groups. Run benchmark.py run
in runtime/fresh50-recovery-20261020 only; successful saved responses are not
resent. Hidden grading starts only after200 terminal workflow states. Unknown
fees or identity mismatches stop new POST; read-only reconciliation allowed.

The new cohort is complete: 200 terminal workflows and 200 post-dispatch hidden
grades, 221 independent POSTs, USD5.805611568, zero unknown/unmatched costs. Key
usage delta matches exactly. Sources:220 generation receipts plus one explicit
response usage.cost (generation GET408 retained). No new429 occurred; do not
claim a real-provider429 recovery rate. At most two generation attempts and
completed replay0POST are verified. Original2,369-file cohort is unchanged.

[Results](fresh50-recovery-20261020.md): CloudyFan48/50, high50/50,
low-first50/50, OSS-first49/50. CloudyFan's96.80% lower spend is not equal-quality
savings. Low-first saves82.79% with50/50 in both arms in this sample. Three
public false accepts (CF F32/F48, OSS F32) bypassed selection/repair; six CF
repairs all passed. Never tune or rerun the frozen cohort from hidden outcomes.
Private raw records remain ignored, with final inventory. New API spending was
authorized only by the separate trial; maintenance checks remain offline.
No deployment, push, local commit or formal gate change. Prior smoke and prior
50-task cost remain separate. Next technical priority is general public-validator
coverage against independent unseen cases, not claiming that hidden rejection
itself made this public validator reliable.

## Public SQL suite and false-accept regression — 2026-10-20

User authorizes offline analysis/general validation strengthening only, no new
API/receipt refresh, deployment or push. [Public suite](sql-public-suite.md)
materializes independent application-owned expectations from PUBLIC cases before
dispatch. Declared coverage requires actual empty/NULL/duplicate/orphan/unmatched
parent witnesses; no query-text heuristic, hidden answer or live oracle enters
the Core. Oracle/corpus/builder provenance joins the frozen public contract.
This is opt-in; legacy SqlContract and routing remain unchanged.

Controlled public-only diagnosis: F32 CF loses uniqueness and existing-parent
membership; OSS F32 loses existing-parent membership. CF F48 drops parent rows
without eligible children, including all-NULL work units. New shared seven-case
public corpus plus independent Python semantics rejects all three known false-accepted
answers while retaining five controls. This is post-hoc regression, not a new
blind experiment, improved50-task score or measured CDR. Original48/50,50/50,
50/50,49/50 and USD5.805611568 remain unchanged.

Final full integration passed1,182 tests, Ruff, both mock demos and package build.
Fifty-nine focused state/suite tests passed. A code-only independent review found
int/bool/float equality could miss input mutation; type-sensitive canonical JSON
comparison and two regressions fix it. Initial synthetic SQL alias mismatch,
metadata null-key and historical ownership-plan failures are preserved with the
successful final receipt in [offline evidence](evidence/sql-public-suite-offline-20261020.json).
Current plan contains only this task; earlier completed plans remain history.

All2,552 current trial files, three result documents and2,369 older trial files
are unchanged. Existing executable inputs excluding the new builder/test match
the original frozen source digest exactly. No new API, deployment, push, local
commit or formal gate promotion. Trusted-oracle semantics, unseen generalization,
new prompt/verification latency and actual live cost effects remain unverified.

## Public-suite local commit review — 2026-10-20

User authorizes local review/commit only. The independent staged-source export
passes1,166 tests, Ruff, both mock demos and package build. The earlier1,182
working-tree receipt includes16 separately pending fee-policy tests and is
retained as historical evidence, not attributed to this narrower commit.
[Precommit evidence](evidence/sql-public-suite-precommit-20261020.json) binds
the isolated source, original receipt and preserved trial inventories.
The pending fee-policy implementation and fresh50 report stay in the working
tree outside this commit. No paid API, deployment, push or formal qualification.

## Remaining fee-policy/report local commit — 2026-10-20

User subsequently authorizes review/local commit of the remaining fee-policy
integration and sanitized50-task report. This supersedes the pending-only scope
of12b4f34; its1,166 staged-source and1,182 larger-tree receipts remain history.
Review found two defects: expired policy blocked actual receipt GET; derived
fee fields embedded in original response bypassed separate proof validation.
Policy v2 uses original/actual-only reconciliation, rejecting embedded derived
fields while preserving actual-fee priority and all unknown/duplicate stops.
Five reproductions failed before fixes,55 focused cases and1,187 full integration
tests now pass, with Ruff, both mocked demos and packaging. The application
still owns paid admission; policy attribution is not a fee cap or permission.

[Commit review](evidence/fee-report-precommit-20261020.json) verifies saved221-call
costs USD5.805611568, final48/50/50/49 and both original file inventories. No
grading, paid POST or real receipt GET was rerun. Original v1 code is archived
from the prior successful build and reconstructs the frozen trial source hash.
v2 is a separate descriptor version; frozen raw records/reports remain unchanged.
Historical report statements about no local commit describe capture time. Only
sanitized code/tests/records are version controlled; private runtime remains local.
No deployment, push or formal qualification. Unknown historical charges outside
this cohort are not cleared by this commit or its matched account delta.


## First-release pool integration — 2026-10-20

User authorizes implementing the five local POOL tickets under mattpocock/implement, including offline validation, review and local commit. Tickets remain local, not GitHub Issues. [Pool entry](first-pool.md) reuses the score adapters/qualification/updates/ScoreCore and composes BudgetedCore. Historical CodexRadar import keeps actual observation times, 14 native scores and unresolved version/SQL-capability/access evidence; the real archive remains blocked. Synthetic lifecycle uses fixed MockTransport and actual SQLite, explicit artificial approval/caps, and is never a production pool. Fee gaps bind every candidate plus classifier, all eight components and seven paths; no provider guarantee or numeric upper bound is invented. RG-01 and original paid trials remain unchanged. Final checks and review will be recorded separately, not inferred from prior 1,187 tests. No paid/source API, supplier contact, model download, hardware, deployment or push.

Final first-pool integration passed 1,206 tests, Ruff, both mock demos and sdist/wheel; [evidence](evidence/first-pool-offline-20261020.json) and [ticket status](evidence/first-pool-ticket-status-20261020.json) separate offline completion from actual qualification. Standards P3 shared-interface ownership and Spec P2 missing new-evidence review binding were fixed and independently re-reviewed; both axes have zero remaining findings. Five evidence-change regressions were red before the review-binding fix. RG01 source, formal registry and paid50 reports match the starting commit; no new API or qualification claim.


## RG-02/RG-03 free public evidence — 2026-10-20

User authorizes existing-material and unauthenticated free public-source checks only;
no paid API, supplier contact, deployment or push. [New evidence](first-pool-public-evidence-20261020.md)
separates API catalog identities/efforts/provider tags/declared context from benchmark
snapshot identity and actual SQL capability. Three original fixed candidates have
new native scores 34.181422 / 99.073981 / 115.035351, each with 64/64 coverage; current last3
uses 80 / 110 / 74 rewards (middle 112 total valid events is not 110 used rewards). Source
explicitly reports a changed audit population; no controlled trend or SQL quality
claim follows. Old DOM 14 rows, requirements/thresholds/TTLs, RG01 source, formal
registry and historical trials remain unchanged.

New public profile is separate, approved=false/review empty, no current activation.
All four configuration evidence digests bind; exact benchmark/API snapshot links,
OSS deployed artifacts, six SQL operations, complete execution/evaluation dates,
source/domain/use policy and update owner/review remain pending. latest_graded_at
is not evaluated_at; do not populate missing evaluation dates with retrieval time.
Robots crawl permission and OSS Apache-2.0 do not license score-data redistribution.
410 old score endpoint then declared new endpoint 200, and LiveBench 404s are retained.

141 focused offline score/qualification/update tests and quick check passed; 32 raw
public GET hashes, three public score recomputations, blocked report replay,
future source expiry and unchanged executable source were checked. CLI expected
exit 1/blocked is a successful admission-guard check. Full 1,206 test evidence is
historical and still matches source; it was not rerun. [Matrix](evidence/first-pool-public-20261020.json)
retains actual receipts, setup failures and limits. No model POST, weights download,
new benchmark, local commit, deployment, push or gate promotion. Continue from
specific remaining evidence, not S01-S08 review or another validator experiment.


## Public-evidence local commit review — 2026-10-20

User now authorizes review and local Git commit of the RG02/RG03 public supplement,
not new source reads or model requests. The earlier no-local-commit statements
above describe their capture time. [Precommit evidence](evidence/first-pool-public-precommit-20261020.json)
records fresh 141-test offline regression, independent recomputation of all three
scores/coverage/used rewards, exact prior receipt hashes, blocked admission and
future expiry, unchanged executable/RG01/formal-registry inputs, and explicit
staged scope. Public facts remain separate from missing benchmark snapshot/SQL/
evaluation-date/use-policy qualification. Prior evidence and private raw responses
are preserved. No API, supplier contact, deployment, push or formal gate promotion.


## Explicit family-prior + independent SQL qualification v2 — 2026-10-20

User authorizes the policy change, offline version table/exam/grading and future
API plan only. V1 profiles and their exact-mapping/date semantics remain intact.
V2 accepts rank scores as explicit family/effort priors (never API snapshot equality),
requires separately bound exact-API identity and a complete independent50-task SQL
report, retains original unknown benchmark dates and retrieval freshness, and adds
SQL evidence30-day expiry to snapshot lifetime. Input-bound review, synthetic
rejection, missing-pool stops, budget admission and formal pending registry remain.
Only aggregate qualified operation/card metadata reaches Jev, not exam answers.

[Contract and plan](sql-model-qualification-v2.md), [pending profile](../examples/first-pool-profile-sql-v2.json)
and [versions](../examples/sql-model-versions-v1.json) are prepared. Offline scripts
write private runtime material and grade saved Client-bound records, never POST/GET.
Python semantics and frozen SQLite agree on50 controls and reject50 mutants; this
is not real model capability evidence. Public2/sealed4 cases per task, six finite
families and15/20/15 declared strata. First/final outcomes and all known fees are
separate; engineering/unknown evidence has null outcome, not a wrong answer.

Full offline integration passed1,281 tests (75new), Ruff, both existing mocked
high-route demos and offline sdist/wheel. [Receipt/source/evidence](evidence/sql-model-offline-20261020.json)
records exact scope and initial fixture/import failures. RG01/old profiles/formal
gates/old trial reports:77selected files unchanged. No S01-S08 review or new API.
No actual candidate is approved, no current is activated, no local commit/push.

[Unexecuted API plan](evidence/sql-model-api-plan-20261020.json):4identity probes +
200first answers + at most200same-model repairs, no Jev classifier/network retries.
Old-price token scenarioUSD46.2263424; proposed authorizationUSD60, not a supplier
cap or current permission. Explicit spending authority, fresh exact-ID/price and
fee/unknown/reserve checks, plus a controlled capture runner are prerequisites.
The actual model/API evidence, source-use/update owner/review and RG04 guarantees
remain pending; do not relabel estimated bounds or staged artifacts as qualification.

## Sequential routing + preserved SQL qualification integration — 2026-10-20

The reviewed isolated patch is applied to this main checkout without replacing its state, plan or coordination. The completed SQL-MODEL-01 work, exam/grader, model versions and qualification thresholds remain unchanged. New CloudyFan tasks use hierarchical-sequential-v1 / engine 0.2.0: OSS validation, low/mid generation and validation, then conditional mid/high generation and validation (at most three generations/two classifiers). Baseline modes remain two. The second selector sees the latest failed answer/configuration/diagnostic. Legacy plans are reportable but cannot execute, and legacy manifests refuse resume before POST.

Current combined offline verification: 1,304 tests, Ruff, both mocked high-route demos and sdist/wheel passed. See [combined evidence](evidence/sequential-integration-offline-20261020.json). The first full run had 13 SQL worker/resource failures; all 13 passed unchanged in focused recheck, then the final full run passed independently. The original failed receipt and logs are retained. 132 protected inputs/evidence files are unchanged. No SQL timeout or semantic rule was relaxed.

No real API, host operation, deployment, source push or local commit occurred. All formal gates remain pending. The fixed-high independent control remains paused. The earlier two-generation routing cost estimate is historical; any new sequential experiment needs a new frozen plan and full three-phase reservation. Independent SQL qualification retains its separate two-attempt contract and unexecuted API proposal; it was not run by this integration.

## Sequential integration local commit closure — 2026-10-20

The user now authorizes a local commit; earlier no-commit statements record their capture time. Standards and Spec reviews found no blocking issue, with one nonblocking legacy test-name observation. Full integration source fingerprint and original failed/final receipts remain intact;22 sequential regressions were rerun offline. [Precommit evidence](evidence/sequential-integration-precommit-20261020.json) records selective staged scope, preservation and fresh checks. SQL qualification implementation stays in the same coherent source tree; real API/SQL model qualification is still pending. The independent release-index prose rewrite remains in the working file, outside this commit, with its stale progress paragraph corrected. No API, supplier contact, deployment or push.


## SQL qualification runner + manual source operations — 2026-10-20

The user authorizes local implementation and offline validation only. QualificationRunner composes unchanged Client/BudgetStore and the frozen SQL exam/validator: four identity probes, fixed rotating50x4 schedule, at most two same-model attempts, public-only repair and sealed grading only after complete capture. No classifier, fallback, automatic retry or reconciliation GET. Intent/response and public checkpoints have an immutable original-answer seal; later explicit reconciliation updates original ledger events and appends billing revisions without resending answers. The narrow existing capture normalizer now uses actual Client JSON encoding rather than compact score encoding.

[Current evidence](evidence/sql-runner-offline-20261020.json):1,322 tests (18new), Ruff, two existing mock demos and sdist/wheel passed; wheel contains the new runner. [Complete mock rehearsal](evidence/sql-runner-mock-20261020.json) passed200/200 synthetic final answers,205 mockPOSTs, one explicit mockGET, zeroPOST completed replay and post-reconciliation grading. Synthetic cost0.208 includes0.004 identity fees; real API0. Original red tests and the failed initial full mock are preserved. First integration collection failed because an iCloud-evicted native pydantic_core file was absent; restored from existing offline uv cache matching installed RECORD, no files deleted/downloaded; subsequent full integration passed.

[Manual source process](score-source-manual-operations.md) documents source use, native tiers, deadlines, review/update responsibility and failures; actual rights/named people remain pending. RG01 validator/exam/profiles/formal registry and historical evidence unchanged. Real model identity/SQL results, supplier bounds, source-use qualification and real current activation remain pending. No supplier/source/model network, deployment, push or local commit.


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
