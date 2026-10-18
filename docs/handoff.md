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
