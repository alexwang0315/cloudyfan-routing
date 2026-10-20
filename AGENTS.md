# CloudyFan Jev core

- This repository is the standalone extraction of the new open-source-first binary Jev tree, not the older A/B or alpha/beta experiment.
- Preserve the sequential policy: OSS first; public failure/unknown triggers low/mid selection AND generation/validation; only another failure/unknown triggers mid/high selection AND final generation/validation. Public pass skips remaining classification. At most three generation attempts for CloudyFan, two for baseline modes. See docs/sequential-routing.md.
- A model's confidence or external score never establishes task correctness. Validation is an injected, versioned public contract. Unknown is not success.
- Hidden answers, hidden grader outcomes and another arm's answers must never enter selection or repair.
- Do not call real APIs as part of tests. Use MockTransport. Paid experiments require a separate authorized plan; repository maintenance does not authorize spending.
- Keep intent before POST, response before parsing, no automatic paid retries, identity checks, unknown-charge stops and original receipts.
- Runtime receipts may contain sensitive prompts and outputs. Never commit runtime directories, credentials, private responses or original experimental records.
- Run `./scripts/check`; use `--integration` for orchestration or delivery changes. Fresh checkout, hook and offline-cache instructions: [docs/checks.md](docs/checks.md).
- Keep original experiment sources and history outside this repo unchanged. Track provenance and extraction differences in docs.
- Creating this local repository does not authorize public publication. Do not add a public remote, upload artifacts or choose an open-source license without user direction.
- Before continuing implementation, read [docs/handoff.md](docs/handoff.md) and run `./scripts/project-state show`. Update the current work plan before shared-interface edits; preserve historical evidence. Record consistency never grants external-action authority.
