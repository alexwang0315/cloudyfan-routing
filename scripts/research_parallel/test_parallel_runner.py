import json
import threading
import time

import pytest
from cloudyfan_jev.client import CHAT
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from concurrent_ledger import ConcurrentLedger
from core_bridge import ConcurrentClient, HostedWorkflowRunner
from hosted_jev import HostedJevAdapter
from parallel_runner import CoreWorkflowRunner, Job, digest, file_evidence, simulate
from parallel_runner import FourSlotScheduler as _FourSlotScheduler


def FourSlotScheduler(root, **kwargs):
    return _FourSlotScheduler(
        root,
        evidence_provider=lambda j: file_evidence(root.parent / "capture" / j.run_id),
        **kwargs,
    )


def ledger(path, target="10"):
    return ConcurrentLedger(
        path / "ledger.sqlite",
        plan_id="test",
        target_usd=target,
        deadline=time.time() + 60,
        clock=time.time,
    )


def job(i, arm="A", forecast="0.02", bindings=None):
    return Job(f"test-{i}-{arm}", f"S50-{i:03d}", arm, i, forecast, bindings or {})


def proof(record):
    body = json.loads(record["raw"])
    return {
        "verified": True,
        "complete_request_cost": True,
        "source": "mock_official_total",
        "generation_id": body["id"],
        "cost_usd": str(body["usage"]["cost"]),
    }


def sender(count, answer="2", barrier=None):
    def send(cid, url, payload, headers):
        if barrier is not None:
            barrier.wait(timeout=5)
        count.append(cid)
        if url.endswith("decisions"):
            stage = next(iter(payload["questions"]))
            body = {
                "id": "gen-" + cid,
                "model": payload["model"],
                "provider": "TypeSafe",
                "usage": {"cost": "0.0001"},
                "answers": {
                    stage: {
                        "type": "choice",
                        "choice": "mid",
                        "probabilities": {"low": 0.1, "mid": 0.9},
                    }
                },
            }
        else:
            model = (
                "example/high" if payload["model"] == "typesafe/jev-router" else payload["model"]
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
                                "reason": "mock",
                                "candidates": [{"model": model, "estimated_cost": "99"}],
                            },
                        }
                    ]
                },
            }
        return {"raw": json.dumps(body), "http_status": 200}

    return send


def client(path, book, work, send, *, verifier=proof, gate=lambda *args: True):
    return ConcurrentClient(
        path / work,
        workflow_id=work,
        ledger=book,
        sender=send,
        fee_verifier=verifier,
        forecast=lambda *args: "0.002",
        pre_dispatch_check=gate,
    )


def test_two_hundred_jobs_four_slots_and_zero_post_replay(tmp_path):
    book = ledger(tmp_path)
    ids = [job(i, arm) for i in range(1, 51) for arm in "ABCD"]
    count, peaks = [], []
    active = [0]
    mutex = threading.Lock()
    first = threading.Barrier(4)

    def run(j):
        with mutex:
            active[0] += 1
            peaks.append(active[0])
            ordinal = len(peaks)
        if ordinal <= 4:
            first.wait(timeout=5)
        try:
            c = client(tmp_path / "capture", book, j.run_id, sender(count))
            c.call("call-" + j.run_id, CHAT, {"model": "example/low"})
            return {"final_public_pass": True}
        finally:
            with mutex:
                active[0] -= 1

    scheduler = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=run)
    result = scheduler.run(ids)
    assert result["newly_completed"] == 200 and result["remaining"] == 0
    assert max(peaks) == 4 and len(count) == 200
    actual = book.snapshot()["known_cumulative_usd"]
    replay = FourSlotScheduler(
        tmp_path / "study",
        plan_id="test",
        ledger=book,
        runner=lambda _: pytest.fail("Completed Core rebuilt"),
    )
    assert replay.run(ids)["reused_completed"] == 200
    assert len(count) == 200 and book.snapshot()["known_cumulative_usd"] == actual


