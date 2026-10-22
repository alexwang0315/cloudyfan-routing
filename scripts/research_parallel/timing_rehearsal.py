"""Public-metadata-only 50x4 offline timing sensitivity, never live dispatch."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

from parallel_runner import ARMS, CLASSIFIER_CAPS, GENERATION_CAPS, digest, read, simulate

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
ARCHIVE_ROOT = WORKSPACE / "artifacts/routing-parallel-four-arm-offline-20261022"
OLD = WORKSPACE / "artifacts/routing-v2-s50-20261022"
BANK = WORKSPACE / "question-banks/sql-stratified-50-v1-20261022"
EXPORT = OLD / "execution-unpaid-selection-v8/exports/609cd008d0b1481ca2ebea8c1f7b16f2"
BANK_SHA = "dd3357e73ee29c88b135b00e4541345f479bf4e9a5ee5bb5f67c4e890a2288fb"


def offline_guard(event, args):
    if event.startswith("socket."):
        raise RuntimeError("OFFLINE_TIMING_NETWORK_FORBIDDEN")
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        value = str(args[0]).replace("\\", "/")
        # This entry never needs private contracts, answers, authoring or keys.
        if any(part in value for part in ("/private/", "/authoring/", "scout-fusion/.env")):
            # macOS's /private/var/folders is the system tempfile location, so
            # scope 'private' specifically to the bank rather than the prefix.
            if (
                "sql-stratified-50-v1-20261022/private/" in value
                or "/authoring/" in value
                or value.endswith("scout-fusion/.env")
            ):
                raise RuntimeError("OFFLINE_TIMING_PRIVATE_INPUT_FORBIDDEN")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def trusted_old_file(path):
    path = Path(path).resolve()
    pins = read(ARCHIVE_ROOT / "preservation-before.json")["original_files"]
    if not path.is_relative_to(OLD.resolve()):
        raise ValueError("TIMING_SOURCE_OUTSIDE_FROZEN_STUDY")
    expected = pins.get(str(path.relative_to(WORKSPACE)))
    if expected is None or sha(path) != expected:
        raise ValueError("FROZEN_TIMING_INPUT_CHANGED")
    return expected


def verified_schedule():
    if sha(BANK / "freeze.json") != BANK_SHA:
        raise ValueError("BANK_FREEZE_CHANGED")
    freeze = read(BANK / "freeze.json")
    p = BANK / "public/schedule.json"
    if sha(p) != freeze["files"]["public/schedule.json"]:
        raise ValueError("PUBLIC_SCHEDULE_CHANGED")
    return read(p)["schedule"]


def four_arm_schedule(schedule=None):
    modes = {v: k for k, v in ARMS.items()}
    rows = []
    for row in verified_schedule() if schedule is None else schedule:
        ordered = [modes[mode] for mode in row["mode_order"]]
        ordered.insert((row["ordinal"] - 1) % 4, "D")
        rows.append({"ordinal": row["ordinal"], "question_id": row["question_id"], "arms": ordered})
    return rows


def trace_inputs():
    ledger_path, report_path = EXPORT / "ledger.json", EXPORT / "report.json"
    trusted_old_file(ledger_path)
    trusted_old_file(report_path)
    ledger, report = read(ledger_path), read(report_path)
    # Only timing metadata and public terminal availability; no answers and no
    # correctness labels are consumed to select settings or simulate quality.
    completed = {(r["question_id"], r["arm"]) for r in report["workflow_rows"] if r["completed"]}
    grouped, sources = (
        defaultdict(list),
        {str(ledger_path): sha(ledger_path), str(report_path): sha(report_path)},
    )
    all_wire = []
    for row in ledger["rows"]:
        reference = row.get("wire_evidence")
        if not reference or row.get("not_dispatched"):
            continue
        path = Path(reference["path"])
        if not path.resolve().is_relative_to(OLD.resolve()):
            raise ValueError("WIRE_EVIDENCE_OUTSIDE_STUDY")
        if sha(path) != reference["sha256"]:
            raise ValueError("HISTORICAL_WIRE_EVIDENCE_CHANGED")
        wire = read(path)
        if wire.get("method") != "POST" or wire.get("generation_id") != row["generation_id"]:
            raise ValueError("HISTORICAL_WIRE_IDENTITY_CHANGED")
        sources[str(path)] = sha(path)
        seconds = wire["wire_ms"] / 1000
        all_wire.append(seconds)
        if (row["question_id"], row["arm"]) in completed:
            grouped[row["question_id"], row["arm"]].append((row["call_id"], seconds))
    per_arm = defaultdict(list)
    for (_qid, arm), values in sorted(grouped.items()):
        # Call identifiers retain attempt/stage order; recorded usage is not
        # reinterpreted as new model latency or a new paid measurement.
        per_arm[arm].append([seconds for _, seconds in sorted(values)])
    preview = WORKSPACE / "artifacts/routing-resume-offline-20261022/preview-deferred-final-r4/preview.json"
    prior = read(preview)
    startup = sum(prior[k] for k in ("attach_seconds", "report_seconds", "ledger_seconds"))
    sources[str(preview)] = sha(preview)
    return per_arm, all_wire, startup, sources


def stages(durations, gate_seconds):
    result = []
    for duration in durations:
        result.extend(
            [
                {"kind": "account_gate", "seconds": gate_seconds},
                {"kind": "model", "seconds": duration},
                {"kind": "validation", "seconds": 0.5},
            ]
        )
    return result


def scenario_jobs(
    schedule,
    by_arm,
    wires,
    *,
    hosted_seconds,
    hosted_attempts,
    maximum_attempts=False,
    gate_seconds=0.05,
):
    counts = defaultdict(int)
    result = []
    for row in schedule:
        for arm in row["arms"]:
            index = counts[arm]
            counts[arm] += 1
            if arm == "D":
                durations = [hosted_seconds] * hosted_attempts
            elif maximum_attempts:
                # Stress bound includes every allowed self-built classifier.
                # The frozen actual bypass policy still makes0 paid Jev; this
                # is a capacity stress case, not a changed routing rule.
                durations = [
                    wires[(index * 11 + n) % len(wires)]
                    for n in range(GENERATION_CAPS[arm] + CLASSIFIER_CAPS[arm])
                ]
            else:
                durations = by_arm[arm][index % len(by_arm[arm])]
            result.append(
                {
                    "id": row["question_id"] + "-" + arm,
                    "arm": arm,
                    "stages": stages(durations, gate_seconds),
                }
            )
    return result


def prepare_plan(output):
    trusted_old_file(OLD / "execution-unpaid-selection-v8/study/plan.json")
    trusted_old_file(OLD / "launch-plan-unpaid-selection-v8.json")
    original = read(OLD / "execution-unpaid-selection-v8/study/plan.json")
    launch = read(OLD / "launch-plan-unpaid-selection-v8.json")
    schedule = four_arm_schedule()
    rows = read(EXPORT / "report.json")["workflow_rows"]
    reuse = [
        {
            "question_id": r["question_id"],
            "arm": r["arm"],
            "kind": "historical_link_only",
            "preserve_original_cost_and_latency": True,
        }
        for r in rows
        if r["completed"]
    ]
    plan = {
        "version": "s50-four-arm-parallel-research-offline-v2",
        "planned_workflows": 200,
        "question_count": 50,
        "arms": ARMS,
        "slots": 4,
        "validation_slots": 2,
        "bank_freeze_sha256": BANK_SHA,
        "original_three_arm_plan_sha256": sha(
            OLD / "execution-unpaid-selection-v8/study/plan.json"
        ),
        "original_plan_id": original["plan_id"],
        "schedule": schedule,
        "abc_relative_order_preserved": True,
        "hosted_insert_position": "ordinal-1 modulo4",
        "generation_caps": GENERATION_CAPS,
        "self_jev_caps": CLASSIFIER_CAPS,
        "abc_routing_policy": original["policy"],
        "abc_run_configuration_sha256": original["run_configuration_sha256"],
        "abc_run_public_task_sha256": original["run_public_task_sha256"],
        "abc_run_pack_sha256": original["run_pack_sha256"],
        "abc_public_contracts": original["public_contracts"],
        "actual_self_jev_policy": launch["actual_classifier_policy"],
        "shared_prompt_source": "public_adapter.public_contract(id).task(); frozen common system",
        "hosted_policy": {
            "model": "typesafe/jev-router",
            "cost_tier": "medium",
            "metadata_header": "enabled",
            "local_classifier": False,
            "answer_cap": 2,
            "repair": "latest actual answer and public checks only",
            "comparison_scope": "hosted_service_comparison",
            "identical_pool_enforcement": "unverified",
            "internal_advisor_attempt_count": "unknown_not_a_local_attempt",
        },
        "cost_scenarios": original["cost_comparison"],
        "historical_links": reuse,
        "historical_completed": len(reuse),
        "remaining_workflows_including_hosted": 200 - len(reuse),
        "s50_038_c_saved_response_not_resent": True,
        "hidden_grading": "only_after_all200_sealed; not read or run by this entry",
        "budget": {
            "target_usd": "1",
            "known_cumulative_usd": "0.243455651",
            "historical_unknown_calls": 1,
            "historical_unknown_reserve_usd": "0.01",
            "remaining_after_reserve_usd": "0.746544349",
            "hosted_complete_fee_upper": None,
            "abc_forecasts": launch["forecasts"],
            "in_flight_reserved": True,
            "unknown_stop": True,
            "authority": "expired20:00;offlineonly;no renewal",
        },
        "live_blockers": [
            "no_effective_paid_authorization",
            "hosted_complete_fee_reserve_not_verified",
            "hosted_serving_identity_and_official_fee_verifier_not_qualified",
            "production_account_index_gate_not_connected_to_new_bridge",
            "real_provider_parallel_rate_limits_unknown",
            "real_hosted_latency_unknown",
            "whole200_independent_grading_time_unmeasured",
        ],
        "paid_dispatch_enabled": False,
        "original_authorization_valid_until": original["valid_until"],
        "timing_target_seconds": 1800,
        "timing_target_includes": ["startup", "four_arm_capture", "independent_grading", "report"],
        "implementation_sha256": {p.name: sha(p) for p in sorted(ROOT.glob("*.py"))},
        "formal_qualification": "pending",
        "no_all_high_model_arm": True,
    }
    plan["plan_id"] = digest(plan)
    (output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    sys.addaudithook(offline_guard)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    plan = prepare_plan(args.output)
    by_arm, wires, startup, sources = trace_inputs()
    scenarios = []
    for attempts in (1, 2):
        for seconds in (10, 30, 60, 120):
            for maximum in (False, True):
                jobs = scenario_jobs(
                    plan["schedule"],
                    by_arm,
                    wires,
                    hosted_seconds=seconds,
                    hosted_attempts=attempts,
                    maximum_attempts=maximum,
                )
                r = simulate(jobs, startup_seconds=startup, grading_seconds=300, report_seconds=30)
                r.pop("launches")
                scenarios.append(
                    {
                        "hosted_seconds_per_request_assumed": seconds,
                        "hosted_attempts_assumed": attempts,
                        "abc_path_basis": "all_attempt_capacity_stress"
                        if maximum
                        else "historical_depth_cyclic_extrapolation",
                        "account_index_gate_seconds_assumed": 0.05,
                        "public_validation_seconds_assumed": 0.5,
                        **r,
                    }
                )
    jobs = scenario_jobs(
        plan["schedule"], by_arm, wires, hosted_seconds=30, hosted_attempts=2, gate_seconds=15
    )
    lag = simulate(jobs, startup_seconds=startup, grading_seconds=300, report_seconds=30)
    lag.pop("launches")
    unknown = simulate(
        scenario_jobs(plan["schedule"], by_arm, wires, hosted_seconds=30, hosted_attempts=2),
        startup_seconds=startup,
        stop_at=startup + 60,
    )
    unknown.pop("launches")
    result = {
        "kind": "offline_capacity_sensitivity_not_real_model_result",
        "plan_id": plan["plan_id"],
        "model_api_requests": 0,
        "independent_real_quality": "not_measured",
        "historical_wire": {
            "count": len(wires),
            "sum_seconds": sum(wires),
            "median_seconds": median(wires),
            "max_seconds": max(wires),
        },
        "historical_completed_workflows_used_for_depth_only": {
            a: len(v) for a, v in by_arm.items()
        },
        "startup_seconds_from_prior_readonly_inspection": startup,
        "grading_seconds_assumed_not_measured": 300,
        "report_seconds_assumed": 30,
        "scenarios": scenarios,
        "strict_account_index_lag_15s_case": lag,
        "unknown_fee_case": unknown,
        "source_sha256": sources,
        "original_paid_results_not_resent": True,
        "conclusion": "conditional_capacity_only; no_live_SLA_or_equal_quality_savings_claim",
    }
    (args.output / "timing-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "workflows": 200,
                "scenarios": len(scenarios),
                "conditional_passes": sum(r["under_30_minutes"] for r in scenarios),
                "real_api_requests": 0,
                "live_ready": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
