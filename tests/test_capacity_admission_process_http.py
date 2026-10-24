"""Shared capacity acceptance over real HTTP in independent spawned processes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

import httpx
import pytest

from capacity_admission_process_fixture import process_pair as process_pair
from test_local_http_integration import loopback_network as loopback_network, TASK


def test_multikey_processes_share_capacity_and_all_group_refusal_is_atomic(process_pair):
    with process_pair() as pair:
        pair.activate()
        first, second = pair.client(0, timeout=20), pair.client(1)
        with ThreadPoolExecutor(1) as requests:
            pending = requests.submit(first.post, "/v1/tasks", json={**TASK, "run_id": "held-a"})
            try:
                entered = pair.entered(0)
                assert entered["pid"] != pair.workers[1].process.pid
                denied = second.post("/v1/tasks", json={**TASK, "run_id": "contending-b"})
                assert denied.status_code == 429, denied.text
                assert denied.json()["error"]["code"] == "capacity_concurrency_exhausted"
                assert denied.json()["costs"]["unknown_events"] == 0
                capacity = pair.capacity()
                assert len(capacity["allocations"]) == 1
                assert pair.group(capacity, "account-shared")["concurrency_used"] == 1
                assert pair.group(capacity, "key-b")["concurrency_used"] == 0
                assert pair.group(capacity, "key-b")["rpm_used"] == 0
                assert pair.group(capacity, "key-b")["tpm_reserved"] == 0
                assert pair.calls(1) == []
            finally:
                pair.release(0)
            completed = pending.result(timeout=10)
            assert completed.status_code == 200 and completed.json()["status"] == "passed", completed.text
        assert len(pair.calls(0)) == 1 and pair.calls(1) == []
        capacity = pair.capacity()
        assert pair.group(capacity, "account-shared")["concurrency_used"] == 0
        assert pair.group(capacity, "account-shared")["rpm_used"] == 1
        assert pair.group(capacity, "account-shared")["tpm_reserved"] == 21


def test_disjoint_groups_can_complete_while_other_process_holds_its_own_fee_account(process_pair):
    with process_pair(disjoint=True) as pair:
        pair.activate()
        first, second = pair.client(0, timeout=20), pair.client(1)
        with ThreadPoolExecutor(1) as requests:
            pending = requests.submit(first.post, "/v1/tasks", json={**TASK, "run_id": "held-independent-a"})
            try:
                pair.entered(0)
                completed = second.post("/v1/tasks", json={**TASK, "run_id": "eligible-independent-b"})
                assert completed.status_code == 200 and completed.json()["status"] == "passed", completed.text
                assert not pending.done()
                capacity = pair.capacity()
                assert len(capacity["allocations"]) == 2
                assert pair.group(capacity, "account-a")["concurrency_used"] == 1
                assert pair.group(capacity, "account-b")["concurrency_used"] == 0
                assert len(pair.calls(0)) == len(pair.calls(1)) == 1
                assert {item["wire_sha256"] for item in capacity["allocations"]} == {
                    pair.calls(0)[0]["wire_sha256"], pair.calls(1)[0]["wire_sha256"]}
            finally:
                pair.release(0)
            assert pending.result(timeout=10).json()["status"] == "passed"


def test_shared_unknown_fee_still_blocks_other_process_with_disjoint_capacity(process_pair):
    with process_pair(disjoint=True, shared_budget=True) as pair:
        pair.activate()
        first, second = pair.client(0, timeout=20), pair.client(1)
        with ThreadPoolExecutor(1) as requests:
            pending = requests.submit(first.post, "/v1/tasks", json={**TASK, "run_id": "shared-fee-held-a"})
            try:
                pair.entered(0)
                denied = second.post("/v1/tasks", json={**TASK, "run_id": "shared-fee-blocked-b"})
                assert denied.status_code == 409 and denied.json()["status"] == "accounting_interrupted", denied.text
                assert denied.json()["error"]["code"] == "unknown_cost"
                assert len(pair.capacity()["allocations"]) == 1
                assert pair.calls(1) == []
                ledger = pair.snapshot()["ledger"]
                assert ledger["unknown_events"] == 1
                assert len(ledger["accounts"]) == 1
                assert ledger["accounts"][0]["dispatch_blocked"] is True
            finally:
                pair.release(0)
            assert pending.result(timeout=10).json()["status"] == "passed"
        eligible = second.post("/v1/tasks", json={**TASK, "run_id": "shared-fee-resolved-b"})
        assert eligible.status_code == 200 and eligible.json()["status"] == "passed", eligible.text
        assert len(pair.calls(0)) == len(pair.calls(1)) == 1
        assert pair.snapshot()["ledger"]["unknown_events"] == 0


def test_fee_denial_before_physical_post_releases_all_capacity_and_keeps_zero_receipt(process_pair):
    with process_pair(first_mode="deny") as pair:
        pair.activate()
        denied = pair.client(0).post("/v1/tasks", json={**TASK, "run_id": "withdrawn-fee-a"})
        assert denied.status_code == 429 and denied.json()["status"] == "capacity_denied", denied.text
        assert denied.json()["error"]["code"] == "capacity_original_budget_denied"
        assert denied.json()["costs"]["unknown_events"] == 0
        assert denied.json()["costs"]["known_api_usd"] == "0"
        assert pair.calls(0) == []
        capacity = pair.capacity()
        assert len(capacity["allocations"]) == 1 and capacity["allocations"][0]["state"] == "released"
        for identifier in ("account-shared", "key-a"):
            assert pair.group(capacity, identifier)["concurrency_used"] == 0
            assert pair.group(capacity, identifier)["rpm_used"] == 0
            assert pair.group(capacity, identifier)["tpm_reserved"] == 0
        ledger = pair.snapshot()["ledger"]
        assert ledger["unknown_events"] == 0 and len(ledger["rows"]) == 1
        assert ledger["rows"][0]["cost_source"] == "not_dispatched"
        accepted = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "released-capacity-b"})
        assert accepted.status_code == 200 and accepted.json()["status"] == "passed", accepted.text
        assert pair.calls(0) == [] and len(pair.calls(1)) == 1


@pytest.mark.parametrize("first_mode", ["prepared", "hold"])
def test_prepared_or_held_request_retains_rate_capacity_until_full_completed_window(process_pair, first_mode):
    with process_pair(first_mode=first_mode, rpm=1, concurrency=2) as pair:
        pair.activate()
        first, second = pair.client(0, timeout=20), pair.client(1)
        with ThreadPoolExecutor(1) as requests:
            pending = requests.submit(first.post, "/v1/tasks", json={**TASK, "run_id": "window-held-a"})
            try:
                entered = pair.entered(0)
                assert entered["phase"] == ("fee_guard_after_prepare_before_physical_post"
                    if first_mode == "prepared" else "physical_mock_post_before_response")
                assert len(pair.calls(0)) == (0 if first_mode == "prepared" else 1)
                capacity = pair.capacity()
                assert capacity["allocations"][0]["state"] == ("prepared" if first_mode == "prepared" else "sent")
                pair.clock.advance(61)
                denied = second.post("/v1/tasks", json={**TASK, "run_id": "window-contending-b"})
                assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_rpm_exhausted", denied.text
                assert denied.json()["costs"]["unknown_events"] == 0
                capacity = pair.capacity()
                assert len(capacity["allocations"]) == 1
                assert pair.group(capacity, "account-shared")["rpm_used"] == 1
                assert pair.group(capacity, "account-shared")["tpm_reserved"] == 1000
                assert pair.group(capacity, "key-b")["rpm_used"] == 0
                assert pair.group(capacity, "key-b")["tpm_reserved"] == 0
                assert pair.group(capacity, "key-b")["concurrency_used"] == 0
                assert pair.calls(1) == []
            finally:
                pair.release(0)
            completed = pending.result(timeout=10)
            assert completed.status_code == 200 and completed.json()["status"] == "passed", completed.text
        assert len(pair.calls(0)) == 1
        capacity = pair.capacity()
        assert pair.group(capacity, "account-shared")["concurrency_used"] == 0
        assert pair.group(capacity, "account-shared")["rpm_used"] == 1
        assert pair.group(capacity, "account-shared")["tpm_reserved"] == 21
        pair.clock.advance(59)
        still_denied = second.post("/v1/tasks", json={**TASK, "run_id": "window-before-reset-b"})
        assert still_denied.status_code == 429 and still_denied.json()["error"]["code"] == "capacity_rpm_exhausted", still_denied.text
        assert len(pair.capacity()["allocations"]) == 1 and pair.calls(1) == []
        pair.clock.advance(1)
        accepted = second.post("/v1/tasks", json={**TASK, "run_id": "window-after-reset-b"})
        assert accepted.status_code == 200 and accepted.json()["status"] == "passed", accepted.text
        assert len(pair.calls(1)) == 1
        assert len(pair.capacity()["allocations"]) == 2


@pytest.mark.parametrize("first_mode", ["kill", "uncertain"])
def test_dead_or_uncertain_process_retains_concurrency_after_windows_and_never_resends(process_pair, first_mode):
    task = {**TASK, "run_id": "unresolved-process-a"}
    with process_pair(first_mode=first_mode) as pair:
        pair.activate()
        original_pid = pair.workers[0].process.pid
        first = pair.client(0, timeout=20)
        with ThreadPoolExecutor(1) as requests:
            pending = requests.submit(first.post, "/v1/tasks", json=task)
            pair.entered(0)
            if first_mode == "kill":
                pair.finish(0, kill=True)
                with pytest.raises(httpx.TransportError):
                    pending.result(timeout=10)
            else:
                pair.release(0)
                unresolved = pending.result(timeout=10)
                assert unresolved.status_code == 409 and unresolved.json()["status"] == "accounting_interrupted", unresolved.text
                pair.finish(0)
        original = pair.receipt_hashes(0)
        assert any(path.endswith("intent.json") for path in original)
        if first_mode == "kill":
            assert not any(path.endswith("response.json") for path in original)
        pair.clock.advance(600)
        replacement = pair.start(0, "normal")
        assert replacement.process.pid != original_pid
        capacity = pair.capacity()
        assert len(capacity["allocations"]) == 1
        assert capacity["allocations"][0]["state"] == ("sent" if first_mode == "kill" else "uncertain")
        assert pair.group(capacity, "account-shared")["rpm_used"] == 1
        assert pair.group(capacity, "account-shared")["tpm_reserved"] == 1000
        assert pair.group(capacity, "account-shared")["concurrency_used"] == 1
        assert capacity["allocations"][0]["wire_sha256"] == pair.calls(0)[0]["wire_sha256"]
        client = pair.client(0)
        capacity_hash = hashlib.sha256((pair.root / "capacity.sqlite").read_bytes()).hexdigest()
        status = client.get("/v1/tasks/" + task["run_id"])
        repeated = client.post("/v1/tasks", json=task)
        assert status.status_code == repeated.status_code == (200 if first_mode == "kill" else 409)
        assert repeated.json()["status"] == ("running" if first_mode == "kill" else "accounting_interrupted")
        assert client.post("/v1/tasks", json={**task, "prompt": "changed"}).status_code == 409
        assert pair.capacity() == capacity
        assert hashlib.sha256((pair.root / "capacity.sqlite").read_bytes()).hexdigest() == capacity_hash
        assert pair.receipt_hashes(0) == original
        assert len(pair.calls(0)) == 1
        blocked = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "still-shared-b"})
        assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "capacity_concurrency_exhausted", blocked.text
        assert blocked.json()["costs"]["unknown_events"] == 0
        assert pair.calls(1) == []
        resumed = client.post("/v1/tasks/" + task["run_id"] + "/resume", json={})
        assert resumed.status_code == 409 and resumed.json()["status"] == "accounting_interrupted", resumed.text
        assert pair.receipt_hashes(0) == original
        assert len(pair.calls(0)) == 1
        assert pair.snapshot()["ledger"]["unknown_events"] == 1