def test_unknown_stops_new_work_and_preserves_other_inflight(tmp_path):
    book = ledger(tmp_path)
    barrier, count = threading.Barrier(4), []

    def run(j):
        c = client(
            tmp_path / "capture",
            book,
            j.run_id,
            sender(count, barrier=barrier),
            verifier=(lambda _: None) if j.ordinal == 1 else proof,
        )
        c.call("call-" + j.run_id, CHAT, {"model": "example/low"})
        return {"final_public_pass": True}

    r = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=run).run(
        [job(i) for i in range(1, 201)]
    )
    assert len(count) == 4 and r["newly_completed"] == 3 and r["remaining"] == 197
    assert book.snapshot()["global_stop"]
    assert all((tmp_path / "capture" / job(i).run_id / "calls").exists() for i in range(1, 5))


def test_stopped_fifth_call_is_not_completed_when_known_work_frees_a_slot(tmp_path, monkeypatch):
    import core_bridge

    book = ledger(tmp_path)
    barrier, count = threading.Barrier(4), []
    release_unknown, unknown_committed, fifth_intent = (
        threading.Event(), threading.Event(), threading.Event()
    )
    original_unknown, original_write = book.mark_unknown, core_bridge.write_once

    def mark_unknown(cid, reason):
        original_unknown(cid, reason)
        if cid == "call-test-1-A":
            unknown_committed.set()

    def write(path, value):
        original_write(path, value)
        if path.name == "intent.json" and value["call_id"] == "call-test-5-A":
            fifth_intent.set()
            release_unknown.set()
            assert unknown_committed.wait(5), "Original unknown transaction did not finish"

    monkeypatch.setattr(book, "mark_unknown", mark_unknown)
    monkeypatch.setattr(core_bridge, "write_once", write)

    def run(j):
        def verifier(record):
            if j.ordinal == 1:
                assert release_unknown.wait(5), "Fifth original intent did not reach its final gate"
                return None
            if j.ordinal in (3, 4):
                assert unknown_committed.wait(5), "Original unknown transaction did not finish"
            return proof(record)

        c = client(tmp_path / "capture", book, j.run_id, sender(count, barrier=barrier), verifier=verifier)
        # A trusted runner may ignore a stopped call's returned record. The
        # study must still require original dispatch evidence before sealing it.
        c.call("call-" + j.run_id, CHAT, {"model": "example/low"})
        return {"final_public_pass": True}

    scheduler = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=run)
    result = scheduler.run([job(i) for i in range(1, 201)])
    assert fifth_intent.is_set() and len(count) == 4
    assert result["newly_completed"] == 3 and result["remaining"] == 197
    assert any(row["run_id"] == "test-5-A" and row["reason"] == "WORKFLOW_NOT_DISPATCHED"
               for row in result["failures"])
    stopped = book.lookup("call-test-5-A")
    assert stopped["revision_id"] == "not_dispatched" and stopped["cost_usd"] == "0"
    assert stopped["unknown"] is False and book.snapshot()["unknown_calls"] == 1
    assert not (scheduler.directory(job(5)) / "terminal.json").exists()
    response = tmp_path / "capture/test-5-A/calls/call-test-5-A/response.json"
    assert json.loads(response.read_text())["not_dispatched"] is True


