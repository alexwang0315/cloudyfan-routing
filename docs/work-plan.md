# Monitor v0.1 delivery scope

## Current Issue #20 integration follow-up (2026-10-22)

The initial side-task history below is preserved. The later main-thread scope is desktop only: mobile implementation and tests were stopped by the user. The invoked implement-spec skill now permits isolated local integration commits, not code push or deployment.

- #21 identity and #22 four-mode real HTTP/desktop evidence: merged and closed.
- #23 reviewed evidence: latest Monitor gate 89 passed; 20 real HTTP cases / 182 responses; three desktop groups with 20 checks passed. Shared fixture IPC/lifecycle and safe stderr handling address the low maintenance finding; scenario assertions remain in the entry scripts.
- The first review rerun's 87 passes / 1 disconnected-result failure is preserved. A deterministic public-HTTP overlap case observed task_busy; it does not recover the original missing error body. Only explicit task_busy receives a bounded read wait; other 409 responses fail immediately. The affected HTTP module and final full gate pass without product/core changes.
- Required human-approved canonical core delivery: **1921 passed** with all 8 integration steps successful and 438 frozen inputs unchanged. The 17 historical numbered snapshots / 633 tests remain preserved outside that approved delivery scope; the original 71 failed / 2483 passed run is not relabelled green.
- The unique source GET window is reused with verified original hashes; 15 attempts and 24 deferred endpoints remain partial. No new external requests, live pool publication, mobile work, paid calls or erasure.
- Root's 15:08 Chrome smoke on the original 18767 preview passed (12 synthetic tasks / 12 passed / 0 unknown, GET-only, unchanged summary); product fingerprints match. Final merge/sync must preserve those product hashes or repeat the smoke. See [current acceptance](issue20-final-acceptance.md) and [two-axis review resolution](issue20-code-review.md).

## Original side-task history

The user explicitly requested a first-version operational monitor in the side conversation on 2026-10-22. This is an independent additive package; it does not change the parent experiment or core checkout. No subagents, supplier contact, model spending, deployment, source push or commit.

1. Inspect the real core TaskService, budget, category-pool and update seams.
2. Implement durable monitor state, authenticated per-client ingress, four mode defaults, immutable task bindings and safe administrative revisions.
3. Implement complete-cost projection and per-client/route/latency statistics; never expose prompts, answers, secrets or hidden grading.
4. Implement all 23 category x four pool x three position views, GET-only candidate refresh, diff/publish and registered-runtime-only activation.
5. Build the local Chinese operator console; validate real Core paths with MockTransport, replay, unknown-fee stops, authentication and source failures.
6. Inspect desktop/mobile UI, retain verification evidence and provide host integration guidance.

First-version mode names map to the existing core: `cloudyfan`, `fixed_high`, `low_first`, `open_source_first`. Adding the high-first control does not authorize a new all-large-model experiment. Defaults change only future tasks; task identity, exact pool/contract and mode survive replay/restart.

Completion means the additive package and offline integration pass, the console interactions are inspected, and live integration boundaries are documented. It does not mean the main experiment or production service has been altered or qualified.

Completed 2026-10-22: steps 1–6 delivered. Final monitor gate: 35 passed, Ruff, JS syntax and offline wheel/sdist; desktop/mobile browser walkthrough completed. Evidence and host integration follow-up are in `docs/verification-20261022.md` and `docs/main-thread-handoff.md`. No main workspace source, qualification, authorization or experiment state changed by this side task.
