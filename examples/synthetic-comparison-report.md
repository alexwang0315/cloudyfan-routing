# Frozen-plan comparison (synthetic)

Plan: `453c4349dff7d6f8411654c234ed0de7ebbb8a747c2751b3dbcb3ea3100c62fb`. Cost basis: **recorded_api_fees**.
Posthoc: False; cache: disabled.

Costs include generation, classification, retry, repair, failure and tool events.
Pass denominators include every planned task, including missing evidence.

| Arm | First pass | Final pass | Failed / unverifiable / interrupted / unknown / missing | Complete API USD | USD per success | End-to-end mean / median ms (n) | Repair rate |
|---|---|---|---|---|---|---|---|
| fixed_high | 2/2 | 2/2 | 0 / 0 / 0 / 0 / 0 | 10 | 5 | 150 / 150 (2) | 0 |
| cloudyfan | 2/2 | 2/2 | 0 / 0 / 0 / 0 / 0 | 7 | 3.5 | 150 / 150 (2) | 0 |

| Arm | Classification / generation / retry / tool calls | Unknown fee events / incomplete flows | Validation mean / median ms (n) | Queue mean / median ms (n) |
|---|---|---|---|---|
| fixed_high | 0 / 2 / 0 / 0 | 0 / 0 | 10 / 10 (2) | 0 / 0 (2) |
| cloudyfan | 0 / 2 / 0 / 0 | 0 / 0 | 10 / 10 (2) | 0 / 0 (2) |

| Compared with fixed_high | Pairs | Aggregate CDR % | Per-task mean / median / min / max % | More expensive fraction | Claim |
|---|---|---|---|---|---|
| cloudyfan | 2 | 30.0 | 37.50 / 37.50 / 25.00 / 50.0 | 0 | synthetic_comparison |

Positive CDR means lower candidate spending; zero baseline or unknown fees is undefined.
Calibration USD: 0; unpaired holdout USD: 0; unattributed USD: 0.
Jev-only gain is not identified: same first configuration, inputs, public validation and repair contract is required.
A passed descriptive quality gate is not statistical equivalence or evidence of commercial profitability.
Detailed JSON retains validation/queue timing sample counts, retries, unknowns, evidence anomalies and strata.