def test_legacy_completed_terminal_with_stopped_call_is_rejected_without_rewriting(tmp_path):
    import hashlib
    from parallel_runner import write_once

    book = ledger(tmp_path)
    j = job(1)
    book.admit(j.run_id, j.forecast_usd, j.generation_cap, j.classifier_cap)
    c = client(tmp_path / "capture", book, j.run_id, lambda *args: pytest.fail("Expired call sent"))
    c.clock = lambda: book.deadline
    assert c.call("stopped", CHAT, {"model": "example/low"})["not_dispatched"] is True
    scheduler = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book,
        runner=lambda _: pytest.fail("Sealed old result rerun"))
    directory = scheduler.directory(j)
    directory.mkdir(parents=True)
    result_path = directory / "result.json"
    write_once(result_path, {"plan_id": "test", "job_sha256": j.identity,
        "result": {"final_public_pass": True}})
    # Prior publisher format: the financial ledger legitimately knew zero,
    # but the study incorrectly sealed this explicitly never-sent attempt.
    terminal = {"plan_id": "test", "job_sha256": j.identity,
        "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
        "workflow_wall_seconds": 0, "state": "completed", "public_pass": True,
        "hidden_grading": "not_run", "replayed_result_api_latency": None,
        "evidence_sha256": file_evidence(tmp_path / "capture" / j.run_id)}
    book.finish(j.run_id)
    book.seal_completion(j.run_id, digest(terminal))
    write_once(directory / "terminal.json", terminal)
    before = book.snapshot()
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(RuntimeError, match="WORKFLOW_NOT_DISPATCHED"):
        scheduler.run([j])
    assert book.snapshot() == before
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value
               for path, value in hashes.items())


def test_verified_zero_fee_completion_and_cached_replay_remain_valid(tmp_path):
    book, calls = ledger(tmp_path), []
    j = job(1)
    original_sender = sender(calls)

    def send(*args):
        record = original_sender(*args)
        body = json.loads(record["raw"])
        body["usage"]["cost"] = "0"
        return {**record, "raw": json.dumps(body)}

    def run(j):
        client(tmp_path / "capture", book, j.run_id, send).call(
            "zero-fee", CHAT, {"model": "example/low"})
        return {"final_public_pass": True}

    scheduler = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=run)
    assert scheduler.run([j])["newly_completed"] == 1
    assert book.lookup("zero-fee")["revision_id"] == "usage"
    assert book.snapshot()["known_cumulative_usd"] == "0"
    scheduler.runner = lambda _: pytest.fail("Cached zero-fee result rerun")
    assert scheduler.run([j])["reused_completed"] == 1
    assert len(calls) == 1


def test_paid_response_before_engineering_crash_reuses_without_resend(tmp_path):
    book, count = ledger(tmp_path), []
    j = job(1)

    def run(j):
        c = client(tmp_path / "capture", book, j.run_id, sender(count))
        c.call("paid-once", CHAT, {"model": "example/low"})
        raise RuntimeError("offline engine interruption after paid response")

    root = tmp_path / "study"
    assert (
        FourSlotScheduler(root, plan_id="test", ledger=book, runner=run).run([j])["remaining"] == 1
    )

    def resume(j):
        c = client(
            tmp_path / "capture", book, j.run_id, lambda *args: pytest.fail("Paid request resent")
        )
        c.call("paid-once", CHAT, {"model": "example/low"})
        return {"final_public_pass": True}

    assert (
        FourSlotScheduler(root, plan_id="test", ledger=book, runner=resume).run([j])["remaining"]
        == 0
    )
    assert len(count) == 1


def test_prepared_result_seals_without_running_core(tmp_path, monkeypatch):
    book = ledger(tmp_path)

    def run(j):
        client(tmp_path / "capture", book, j.run_id, sender([])).call(
            "paid", CHAT, {"model": "example/low"}
        )
        return {"final_public_pass": True}

    scheduler = FourSlotScheduler(
        tmp_path / "study",
        plan_id="test",
        ledger=book,
        runner=run,
    )
    original = book.finish

    def crash(_):
        raise RuntimeError("crash before terminal")

    monkeypatch.setattr(book, "finish", crash)
    assert scheduler.run([job(1)])["remaining"] == 1
    monkeypatch.setattr(book, "finish", original)
    scheduler.runner = lambda _: pytest.fail("prepared result rerun")
    assert scheduler.run([job(1)])["newly_completed"] == 1


