"""Response windows at the approved independent-process real HTTP seam."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from capacity_windows_process_fixture import window_process_pair as window_process_pair
from test_local_http_integration import loopback_network as loopback_network, TASK


def test_original_http_date_window_is_shared_between_keys_until_exact_expiry(window_process_pair):
    with window_process_pair(first_mode="rate-date") as pair:
        pair.activate()
        start = pair.clock()
        first = pair.client(0).post("/v1/tasks", json={**TASK, "run_id": "window-source-a"})
        assert len(pair.calls(0)) == 1 and first.json()["costs"]["unknown_events"] == 0
        view = pair.capacity(1)["windows"]
        assert view["groups"][0]["status"] == "wait", view
        assert view["groups"][0]["group"] == "account-shared"
        assert view["groups"][0]["not_before"] == int(start) + 30
        denied = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "window-peer-b"})
        assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_wait", denied.text
        assert denied.json()["costs"]["unknown_events"] == 0 and pair.calls(1) == []
        pair.clock.advance(int(start) + 30 - pair.clock() - .25)
        before = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "window-boundary-before-b"})
        assert before.status_code == 429 and before.json()["error"]["code"] == "capacity_window_wait", before.text
        pair.clock.advance(.25)
        after = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "window-boundary-after-b"})
        assert after.status_code == 200 and after.json()["status"] == "passed", after.text
        assert len(pair.calls(0)) == len(pair.calls(1)) == 1
        assert pair.capacity()["windows"]["groups"] == []
        assert pair.capacity()["windows"]["events"][0] == view["events"][0]


def test_delta_window_survives_reopen_and_read_only_queries_add_no_requests(window_process_pair):
    with window_process_pair() as pair:
        pair.activate()
        start = pair.clock()
        original = pair.client(0).post("/v1/tasks", json={**TASK, "run_id": "persistent-window-a"})
        assert original.json()["costs"]["unknown_events"] == 0 and len(pair.calls(0)) == 1
        view = pair.capacity(1)["windows"]
        assert view["groups"][0]["not_before"] == start + 30
        assert view["groups"][0]["scope"] == {"kind": "account", "ref": "synthetic-process-account-shared"}
        task = {**TASK, "run_id": "persistent-peer-b"}
        denied = pair.client(1).post("/v1/tasks", json=task)
        assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_wait", denied.text
        pid = pair.workers[1].process.pid
        pair.finish(1)
        assert pair.start(1, "normal").process.pid != pid
        assert pair.capacity(1)["windows"] == view
        unchanged = pair.durable_hashes()
        client = pair.client(1)
        for _ in range(2):
            assert pair.capacity(0)["windows"] == pair.capacity(1)["windows"] == view
            assert client.get("/v1/tasks/" + task["run_id"]).json() == denied.json()
            assert client.post("/v1/tasks", json=task).json() == denied.json()
            assert client.post("/v1/tasks/" + task["run_id"] + "/resume", json={}).json() == denied.json()
            assert "SYNTHETIC_PRIVATE_PROCESS_WINDOW" not in str(pair.snapshot(1))
        assert pair.durable_hashes() == unchanged
        assert len(pair.calls(0)) == 1 and pair.calls(1) == []
        assert client.get("/v1/tasks/persistent-window-a").status_code == 404


def test_new_window_stops_already_prepared_peer_before_send_and_releases_its_groups(window_process_pair):
    with window_process_pair(second_mode="prepared") as pair:
        pair.activate()
        peer = pair.client(1, timeout=20)
        with ThreadPoolExecutor(1) as requests:
            pending = requests.submit(peer.post, "/v1/tasks", json={**TASK, "run_id": "prepared-window-peer-b"})
            try:
                entered = pair.entered(1)
                assert entered["phase"] == "fee_guard_after_prepare_before_physical_post"
                assert pair.calls(1) == []
                prepared = pair.capacity(0)
                assert len(prepared["allocations"]) == 1 and prepared["allocations"][0]["state"] == "prepared"
                assert pair.group(prepared, "key-b")["concurrency_used"] == 1
                first = pair.client(0).post("/v1/tasks", json={**TASK, "run_id": "published-while-peer-prepared-a"})
                assert len(pair.calls(0)) == 1 and first.json()["costs"]["unknown_events"] == 0
                assert pair.capacity(0)["windows"]["groups"][0]["status"] == "wait"
                assert not pending.done() and pair.calls(1) == []
            finally:
                pair.release(1)
            refused = pending.result(timeout=10)
        assert refused.status_code == 429 and refused.json()["error"]["code"] == "capacity_window_wait", refused.text
        assert refused.json()["costs"]["known_api_usd"] == "0"
        assert refused.json()["costs"]["unknown_events"] == 0
        capacity = pair.capacity(1)
        assert sorted(row["state"] for row in capacity["allocations"]) == ["complete", "released"]
        assert pair.group(capacity, "key-b")["rpm_used"] == 0
        assert pair.group(capacity, "key-b")["tpm_reserved"] == 0
        assert pair.group(capacity, "key-b")["concurrency_used"] == 0
        assert pair.group(capacity, "account-shared")["rpm_used"] == 1
        assert pair.group(capacity, "account-shared")["tpm_reserved"] == 21
        assert pair.group(capacity, "account-shared")["concurrency_used"] == 0
        ledger = pair.snapshot(1)["ledger"]
        assert ledger["unknown_events"] == 0 and len(ledger["rows"]) == 1
        assert ledger["rows"][0]["cost_source"] == "not_dispatched"
        assert len(pair.calls(0)) == 1 and pair.calls(1) == []
        assert any(path.endswith("response.json") for path in pair.receipt_hashes(1))
        assert peer.post("/v1/tasks/prepared-window-peer-b/resume", json={}).json() == refused.json()
        assert pair.calls(1) == []


@pytest.mark.parametrize("mode,status,reason,elapsed", [
    ("rate", "wait", "capacity_window_wait", 10),
    ("invalid", "unknown", "capacity_window_clock_reference_unknown", 600)])
def test_window_stops_only_affected_scope_across_time_and_restart(window_process_pair, mode, status, reason, elapsed):
    with window_process_pair(first_mode=mode, disjoint=True) as pair:
        pair.activate()
        start = pair.clock()
        first = pair.client(0).post("/v1/tasks", json={**TASK, "run_id": "invalid-window-source-a"})
        assert first.json()["costs"]["unknown_events"] == 0 and len(pair.calls(0)) == 1
        view = pair.capacity(1)["windows"]
        assert len(view["groups"]) == 1
        assert view["groups"][0]["group"] == "account-a"
        assert view["groups"][0]["status"] == status
        assert view["groups"][0]["not_before"] == (start + 30 if mode == "rate" else None)
        assert view["groups"][0]["reason_code"] == reason
        eligible = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "disjoint-eligible-b"})
        assert eligible.status_code == 200 and eligible.json()["status"] == "passed", eligible.text
        pair.clock.advance(elapsed)
        pid = pair.workers[0].process.pid
        pair.finish(0)
        assert pair.start(0, "normal").process.pid != pid
        assert pair.capacity(0)["windows"] == pair.capacity(1)["windows"] == view
        denied = pair.client(0).post("/v1/tasks", json={**TASK, "run_id": "invalid-still-blocked-a"})
        assert denied.status_code == 429 and denied.json()["error"]["code"] == reason, denied.text
        assert denied.json()["costs"]["unknown_events"] == 0
        assert denied.json()["costs"]["known_api_usd"] == "0"
        still_eligible = pair.client(1).post("/v1/tasks", json={**TASK, "run_id": "disjoint-eligible-after-reopen-b"})
        assert still_eligible.status_code == 200 and still_eligible.json()["status"] == "passed", still_eligible.text
        assert len(pair.calls(0)) == 1 and len(pair.calls(1)) == 2
        unchanged = pair.durable_hashes()
        assert pair.capacity(0)["windows"] == pair.capacity(1)["windows"] == view
        assert pair.durable_hashes() == unchanged
        assert "SYNTHETIC_PRIVATE_PROCESS_WINDOW" not in str(view)
