# Local loading, switching and complete-cost measurement

`BenchmarkHarness(core, private_report_root, clock=monotonic).run(...)` calls
`Core.run` directly with the caller's task, environment factory, public contract
version and workflow mode. `ModelSetup` pins model version, quantization, engine,
hardware and resource settings. Local model/quantization/engine must match the
registered backend; cloud models use explicit provider-unspecified hardware or
quantization labels when those details are unavailable. Such labels are unknown
configuration details, not qualified model-equivalence evidence.

The same environment factory and `contract_version` can compare local and cloud
answers. Core still enforces its public diagnostics, two-attempt limit, privacy
policy and receipt rules. This harness provides no model correctness oracle,
engine launcher, weight downloader or hardware-control function. It does not
supply payment authorization. Any future real call requires the separately
approved budget/execution wrapper and experiment plan.

## Probe and time contract

The optional `prepare(emit)` hook receives an event observer and may attach it to
an operator-supplied engine probe. Tests attach it to `httpx.MockTransport` and a
fake clock. A probe can emit `load_start/end`, `queue_start/end`,
`prompt_start/end`, `generation_start/end`, `switch_start/end`, `first_token`,
`oom`, and `interrupted`. Every event records the injected monotonic clock value.
Unknown event names, nonfinite clocks and backward event clocks are rejected.
OOM and interruption events immediately stop execution and retain the event.

The report preserves the event sequence and lists load, queue, prompt processing,
generation and switch durations separately. TTFT is queue entry to first token;
it includes queue and prompt time. The nonstream local protocol cannot observe
true TTFT by itself, so missing first-token events remain unknown. A missing,
repeated or reversed phase endpoint produces an unknown phase duration; raw
repeated events remain available for a future explicitly designed load analysis.
Wall time includes preparation and Core execution. Measurements of load or
switching never establish that simultaneous inference is possible.

An OOM, interrupted execution or other engineering error is distinct from a
publicly failed answer. It has `public_passed: null`; original Core receipts are
preserved. The report reads all matching classifier/generation receipts from both
local and cloud roots, including interrupted attempts. No unresolved request is
replayed just to complete the benchmark. Report reuse is immutable and rejects
changed task, model/core settings, public contract, measurements or prices.
An updated analysis belongs in a new report identity, not an overwrite.

## Cost and resource data

Optional `measurements` fields are `peak_memory_bytes`, `average_power_w`,
`energy_kwh`, and `human_seconds`. Optional `rates` are
`electricity_usd_per_kwh`, `host_usd_per_hour` (the operator's depreciation or
rental allocation), and `human_usd_per_hour`. Values must be finite and
nonnegative; omissions/null stay unknown. Power alone is not extrapolated into
energy. Model/API fees come from durable receipts, not token price estimates.

The report lists model API, external tool, electricity, host and human costs
separately and computes a complete total only when all components are known.
Declaring tools makes external tool cost unknown in this harness; therefore it
cannot emit a misleading complete total while omitting tool fees. Its separate
`hypothetical_zero_model_fee_complete_usd` removes model API fees as a labeled
scenario; that field is not actual savings. Local no-fee attestation establishes
only model API zero, not host/electricity/labor zero. Synthetic input prices and
measurements produce synthetic cost arithmetic, not real operational costs.
The harness currently measures Core model calls and these explicit resource
components; external tool fees need the complete-cost workflow/reporting layer.
Do not use this harness alone to claim a complete bill for a tool-billed task.

Reports and manifests contain task text and Core answers in private files. They
are not suitable for uploading as public benchmark evidence without redaction.

## Real-hardware release matrix

All rows below are **pending real execution**. Fixtures validate the mechanism
only. Even `evidence_kind: measured` marks source provenance rather than granting
hardware/quality qualification. `hardware_status: not_validated` and
`concurrency_validated: false` remain explicit in generated reports.

| Scenario | Required pinned inputs / evidence | Result status | Release condition |
|---|---|---|---|
| Cold model load | exact weights digest, quantization, engine build, device, OS, RAM, baseline memory | pending | observed load success, resident/peak memory, errors recorded |
| Warm inference | same configuration, public tasks/contract, token/context limits | pending | independent public verification and prompt/generation measurements |
| First token | engine-level streaming/event probe with documented semantics | pending | observed TTFT; never infer from nonstream response time |
| Model A → B switch | both pinned models, unload policy, switch order, warm/cold status | pending | observed unload/load/switch durations and memory peak |
| Simultaneous inference | explicit concurrency, queue bound, per-request isolation | pending | overlap actually observed, no OOM, independent outcomes |
| OOM / interrupted execution | authorized fault setup, stop/restore plan, receipt retention | pending | bounded failure, no silent replay, unknown costs retained |
| Power and energy | meter identity, calibration, idle baseline, sample interval, duration | pending | measured energy and explicit price; no TDP substitution |
| Long run | fixed workload/order, duration, thermal conditions, error/latency capture | pending | separately approved stability criteria satisfied |
| Quality / quantization | qualified pinned configuration, calibration/holdout split, contract signoff | pending | real answers independently verified, no base-score inheritance |
| Cloud comparison | same tasks/contract, selected configuration, complete retry/repair bill | pending | paired evidence, unknowns/failures retained; no synthetic savings claim |

Before running a real scenario, obtain its existing authorization, verify model
license/access, confirm backend location/privacy policy, review worst-case full
cost reservation and stop rules, select an isolated report root, and freeze
hardware/resource/contract settings. Loading a model does not approve any other
row. This delivery executed no real engine, hardware, download or paid API.

Offline evidence: `tests/test_benchmarking.py` exercises Core.run with synthetic
local and cloud responses, fake clock events, success, OOM, interruption, missing
resource evidence, exact arithmetic, immutable reuse and isolated case accounting.