def test_plan_or_terminal_tamper_rejected(tmp_path):
    book = ledger(tmp_path)

    def run(j):
        client(tmp_path / "capture", book, j.run_id, sender([])).call(
            "paid", CHAT, {"model": "example/low"}
        )
        return {"final_public_pass": True}

    scheduler = FourSlotScheduler(
        tmp_path / "study",
        plan_id="test",
        ledger=book,
        runner=run,
    )
    scheduler.run([job(1)])
    terminal = scheduler.directory(job(1)) / "result.json"
    terminal.write_text("{}")
    with pytest.raises(ValueError, match="EVIDENCE_CHANGED"):
        scheduler.run([job(1)])


def test_insufficient_concurrent_reserve_waits_without_skipping(tmp_path):
    book = ledger(tmp_path, target="0.05")
    count = []

    def run(j):
        client(tmp_path / "capture", book, j.run_id, sender(count)).call(
            "call-" + j.run_id, CHAT, {"model": "example/low"}
        )
        return {"final_public_pass": True}

    r = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=run).run(
        [job(i, forecast="0.04") for i in range(1, 5)]
    )
    assert r["newly_completed"] == 4 and not r["failures"]


def test_gate_denial_has_no_dispatch_or_paid_intent(tmp_path):
    book = ledger(tmp_path)
    book.admit("work", "0.02", 3, 2)
    c = client(
        tmp_path, book, "work", lambda *args: pytest.fail("denied POST"), gate=lambda *args: False
    )
    with pytest.raises(RuntimeError, match="GATE_DENIED"):
        c.call("call1", CHAT, {"model": "example/low"})
    assert book.snapshot()["calls"] == []


@pytest.mark.parametrize(
    "mode,arm", [("cloudyfan", "A"), ("low_first", "B"), ("open_source_first", "C")]
)
def test_current_core_routing_and_public_validator_unmodified(tmp_path, mode, arm):
    book, count = ledger(tmp_path), []
    task = {"prompt": "Return only 2."}
    profiles, decision = example_config()

    def factory(j):
        c = client(tmp_path / "capture", book, j.run_id, sender(count))
        return Core(c, profiles, decision, limits=Limits(max_turns=1))

    core_descriptor = factory(job(1, arm)).configuration_descriptor()
    bindings = {
        "task_sha256": digest(task),
        "contract_version": "v1",
        "core_configuration_sha256": digest(core_descriptor),
    }
    j = job(1, arm, bindings=bindings)
    runner = CoreWorkflowRunner(lambda _: (task, "v1", ArithmeticEnvironment), factory)
    r = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=runner).run([j])
    assert r["remaining"] == 0 and len(count) == 1


def test_hosted_fourth_arm_common_prompt_repair_and_two_attempt_limit(tmp_path):
    book, count = ledger(tmp_path), []
    task = {"prompt": "Return only 2."}

    class Public:
        def validate(self, answer):
            return {"passed": False, "checks": ["SQL_RESULT_MISMATCH"]}

    adapter = HostedJevAdapter(
        output_contract="Return only 2.", accounting_verifier=lambda record, proof, packet: proof
    )
    runner = HostedWorkflowRunner(
        adapter,
        lambda j: client(tmp_path / "capture", book, j.run_id, sender(count, answer="wrong")),
        lambda j: (task, "v1", Public),
        tmp_path / "hosted",
        system="Common frozen system.",
    )
    j = job(1, "D", bindings={"task_sha256": digest(task), "contract_version": "v1"})
    r = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=runner).run([j])
    assert r["remaining"] == 0 and len(count) == 2
    files = list((tmp_path / "capture" / j.run_id / "calls").glob("*/intent.json"))
    packets = [json.loads(p.read_text())["payload"] for p in files]
    assert all(
        p["messages"][0] == {"role": "system", "content": "Common frozen system."} for p in packets
    )
    assert any(
        any(m["role"] == "assistant" and m["content"] == "wrong" for m in p["messages"])
        for p in packets
    )
    assert all(p["model"] == "typesafe/jev-router" for p in packets)
    assert book.snapshot()["known_cumulative_usd"] == "0.002"


