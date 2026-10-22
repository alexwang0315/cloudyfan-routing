# Four-slot offline research harness

This directory preserves the reviewed implementation from the external
`artifacts/routing-parallel-four-arm-offline-20261022/` study directory. It is
research tooling, not an enabled paid runner or a production qualification.
No network transport or credential reader is supplied. All real dispatch,
complete-fee verification and account admission ports require explicit injection.

From the repository root:

```sh
.venv/bin/python -m pytest -q scripts/research_parallel
.venv/bin/python scripts/research_parallel/mock_four_arm_demo.py --output runtime/four-arm-demo-NEW
```

Use a new output directory on every run. The demo runs 200 **synthetic** complete
workflows through the current base Core, not the real S50 SQL tasks or frozen
QCS model pool. The committed public schedule contains identifiers and ordering,
not task prompts, data, answers, model responses or receipts. A second pass within
the demo must send zero additional mock requests. Synthetic costs are test values.

`historical_readonly.py` and `timing_rehearsal.py` require the preserved external
workspace data at their declared paths. They fail if those inputs are missing or
changed; a standalone checkout can still run every regression and the mock demo
without those private research files. The archival entry does not revalidate SQL
or perform hidden grading. The timing entry reads historical public status/timing
metadata only; its capacity scenarios do not establish quality or a live SLA.

The reviewed extraction uses a new v2 harness/plan identifier. Compared with the
original files it adjusts archival path discovery, supplies a public-only schedule
for the standalone demo, and rejects saved responses unless the call, workflow,
request and ledger identities match. Original sources, failed attempts and paid
results stay outside Git, unchanged. See the extraction manifest and scoped
verification under `docs/evidence/`.

CloudyFan retains at most three generations/two local classifiers; low-first and
open-source-first at most two generations/no classifiers. Hosted Jev uses medium,
metadata enabled, at most two client requests and no extra local classifier.
Hosted internal advisor/fallback counts are not assumed to equal client requests.
Unknown fees stop all new dispatch; already in-flight responses are saved. Every
attempt and complete-workflow reservation still counts against the budget.
