"""Immutable response-window explanations through the real operator HTTP surface."""
import hashlib
import json

from capacity_windows_fixture import backup_windows_http as backup_windows_http
from test_local_http_integration import loopback_network as loopback_network


def submit(fixture, run):
    response = fixture.client.post("/v1/tasks", json={"run_id": run,
        "category": fixture.host.category, "prompt": "Return only 1+1. PRIVATE_WINDOW_TASK"})
    assert response.status_code == 200, response.json()
    return response.json()


def history(fixture, run):
    snapshot = fixture.operator.get("/monitor/snapshot")
    assert snapshot.status_code == 200
    task = next(task for task in snapshot.json()["tasks"] if task["run"] == run)
    return task, task["operator_observation"]["task_decision"]


def test_saved_window_exclusion_is_distinct_from_original_availability_fallback(backup_windows_http):
    fixture = backup_windows_http
    start = fixture.clock()
    assert fixture.operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    fixture.flags["scenario"] = "window"
    assert submit(fixture, "original-availability")["status"] == "passed"
    first_task, first = history(fixture, "original-availability")
    assert [a["transition"] for a in first["attempts"]] == ["initial", "availability_fallback"]
    assert first["stages"][1]["trigger"] == "availability_failure"
    assert first_task["generation_attempts"] == 2 and first_task["classifier_calls"] == 0
    assert first["costs"]["complete_api_usd"] == "0.02" and len(fixture.requests) == 2
    assert submit(fixture, "excluded-window-candidate")["status"] == "passed"
    second_task, second = history(fixture, "excluded-window-candidate")
    skipped = second["stages"][0]["candidates"][0]
    assert skipped["state"] == "excluded_before_dispatch" and "capacity_window_wait" in skipped["reasons"]
    assert skipped["capacity_window"]["status"] == "wait"
    assert skipped["capacity_window"]["not_before"] == start + 30
    assert len(skipped["capacity_window"]["evidence_refs"]) == 1
    assert second_task["generation_attempts"] == 1 and second_task["classifier_calls"] == 0
    assert len(fixture.requests) == 3 and fixture.side_requests == {"source": [], "accounting": []}
    original = json.dumps(second, sort_keys=True)
    capacity = hashlib.sha256(fixture.controller.store.path.read_bytes()).hexdigest()
    health_bytes = fixture.host.health.path.read_bytes()
    costs = fixture.host.budget.report()
    fixture.clock.now += 31
    for _ in range(2):
        assert json.dumps(history(fixture, "excluded-window-candidate")[1], sort_keys=True) == original
    assert hashlib.sha256(fixture.controller.store.path.read_bytes()).hexdigest() == capacity
    assert fixture.host.health.path.read_bytes() == health_bytes and fixture.host.budget.report() == costs
    assert len(fixture.requests) == 3 and fixture.side_requests == {"source": [], "accounting": []}
    assert "PRIVATE_WINDOW" not in fixture.operator.get("/monitor/snapshot").text


def test_unknown_original_fee_stops_without_accounting_get_or_fallback(backup_windows_http):
    import httpx
    fixture = backup_windows_http
    methods = []
    seen = set()
    for contract in fixture.host.service.contracts.values():
        transport = contract.runner.client.http._transport
        if id(transport) in seen:
            continue
        seen.add(id(transport))
        upstream = transport.handler
        def record(request, original=upstream):
            methods.append(request.method)
            if request.method != "POST":
                return httpx.Response(503, json={"error": "PRIVATE_UNEXPECTED_ACCOUNTING_GET"})
            return original(request)
        transport.handler = record
    assert fixture.operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    fixture.flags["scenario"] = "unknown_fee"
    response = fixture.client.post("/v1/tasks", json={"run_id": "unknown-original-fee",
        "category": fixture.host.category, "prompt": "Return only 1+1."})
    assert response.json()["status"] == "accounting_interrupted"
    assert methods == ["POST"], "An unknown original fee must stop rather than probe/reconcile or dispatch fallback"
    assert len(fixture.requests) == 1
    before = len(methods)
    fixture.operator.get("/monitor/capacity").raise_for_status()
    fixture.operator.get("/monitor/snapshot").raise_for_status()
    fixture.client.post("/v1/tasks/unknown-original-fee/resume", json={})
    assert len(methods) == before


def test_window_expiry_cannot_heal_an_open_health_circuit_or_change_old_choices(backup_windows_http):
    fixture = backup_windows_http
    assert fixture.operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    for index in range(2):
        fixture.flags["scenario"] = "window"
        assert submit(fixture, f"original-window-failure-{index}")["status"] == "passed"
        if index == 0:
            fixture.clock.now += 30
    old = json.dumps(history(fixture, "original-window-failure-0")[1], sort_keys=True)
    assert len(fixture.requests) == 4
    fixture.clock.now += 60
    windows = fixture.operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert windows["groups"] == [] and windows["event_count"] == 2
    pool = fixture.operator.get("/monitor/pools", params={"id": fixture.host.pools[0],
        "category": fixture.host.category, "mode": "cloudyfan"}).json()
    candidate = pool["operator_observation"]["candidates"]["tiers"]["open_source"][0]
    assert candidate["health"]["status"] == "recovery_required"
    assert candidate["health"]["circuit"]["open"] is True
    health_before = fixture.host.health.path.read_bytes()
    assert json.dumps(history(fixture, "original-window-failure-0")[1], sort_keys=True) == old
    assert fixture.host.health.path.read_bytes() == health_before
    assert submit(fixture, "window-ended-still-health-gated")["status"] == "passed"
    task, saved = history(fixture, "window-ended-still-health-gated")
    skipped = saved["stages"][0]["candidates"][0]
    assert skipped["capacity_window"]["status"] == "open" and "recovery_required" in skipped["reasons"]
    assert skipped["state"] == "excluded_before_dispatch"
    assert task["generation_attempts"] == 1 and len(fixture.requests) == 5
    assert fixture.requests[-1]["model"] == "example/open_source-1"
    assert fixture.side_requests == {"source": [], "accounting": []}