def test_no_resend_after_orphan_or_changed_raw(tmp_path):
    book, count = ledger(tmp_path), []
    book.admit("work", "0.02", 3, 2)
    c = client(tmp_path, book, "work", sender(count))
    c.call("one", CHAT, {"model": "example/low"})
    (c._directory("one") / "response.json").write_text("{}")
    with pytest.raises(ValueError, match="SAVED_RESPONSE_CHANGED"):
        c.call("one", CHAT, {"model": "example/low"})
    assert len(count) == 1 and book.snapshot()["global_stop"]


@pytest.mark.parametrize("change", ["request", "workflow", "intent_call", "intent_payload"])
def test_cached_paid_request_must_match_ledger_and_workflow(tmp_path, change):
    book, count = ledger(tmp_path), []
    book.admit("work", "0.02", 3, 2)
    c = client(tmp_path, book, "work", sender(count))
    payload = {"model": "example/low"}
    c.call("one", CHAT, payload)
    path = c._directory("one") / "intent.json"
    intent = json.loads(path.read_text())
    if change == "request":
        payload = {"model": "example/high"}
        intent["payload"] = payload
        intent["request_sha256"] = digest(
            {k: intent[k] for k in ("url", "payload", "headers")}
        )
    elif change == "workflow":
        c.workflow_id = "other-workflow"
        intent["workflow_id"] = c.workflow_id
    elif change == "intent_call":
        intent["call_id"] = "different-call"
    else:
        intent["payload"] = {"model": "changed-with-old-hash"}
    path.write_text(json.dumps(intent))
    with pytest.raises(ValueError, match="SAVED_PAID_REQUEST_CHANGED"):
        c.call("one", CHAT, payload)
    assert len(count) == 1 and book.snapshot()["global_stop"]
    assert book.snapshot()["actual_usd"] == "0.001"


def test_changed_local_intent_without_ledger_stops_durably(tmp_path):
    original, count = ledger(tmp_path / "old"), []
    original.admit("work", "0.02", 3, 2)
    c = client(tmp_path, original, "work", sender(count))
    c.call("one", CHAT, {"model": "example/low"})
    replacement = ledger(tmp_path / "replacement")
    c.ledger = replacement
    with pytest.raises(RuntimeError, match="MISSING_LEDGER_IMPORT"):
        c.call("one", CHAT, {"model": "example/high"})
    assert len(count) == 1 and replacement.snapshot()["global_stop"]


@pytest.mark.parametrize("artifact", ["response.json", "verified-fee.json", "broken_intent"])
def test_orphan_or_unreadable_saved_evidence_never_creates_paid_send(tmp_path, artifact):
    book, count = ledger(tmp_path), []
    book.admit("work", "0.02", 3, 2)
    c = client(tmp_path, book, "work", sender(count))
    directory = c._directory("one")
    directory.mkdir(parents=True)
    if artifact == "broken_intent":
        book.begin("one", "work", "generation", "0.002", "0" * 64)
        (directory / "intent.json").write_text('{"truncated":')
    else:
        (directory / artifact).write_text('{}')
    with pytest.raises(RuntimeError, match="NEVER_RESEND"):
        c.call("one", CHAT, {"model": "example/low"})
    assert count == [] and book.snapshot()["global_stop"]


