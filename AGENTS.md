# CloudyFan Jev core

- This repository is the standalone extraction of the new open-source-first binary Jev tree, not the older A/B or alpha/beta experiment.
- Preserve the policy: public pass skips classification; otherwise low/mid, then mid/high only if mid was selected. Generate once after selection. At most two generation attempts.
- A model's confidence or external score never establishes task correctness. Validation is an injected, versioned public contract. Unknown is not success.
- Hidden answers, hidden grader outcomes and another arm's answers must never enter selection or repair.
- Do not call real APIs as part of tests. Use MockTransport. Paid experiments require a separate authorized plan; repository maintenance does not authorize spending.
- Keep intent before POST, response before parsing, no automatic paid retries, identity checks, unknown-charge stops and original receipts.
- Runtime receipts may contain sensitive prompts and outputs. Never commit runtime directories, credentials, private responses or original experimental records.
- Run `./scripts/check`; use `--integration` for orchestration or delivery changes. Fresh checkout, hook and offline-cache instructions: [docs/checks.md](docs/checks.md).
- Keep original experiment sources and history outside this repo unchanged. Track provenance and extraction differences in docs.
- Creating this local repository does not authorize public publication. Do not add a public remote, upload artifacts or choose an open-source license without user direction.
