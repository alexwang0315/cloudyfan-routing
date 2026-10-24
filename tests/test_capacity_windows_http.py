"""Response-derived windows at the approved actual loopback HTTP seam."""
import hashlib
from email.utils import formatdate
import math
import httpx
import pytest
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_windows_fixture import attach_windows, window_configuration


def test_original_response_pauses_shared_group_without_probe_or_paid_retry(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    start = local_service.clock()
    def upstream(request):
        response = bundle.upstream(request)
        if len(bundle.upstream.calls) != 1:
            return response
        body = response.json()
        body["choices"] = []
        body["error"] = {"code": 429, "message": "SYNTHETIC_PRIVATE_WINDOW_ERROR"}
        return httpx.Response(429, json=body, headers={"Retry-After": "30", "X-Private-Provider": "SYNTHETIC_PRIVATE_WINDOW_HEADER"})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, windows = attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    first = client.post("/v1/tasks", json={**TASK, "run_id": "actual-rate-limited"})
    assert len(bundle.upstream.calls) == 1 and first.json()["costs"]["unknown_events"] == 0
    view = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert view["groups"][0]["status"] == "wait" and view["groups"][0]["not_before"] == start + 30
    response = client.post("/v1/tasks", json={**TASK, "run_id": "excluded-before-post"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_window_wait"
    assert response.json()["costs"]["unknown_events"] == 0 and len(bundle.upstream.calls) == 1
    original = {str(p.relative_to(bundle.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (bundle.root / "receipts").rglob("*.json")}
    capacity = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    for _ in range(2):
        assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == view
        assert client.post("/v1/tasks/excluded-before-post/resume", json={}).json() == response.json()
    assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == capacity
    assert original == {str(p.relative_to(bundle.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (bundle.root / "receipts").rglob("*.json")}
    assert "SYNTHETIC_PRIVATE_WINDOW" not in operator.get("/monitor/capacity").text
    local_service.clock.now = start + 30
    resumed = client.post("/v1/tasks", json={**TASK, "run_id": "new-after-window"})
    assert resumed.json()["status"] == "passed" and len(bundle.upstream.calls) == 2
    assert windows.snapshot()["events"][0]["not_before"] == start + 30


@pytest.mark.parametrize("name,grammar", [("retry-after", "retry_after"),
    ("x-ratelimit-reset-requests", "unix_seconds")])
def test_trusted_absolute_reset_uses_original_date_and_declared_units(local_service, name, grammar):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    start = math.ceil(local_service.clock())
    local_service.clock.now = start
    def upstream(request):
        response = bundle.upstream(request)
        body = response.json()
        body.update(choices=[], error={"code": 429, "message": "SYNTHETIC_PRIVATE_RESET"})
        return httpx.Response(429, json=body, headers={"date": formatdate(start, usegmt=True),
            name: formatdate(start + 30, usegmt=True) if grammar == "retry_after" else str(start + 30)})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_windows(bundle, local_service.clock, rules=[{"binding": "synthetic-binding", "group": "shared",
        "header": name, "format": grammar, "constraint": "requests"}])
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "absolute-reset"})
    observed = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert observed["groups"][0]["status"] == "wait" and observed["groups"][0]["not_before"] == start + 30
    assert observed["events"][0]["observed_at"] == start
    assert len(bundle.upstream.calls) == 1


def test_contradictory_saved_headers_remain_unknown_after_every_reported_time(local_service):
    bundle = local_service.bundle
    start = math.ceil(local_service.clock())
    local_service.clock.now = start
    def upstream(request):
        response = bundle.upstream(request)
        body = response.json()
        body.update(choices=[], error={"code": 429, "message": "SYNTHETIC_PRIVATE_CONTRADICTION"})
        return httpx.Response(429, json=body, headers={"date": formatdate(start, usegmt=True),
            "retry-after": "30", "x-ratelimit-reset-requests": str(start + 90)})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_windows(bundle, local_service.clock, rules=[
        {"binding": "synthetic-binding", "group": "shared", "header": name, "format": grammar, "constraint": "requests"}
        for name, grammar in [("retry-after", "retry_after"), ("x-ratelimit-reset-requests", "unix_seconds")]])
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "contradictory-original"})
    saved = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert saved["groups"][0]["status"] == "unknown"
    assert saved["groups"][0]["reason_code"] == "capacity_window_source_contradictory"
    assert saved["groups"][0]["not_before"] is None
    local_service.clock.now = start + 100
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "unknown-not-expired"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_source_contradictory"
    assert denied.json()["costs"]["unknown_events"] == 0 and len(bundle.upstream.calls) == 1
    assert operator.post("/monitor/capacity/activate", json={"revision": 1, "version": None}).status_code == 409
    assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == saved


def test_long_window_blocks_policy_disable_and_rebinding_after_ordinary_rate_horizon(local_service):
    bundle = local_service.bundle
    start = local_service.clock()
    def upstream(request):
        response = bundle.upstream(request)
        body = response.json()
        body.update(choices=[], error={"code": 429})
        return httpx.Response(429, json=body, headers={"retry-after": "3600"})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, _ = attach_windows(bundle, local_service.clock)
    controller.register("synthetic-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=controller.routes["synthetic-window-admission-v1"], max_seconds=300)
    controller.register("synthetic-window-alternate", configuration_version="synthetic-admission-config-v1",
        routes=controller.routes["synthetic-window-admission-v1"], max_seconds=300, windows_version="synthetic-windows-v1")
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "long-original-window"})
    local_service.clock.now = start + 61
    original = operator.get("/monitor/capacity").json()
    assert original["dispatch"]["groups"][0]["rpm_used"] == 0
    assert original["dispatch"]["windows"]["groups"][0]["not_before"] == start + 3600
    for version in [None, "synthetic-admission-v1", "synthetic-window-alternate"]:
        denied = operator.post("/monitor/capacity/activate", json={"revision": 1, "version": version})
        assert denied.status_code == 409 and denied.json()["error"]["code"] == "capacity_live_state_blocks_change"
    renamed = window_configuration(start)
    renamed["version"] = "synthetic-renamed-v2"
    renamed["bindings"][0]["groups"] = ["renamed-shared"]
    renamed["groups"][0]["id"] = "renamed-shared"
    with pytest.raises(ValueError, match="Live capacity prevents configuration change"):
        controller.store.configure(renamed)
    assert operator.get("/monitor/capacity").json() == original
    assert len(bundle.upstream.calls) == 1