@pytest.mark.parametrize("error_kind", [PermissionError, OSError])
def test_saved_response_read_error_stops_without_retransmission(tmp_path, monkeypatch, error_kind):
    from pathlib import Path

    book, count = ledger(tmp_path), []
    book.admit("work", "0.02", 3, 2)
    c = client(tmp_path, book, "work", sender(count))
    c.call("one", CHAT, {"model": "example/low"})
    response = c._directory("one") / "response.json"
    original = Path.read_bytes

    def unreadable(path):
        if path == response:
            raise error_kind("simulated saved response read failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(RuntimeError, match="SAVED_RESPONSE_UNREADABLE_NEVER_RESEND"):
        c.call("one", CHAT, {"model": "example/low"})
    assert len(count) == 1 and book.snapshot()["global_stop"]
    assert book.snapshot()["actual_usd"] == "0.001"


def test_changed_completed_public_pass_and_paid_source_are_rejected(tmp_path):
    book, count = ledger(tmp_path), []
    j = job(1)

    def run(j):
        client(tmp_path / "capture", book, j.run_id, sender(count)).call(
            "paid", CHAT, {"model": "example/low"}
        )
        return {"final_public_pass": True}

    scheduler = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=run)
    scheduler.run([j])
    p = scheduler.directory(j) / "terminal.json"
    original = p.read_bytes()
    terminal = json.loads(original)
    terminal["public_pass"] = False
    p.write_text(json.dumps(terminal))
    with pytest.raises(ValueError, match="EVIDENCE_CHANGED"):
        scheduler.run([j])
    p.write_bytes(original)
    response = next((tmp_path / "capture" / j.run_id).rglob("response.json"))
    response.write_text("{}")
    with pytest.raises(ValueError, match="EVIDENCE_CHANGED"):
        scheduler.run([j])
    assert len(count) == 1


def test_deadline_crossing_intent_fsync_does_not_dispatch_or_create_unknown(tmp_path, monkeypatch):
    import core_bridge

    now = [0.0]
    book = ConcurrentLedger(
        tmp_path / "ledger.sqlite",
        plan_id="test",
        target_usd="1",
        deadline=10,
        clock=lambda: now[0],
    )
    book.admit("work", "0.02", 3, 2)
    c = client(tmp_path, book, "work", lambda *args: pytest.fail("expired POST"))
    c.clock = lambda: now[0]
    original = core_bridge.write_once

    def slow_intent(path, value):
        original(path, value)
        if path.name == "intent.json":
            now[0] = 11

    monkeypatch.setattr(core_bridge, "write_once", slow_intent)
    result = c.call("late", CHAT, {"model": "example/low"})
    assert result["not_dispatched"] is True
    assert book.snapshot()["known_cumulative_usd"] == "0"
    assert book.snapshot()["unknown_calls"] == 0


def test_prepost_ports_cannot_mutate_frozen_request(tmp_path):
    book = ledger(tmp_path)
    book.admit("work", "0.02", 3, 2)
    original = {"model": "example/low"}

    def mutate(cid, url, payload):
        payload["model"] = "silently-widened"
        return True

    c = client(tmp_path, book, "work", lambda *args: pytest.fail("mutated POST"), gate=mutate)
    with pytest.raises(ValueError, match="CHANGED_REQUEST"):
        c.call("changed", CHAT, original)
    assert original == {"model": "example/low"} and book.snapshot()["calls"] == []


def test_simulation_has_four_slots_and_separate_bounded_validation():
    jobs = [
        {
            "id": str(i),
            "stages": [{"kind": "model", "seconds": 10}, {"kind": "validation", "seconds": 1}],
        }
        for i in range(200)
    ]
    r = simulate(jobs, startup_seconds=10, grading_seconds=30, report_seconds=5)
    assert r["completed"] == 200 and r["under_30_minutes"]
    assert r["peak_workflow_slots"] == 4 and r["end_to_end_seconds"] >= 595
    assert r["real_api_requests"] == 0 and not r["quality_measured"]


def test_simulated_unknown_stops_without_cancelling_inflight():
    jobs = [
        {
            "id": str(i),
            "stages": [{"kind": "model", "seconds": 10}, {"kind": "validation", "seconds": 1}],
        }
        for i in range(200)
    ]
    r = simulate(jobs, stop_at=15)
    assert r["stopped_on_unknown"] and r["remaining"] > 0
    assert not any(x["at"] >= 15 for x in r["launches"])
    assert not r["under_30_minutes"]


def test_slow_provider_and_global_account_lag_cannot_claim_thirty_minutes():
    jobs = [
        {
            "id": str(i),
            "stages": [{"kind": "account_gate", "seconds": 15}, {"kind": "model", "seconds": 80}],
        }
        for i in range(200)
    ]
    r = simulate(jobs)
    assert not r["under_30_minutes"] and r["remaining"] > 0


