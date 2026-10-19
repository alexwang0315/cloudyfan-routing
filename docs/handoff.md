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
