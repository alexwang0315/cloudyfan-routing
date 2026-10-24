"""Actual shared capacity queue over independent loopback HTTP processes."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from capacity_queue_process_fixture import queue_process_pair as queue_process_pair
from test_local_http_integration import loopback_network as loopback_network, TASK


def test_two_processes_concurrently_submit_same_identity_then_cancel_without_paid_work(queue_process_pair):
    with queue_process_pair() as pair:
        pair.activate()
        first, second = pair.client(0), pair.client(1)
        task = {**TASK, "run_id": "process-idempotent-cancel"}
        with ThreadPoolExecutor(2) as requests:
            responses = list(requests.map(lambda client: client.post("/v1/tasks", json=task), (first, second)))
        assert [response.status_code for response in responses] == [202, 202]
        assert responses[0].json()["queue"]["accepted_at"] == responses[1].json()["queue"]["accepted_at"]
        assert pair.view("/monitor/queue", 1)["job_count"] == 1
        assert first.post("/v1/tasks", json={**task, "prompt": "changed original"}).status_code == 409
        with ThreadPoolExecutor(2) as requests:
            responses = list(requests.map(lambda client: client.post("/v1/tasks/" + task["run_id"] + "/cancel", json={}),
                (first, second)))
        assert all(response.status_code == 200 and response.json()["status"] == "cancelled" for response in responses)
        assert first.post("/v1/tasks", json=task).json()["status"] == "cancelled"
        assert second.get("/v1/tasks/" + task["run_id"]).json()["provider_cancellation_confirmed"] is False
        assert pair.client(1, principal="agent").get("/v1/tasks/" + task["run_id"]).status_code == 404
        assert pair.calls() == [] and pair.receipt_hashes() == {}
        assert all(not ledger["events"] and not ledger["reservations"] for ledger in pair.ledgers().values())
        before = pair.protected_hashes()
        retention = pair.retention_tasks()
        pair.clock.advance(10)
        for index in (0, 1):
            pair.view("/monitor/queue", index)
            pair.capacity(index)
            pair.view("/monitor/snapshot", index)
            assert pair.client(index).get("/v1/tasks/" + task["run_id"]).json()["status"] == "cancelled"
        assert pair.protected_hashes() == before
        assert pair.retention_tasks() == retention
        assert pair.calls() == []


def test_concurrent_real_workers_claim_one_original_request_once(queue_process_pair):
    with queue_process_pair() as pair:
        pair.activate()
        task = {**TASK, "run_id": "process-one-claim"}
        accepted = pair.client(0).post("/v1/tasks", json=task)
        assert accepted.status_code == 202, accepted.text
        assert pair.calls() == [] and pair.receipt_hashes() == {}
        commands = [pair.dispatch(index) for index in (0, 1)]
        results = [pair.dispatched(index, command) for index, command in enumerate(commands)]
        completed = [result for result in results if result is not None]
        assert len(completed) == 1 and completed[0]["status"] == "passed", results
        assert len(pair.calls()) == 1
        assert len([path for path in pair.receipt_hashes() if path.endswith("intent.json")]) == 1
        assert len([path for path in pair.receipt_hashes() if path.endswith("response.json")]) == 1
        accounts = pair.ledgers()
        assert sum(len(ledger["events"]) for ledger in accounts.values()) == 1
        assert sum(len(ledger["reservations"]) for ledger in accounts.values()) == 1
        before = pair.protected_hashes()
        retention = pair.retention_tasks()
        for index in (0, 1):
            assert pair.client(index).post("/v1/tasks", json=task).json()["status"] == "passed"
            assert pair.client(index).get("/v1/tasks/" + task["run_id"]).json()["status"] == "passed"
            pair.view("/monitor/queue", index)
            pair.capacity(index)
            pair.view("/monitor/snapshot", index)
        assert pair.protected_hashes() == before
        assert pair.retention_tasks() == retention
        assert len(pair.calls()) == 1


def test_overlapping_groups_preserve_fifo_and_all_group_claims_are_atomic(queue_process_pair):
    with queue_process_pair(first_mode="hold", separate_budgets=True, disjoint=True, overlap_keys=True) as pair:
        pair.activate()
        pair.mode("agent", "open_source_first")
        first, second = pair.client(0), pair.client(1, principal="agent")
        tasks = [{**TASK, "run_id": name} for name in ("fifo-active-a", "fifo-waiting-a", "fifo-later-b")]
        assert first.post("/v1/tasks", json=tasks[0]).status_code == 202
        active = pair.dispatch(0)
        pair.entered(0)
        assert first.post("/v1/tasks", json=tasks[1]).status_code == 202
        assert second.post("/v1/tasks", json=tasks[2]).status_code == 202
        waiting_receipts, waiting_ledgers = pair.receipt_hashes(), pair.ledgers()
        blocked = pair.dispatch(1)
        assert pair.dispatched(1, blocked) is None
        capacity = pair.capacity(1)
        assert len(capacity["allocations"]) == 1
        assert pair.group(capacity, "shared")["concurrency_used"] == 1
        for field in ("concurrency_used", "rpm_used", "tpm_reserved"):
            assert pair.group(capacity, "key-b")[field] == 0
        assert pair.receipt_hashes() == waiting_receipts
        assert pair.ledgers() == waiting_ledgers
        assert len(pair.calls()) == 1 and pair.calls(1) == []
        pair.release(0)
        assert pair.dispatched(0, active)["status"] == "passed"
        first_ready = pair.dispatched(1, pair.dispatch(1))
        second_ready = pair.dispatched(1, pair.dispatch(1))
        assert [first_ready["run_id"], second_ready["run_id"]] == [tasks[1]["run_id"], tasks[2]["run_id"]]
        assert first_ready["status"] == second_ready["status"] == "passed"
        assert len(pair.calls()) == 3
        assert len(pair.capacity()["allocations"]) == 3


def test_rate_wait_uses_no_execution_slot_and_disjoint_original_account_can_progress(queue_process_pair):
    limits = {"capacity": 8, "concurrency": 1, "per_principal": 4, "max_wait_seconds": 1800}
    with queue_process_pair(separate_budgets=True, disjoint=True, rpm=1, limits=limits) as pair:
        pair.activate()
        pair.mode("agent", "open_source_first")
        first, second = pair.client(0), pair.client(1, principal="agent")
        original = {**TASK, "run_id": "rate-complete-a"}
        assert first.post("/v1/tasks", json=original).status_code == 202
        assert pair.dispatched(0, pair.dispatch(0))["status"] == "passed"
        waiting, independent = ({**TASK, "run_id": name} for name in ("rate-waiting-a", "rate-independent-b"))
        assert first.post("/v1/tasks", json=waiting).status_code == 202
        assert second.post("/v1/tasks", json=independent).status_code == 202
        ledgers, receipts = pair.ledgers(), pair.receipt_hashes()
        before = sum(len(value["reservations"]) for value in ledgers.values())
        result = pair.dispatched(1, pair.dispatch(1))
        assert result["run_id"] == independent["run_id"] and result["status"] == "passed", result
        pending = first.get("/v1/tasks/" + waiting["run_id"]).json()
        assert pending["status"] == "queued" and pending["capacity_reason"] == "capacity_rpm_exhausted"
        assert sum(len(value["reservations"]) for value in pair.ledgers().values()) == before + 1
        assert all(pair.receipt_hashes()[path] == value for path, value in receipts.items())
        assert len(pair.calls()) == 2
        snapshot = pair.view("/monitor/queue")
        assert next(row for row in snapshot["jobs"] if row["run_id"] == waiting["run_id"])["queue_state"] == "queued"
        pair.clock.advance(60)
        result = pair.dispatched(0, pair.dispatch(0))
        assert result["run_id"] == waiting["run_id"] and result["status"] == "passed", result
        assert len(pair.calls()) == 3


@pytest.mark.parametrize("shared_budget", [False, True])
def test_disjoint_capacity_keeps_original_shared_unknown_fee_stop(queue_process_pair, shared_budget):
    with queue_process_pair(first_mode="hold", disjoint=True, shared_budget=shared_budget,
            separate_budgets=not shared_budget) as pair:
        pair.activate()
        pair.mode("agent", "open_source_first")
        first, second = pair.client(0), pair.client(1, principal="agent")
        active_task = {**TASK, "run_id": "fee-held-a"}
        ready_task = {**TASK, "run_id": "fee-disjoint-b"}
        assert first.post("/v1/tasks", json=active_task).status_code == 202
        active = pair.dispatch(0)
        pair.entered(0)
        assert second.post("/v1/tasks", json=ready_task).status_code == 202
        original = pair.ledgers()
        result = pair.dispatched(1, pair.dispatch(1))
        if shared_budget:
            assert result is None
            state = second.get("/v1/tasks/" + ready_task["run_id"]).json()
            assert state["queue_state"] == "queued" and state["reason"] == "queue_waiting_budget", state
            assert state["capacity_reason"] == "capacity_original_budget_denied"
            assert sum(event["cost"] is None for value in original.values() for event in value["events"]) == 1
            assert pair.ledgers() == original
            assert pair.calls(1) == []
            capacity = pair.capacity()
            assert len(capacity["allocations"]) == 1
            assert pair.group(capacity, "disjoint")["concurrency_used"] == 0
            assert pair.group(capacity, "disjoint")["rpm_used"] == 0
        else:
            assert result["run_id"] == ready_task["run_id"] and result["status"] == "passed", result
            assert len(pair.calls(0)) == len(pair.calls(1)) == 1
            assert first.get("/v1/tasks/" + active_task["run_id"]).json()["queue_state"] == "running"
        pair.release(0)
        assert pair.dispatched(0, active)["status"] == "passed"
        if shared_budget:
            result = pair.dispatched(1, pair.dispatch(1))
            assert result["run_id"] == ready_task["run_id"] and result["status"] == "passed", result
        accounts = pair.ledgers()
        assert sum(len(value["events"]) for value in accounts.values()) == 2
        assert sum(len(value["reservations"]) for value in accounts.values()) == 2
        assert all(event["cost"] is not None for value in accounts.values() for event in value["events"])
        assert len(pair.calls()) == 2


@pytest.mark.parametrize("mode", ["kill", "uncertain"])
def test_sent_or_uncertain_worker_never_retries_or_frees_original_occupancy(queue_process_pair, mode):
    with queue_process_pair(first_mode=mode, separate_budgets=True, disjoint=True, overlap_keys=True) as pair:
        pair.activate()
        pair.mode("agent", "open_source_first")
        task = {**TASK, "run_id": "unresolved-queue-process"}
        first = pair.client(0)
        assert first.post("/v1/tasks", json=task).status_code == 202
        command = pair.dispatch(0)
        pair.entered(0)
        requested = pair.client(1).post("/v1/tasks/" + task["run_id"] + "/cancel", json={})
        assert requested.status_code == 200, requested.text
        assert requested.json()["cancellation_requested"] is True
        assert requested.json()["provider_cancellation_confirmed"] is False
        if mode == "kill":
            pair.finish(0, kill=True)
        else:
            pair.release(0)
            assert pair.dispatched(0, command)["queue_state"] == "interrupted_pending"
            pair.finish(0)
        receipts = pair.receipt_hashes()
        assert sum(path.endswith("intent.json") for path in receipts) == 1
        assert sum(path.endswith("response.json") for path in receipts) == (0 if mode == "kill" else 1)
        original_ledger = pair.ledgers()
        assert sum(len(value["events"]) for value in original_ledger.values()) == 1
        assert sum(event["cost"] is None for value in original_ledger.values() for event in value["events"]) == 1
        pair.clock.advance(600)
        old_pid = pair.workers[0].process.pid
        assert pair.start(0).process.pid != old_pid
        first = pair.client(0)
        capacity = pair.capacity(1)
        assert len(capacity["allocations"]) == 1
        assert capacity["allocations"][0]["state"] == ("sent" if mode == "kill" else "uncertain")
        assert pair.group(capacity, "shared")["concurrency_used"] == 1
        assert pair.group(capacity, "key-a")["concurrency_used"] == 1
        for index in (0, 1):
            replay = pair.client(index).post("/v1/tasks", json=task)
            assert replay.json()["queue_state"] in {"running", "interrupted_pending"}, replay.text
            assert pair.client(index).post("/v1/tasks", json={**task, "prompt": "changed"}).status_code == 409
            assert pair.client(index).get("/v1/tasks/" + task["run_id"]).json()["provider_cancellation_confirmed"] is False
        resumed = first.post("/v1/tasks/" + task["run_id"] + "/resume", json={})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["queue_state"] in {"running", "interrupted_pending"}
        assert resumed.json()["costs"]["unknown_events"] == 1
        assert pair.receipt_hashes() == receipts and pair.ledgers() == original_ledger
        second = pair.client(1, principal="agent")
        assert second.post("/v1/tasks", json={**TASK, "run_id": "unresolved-capacity-contender"}).status_code == 202
        assert pair.dispatched(1, pair.dispatch(1)) is None
        assert pair.calls(1) == [] and len(pair.calls(0)) == 1
        assert pair.group(pair.capacity(), "shared")["concurrency_used"] == 1
        assert pair.group(pair.capacity(), "key-b")["rpm_used"] == 0
        assert pair.receipt_hashes() == receipts and pair.ledgers() == original_ledger


def test_clock_expiry_is_readonly_and_cannot_resubmit_original_identity(queue_process_pair):
    limits = {"capacity": 2, "concurrency": 1, "per_principal": 2, "max_wait_seconds": 10}
    with queue_process_pair(limits=limits) as pair:
        pair.activate()
        task = {**TASK, "run_id": "process-expired"}
        first, second = pair.client(0), pair.client(1)
        accepted = first.post("/v1/tasks", json=task)
        assert accepted.status_code == 202, accepted.text
        before, retention = pair.protected_hashes(), pair.retention_tasks()
        pair.clock.advance(10)
        for client in (first, second):
            projected = client.get("/v1/tasks/" + task["run_id"])
            assert projected.status_code == 410 and projected.json()["status"] == "expired", projected.text
            assert projected.json()["queue"]["deadline"] == accepted.json()["queue"]["deadline"]
        for index in (0, 1):
            pair.view("/monitor/queue", index)
            pair.capacity(index)
            pair.view("/monitor/snapshot", index)
        assert pair.protected_hashes() == before and pair.retention_tasks() == retention
        assert second.post("/v1/tasks", json=task).json()["status"] == "expired"
        assert first.post("/v1/tasks", json={**task, "prompt": "changed"}).status_code == 409
        assert pair.dispatched(0, pair.dispatch(0)) is None
        assert pair.calls() == [] and pair.receipt_hashes() == {}
        assert all(not value["events"] and not value["reservations"] for value in pair.ledgers().values())