def test_deadline_and_invalid_timings_fail_closed():
    with pytest.raises(ValueError):
        simulate([{"id": "bad", "stages": [{"kind": "model", "seconds": float("nan")}]}])
    assert (
        simulate([{"id": "long", "stages": [{"kind": "model", "seconds": 1801}]}])[
            "under_30_minutes"
        ]
        is False
    )


def test_caller_cannot_change_queued_job_bindings(tmp_path):
    book, original, seen = ledger(tmp_path), job(1, bindings={"setting": "low"}), []
    scheduler = FourSlotScheduler(
        tmp_path / "study",
        plan_id="test",
        ledger=book,
        runner=lambda j: (_ for _ in ()).throw(RuntimeError("intentional after inspection")),
    )
    normal = scheduler._work

    def worker(j):
        original.bindings["setting"] = "high"
        seen.append(j.bindings["setting"])
        return normal(j)

    scheduler._work = worker
    scheduler.run([original])
    assert seen == ["low"]


def test_historical_completed_is_verified_not_charged_zero_or_resent(tmp_path):
    import hashlib

    from parallel_runner import HistoricalCompletion

    book = ConcurrentLedger(
        tmp_path / "ledger.sqlite",
        plan_id="test",
        target_usd="1",
        prior_known_usd="0.12",
        historical_reserve_usd="0.01",
        deadline=0,
        clock=lambda: 1,
    )
    old = tmp_path / "old-terminal.json"
    old.write_text('{"public_pass": true}')
    j = job(1)
    link = HistoricalCompletion(
        j.identity,
        {str(old): hashlib.sha256(old.read_bytes()).hexdigest()},
        {"public_pass": True, "actual_api_usd": "0.03", "api_latency_is_historical": True},
        "0.12",
        "0.01",
    )
    scheduler = FourSlotScheduler(
        tmp_path / "study",
        plan_id="test",
        ledger=book,
        runner=lambda _: pytest.fail("historical resent"),
        historical_links={j.run_id: link},
    )
    r = scheduler.run([j])
    assert r["historical_completed"] == 1 and r["remaining"] == 0
    assert book.snapshot()["known_cumulative_usd"] == "0.12"
    assert book.snapshot()["calls"] == []
    old.write_text("{}")
    with pytest.raises(ValueError, match="HISTORICAL_PAID_SOURCE_CHANGED"):
        scheduler.run([j])


def test_partial_historical_paid_answer_import_uses_no_new_cost_or_send(tmp_path):
    import hashlib

    from cloudyfan_jev.receipt_store import _encoded

    now = [0.0]
    book = ConcurrentLedger(
        tmp_path / "ledger.sqlite",
        plan_id="test",
        target_usd="1",
        prior_known_usd="0.12",
        deadline=10,
        clock=lambda: now[0],
    )
    book.admit("work", "0.02", 3, 2)
    old = tmp_path / "old"
    old.mkdir()
    packet = {"url": CHAT, "payload": {"model": "example/low"}}
    reqsha = hashlib.sha256(_encoded(packet)).hexdigest()
    intent = {**packet, "call_id": "old-paid", "request_sha256": reqsha}
    record = {**sender([])("old-paid", CHAT, packet["payload"], {}), "request_sha256": reqsha}
    (old / "intent.json").write_text(json.dumps(intent))
    (old / "response.json").write_text(json.dumps(record))
    refs = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in old.glob("*.json")}
    entry = {
        "source_evidence": refs,
        "intent_path": str(old / "intent.json"),
        "response_path": str(old / "response.json"),
        "required_prior_known_usd": "0.12",
    }
    now[0] = 11
    c = ConcurrentClient(
        tmp_path / "new",
        workflow_id="work",
        ledger=book,
        sender=lambda *args: pytest.fail("historical POST"),
        fee_verifier=proof,
        forecast=lambda *args: "0.002",
        pre_dispatch_check=lambda *args: False,
        historical_calls={"old-paid": entry},
        clock=lambda: now[0],
    )
    first = c.call("old-paid", CHAT, {"model": "example/low", "session_id": "local-trace-only"})
    assert first["historical_reused"] and first["replay_api_latency_ms"] is None
    c.call("old-paid", CHAT, {"model": "example/low"})
    assert book.snapshot()["known_cumulative_usd"] == "0.12"
    assert book.lookup_workflow("work")["generations_started"] == 1


