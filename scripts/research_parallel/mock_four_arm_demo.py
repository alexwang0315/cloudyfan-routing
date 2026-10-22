"""200 synthetic plumbing workflows, not S50 answers or live model evidence."""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from cloudyfan_jev.demo import example_config
from cloudyfan_jev.engine import Core, Limits
from concurrent_ledger import ConcurrentLedger
from core_bridge import ConcurrentClient, HostedWorkflowRunner
from hosted_jev import HostedJevAdapter
from parallel_runner import CoreWorkflowRunner, FourSlotScheduler, Job, digest, file_evidence, read
from timing_rehearsal import four_arm_schedule, offline_guard


class PublicToyEnvironment:
    def tools_schema(self):
        return []

    def call(self, *args):
        raise ValueError("No tools")

    def validate(self, answer):
        return {
            "passed": answer == "2",
            "checks": [] if answer == "2" else ["INTEGER_RESULT_MISMATCH"],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    sys.addaudithook(offline_guard)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.perf_counter()
    book = ConcurrentLedger(
        args.output / "ledger.sqlite",
        plan_id="mock-four-arm",
        target_usd="1",
        prior_known_usd="0.243455651",
        historical_reserve_usd="0.01",
        deadline=time.time() + 1800,
        clock=time.time,
    )
    mutex, barrier = threading.Lock(), threading.Barrier(4)
    counters = {"mock_calls": 0, "active_mock_calls": 0, "peak_mock_calls": 0}

    def send(cid, url, payload, headers):
        with mutex:
            counters["mock_calls"] += 1
            number = counters["mock_calls"]
            counters["active_mock_calls"] += 1
            counters["peak_mock_calls"] = max(
                counters["peak_mock_calls"], counters["active_mock_calls"]
            )
        try:
            if number <= 4:
                barrier.wait(timeout=10)
            if url.endswith("decisions"):
                stage = next(iter(payload["questions"]))
                choice = "mid" if stage == "low_mid" else "high"
                body = {
                    "id": "gen-" + cid,
                    "model": payload["model"],
                    "provider": "TypeSafe",
                    "usage": {"cost": "0.0001"},
                    "answers": {
                        stage: {
                            "type": "choice",
                            "choice": choice,
                            "probabilities": (
                                {"low": 0.1, "mid": 0.9}
                                if stage == "low_mid"
                                else {"mid": 0.1, "high": 0.9}
                            ),
                        }
                    },
                }
            else:
                hosted = payload["model"] == "typesafe/jev-router"
                model = "example/high" if hosted else payload["model"]
                answer = (
                    "2"
                    if (hosted and cid.endswith("a1")) or (not hosted and model == "example/high")
                    else "wrong"
                )
                body = {
                    "id": "gen-" + cid,
                    "model": model,
                    "provider": "Example",
                    "usage": {"cost": "0.001"},
                    "choices": [{"message": {"content": answer}}],
                    "openrouter_metadata": {
                        "pipeline": [
                            {
                                "name": "jev-router",
                                "data": {
                                    "resolved_models": [model],
                                    "reason": "synthetic fixture, not actual routing",
                                    "advisor_model": None,
                                    "candidates": [{"model": model, "estimated_cost": "999"}],
                                },
                            }
                        ]
                    },
                }
            return {"raw": json.dumps(body), "http_status": 200}
        finally:
            with mutex:
                counters["active_mock_calls"] -= 1

    def fees(record):
        body = json.loads(record["raw"])
        return {
            "verified": True,
            "complete_request_cost": True,
            "source": "mock_complete_fee_fixture",
            "generation_id": body["id"],
            "cost_usd": body["usage"]["cost"],
        }

    def client(job):
        return ConcurrentClient(
            args.output / "capture" / job.run_id,
            workflow_id=job.run_id,
            ledger=book,
            sender=send,
            fee_verifier=fees,
            forecast=lambda *args: "0.002",
            pre_dispatch_check=lambda *args: True,
        )

    profiles, decision = example_config()

    def make_core(job):
        return Core(client(job), profiles, decision, limits=Limits(max_turns=1))

    common_task = {"prompt": "Return only the integer sum of 1 and 1."}
    template = make_core(Job("mock-template", "mock-template", "A", 0, "0.02", {}))
    config_sha, system = digest(template.configuration_descriptor()), template.system
    adapter = HostedJevAdapter(
        output_contract="Return only the integer sum.",
        accounting_verifier=lambda saved, proof, packet: proof,
    )

    def loader(job):
        return common_task, "mock-integer-sum-v1", PublicToyEnvironment

    hosted = HostedWorkflowRunner(adapter, client, loader, args.output / "hosted", system=system)
    runner = CoreWorkflowRunner(loader, make_core, hosted)
    jobs = []
    # Public identifiers/order only: runnable without the private historical tree.
    for row in four_arm_schedule(read(Path(__file__).with_name("public-schedule.json"))["schedule"]):
        for arm in row["arms"]:
            jobs.append(
                Job(
                    "mock-" + row["question_id"] + "-" + arm,
                    row["question_id"],
                    arm,
                    row["ordinal"],
                    "0.02",
                    {
                        "task_sha256": digest(common_task),
                        "contract_version": "mock-integer-sum-v1",
                        "core_configuration_sha256": config_sha,
                    },
                )
            )
    scheduler = FourSlotScheduler(
        args.output / "study",
        plan_id="mock-four-arm",
        ledger=book,
        runner=runner,
        evidence_provider=lambda job: file_evidence(args.output / "capture" / job.run_id),
    )
    first = scheduler.run(jobs)
    before = counters["mock_calls"]
    scheduler.runner = lambda _: (_ for _ in ()).throw(RuntimeError("Completed Core must not run"))
    replay = scheduler.run(jobs)
    summary = {
        "data_kind": "synthetic_plumbing_only_not_S50_model_answers",
        "scheduled_workflows": len(jobs),
        "newly_completed": first["newly_completed"],
        "remaining": first["remaining"],
        "failures": first["failures"],
        "counters": counters,
        "replay_completed": replay["reused_completed"],
        "replay_extra_mock_calls": counters["mock_calls"] - before,
        "actual_model_api_requests": 0,
        "actual_api_cost_usd": "0",
        "synthetic_new_cost_usd": book.snapshot()["actual_usd"],
        "real_independent_quality": "not_measured",
        "elapsed_offline_seconds": time.perf_counter() - started,
        "uses_current_base_core": True,
        "base_core_branch_fixture_not_frozen_QCS_pool_activation": True,
        "frozen_SQL_or_question_inputs_changed": False,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    if first["remaining"] or len(first["failures"]) or summary["replay_extra_mock_calls"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