def test_current_core_two_jev_stages_use_latest_answer_and_caps(tmp_path):
    book, packets = ledger(tmp_path), []
    base = sender([])

    def send(cid, url, payload, headers):
        packets.append((url, payload))
        record = base(cid, url, payload, headers)
        body = json.loads(record["raw"])
        if url.endswith("decisions"):
            stage = next(iter(payload["questions"]))
            choice = "mid" if stage == "low_mid" else "high"
            body["answers"][stage] = {
                "type": "choice",
                "choice": choice,
                "probabilities": (
                    {"low": 0.1, "mid": 0.9} if stage == "low_mid" else {"mid": 0.1, "high": 0.9}
                ),
            }
        else:
            body["choices"][0]["message"]["content"] = {
                "example/open_source": "first-bad",
                "example/mid": "second-bad",
                "example/high": "2",
            }[payload["model"]]
        return {**record, "raw": json.dumps(body)}

    profiles, decision = example_config()
    task = {"prompt": "Return exactly 2."}

    def factory(j):
        return Core(
            client(tmp_path / "capture", book, j.run_id, send),
            profiles,
            decision,
            limits=Limits(max_turns=1),
        )

    j = job(
        1,
        bindings={
            "task_sha256": digest(task),
            "contract_version": "v1",
            "core_configuration_sha256": digest(factory(job(1)).configuration_descriptor()),
        },
    )
    runner = CoreWorkflowRunner(lambda j: (task, "v1", ArithmeticEnvironment), factory)
    r = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=runner).run([j])
    assert r["remaining"] == 0 and len(packets) == 5
    decisions = [p for url, p in packets if url.endswith("decisions")]
    assert len(decisions) == 2 and "second-bad" in json.dumps(decisions[1])
    assert "first-bad" not in json.dumps(decisions[1])
    w = book.lookup_workflow(j.run_id)
    assert w["generations_started"] == 3 and w["classifiers_started"] == 2


def test_hosted_resume_reuses_sealed_public_validation(tmp_path, monkeypatch):
    book, count, validated = ledger(tmp_path), [], []
    task = {"prompt": "Return exactly 2."}

    class Public:
        def validate(self, answer):
            validated.append(answer)
            return {"passed": False, "checks": ["SQL_RESULT_MISMATCH"]}

    adapter = HostedJevAdapter(
        output_contract="Return exactly 2.", accounting_verifier=lambda r, p, q: p
    )
    runner = HostedWorkflowRunner(
        adapter,
        lambda j: client(tmp_path / "capture", book, j.run_id, sender(count, answer="wrong")),
        lambda j: (task, "v1", Public),
        tmp_path / "hosted",
        system="Common frozen system",
    )
    j = job(1, "D", bindings={"task_sha256": digest(task), "contract_version": "v1"})
    original = adapter.build_request

    def stop_before_second(*args, **kwargs):
        if kwargs.get("attempt") == 1:
            raise RuntimeError("engineering interrupt after sealed validation")
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "build_request", stop_before_second)
    s = FourSlotScheduler(tmp_path / "study", plan_id="test", ledger=book, runner=runner)
    assert s.run([j])["remaining"] == 1 and len(count) == len(validated) == 1
    monkeypatch.setattr(adapter, "build_request", original)
    assert s.run([j])["remaining"] == 0
    assert len(count) == 2 and len(validated) == 2
