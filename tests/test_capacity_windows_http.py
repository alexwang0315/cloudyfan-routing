"""Response-derived windows at the approved actual loopback HTTP seam."""
import hashlib
import httpx
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_windows_fixture import attach_windows


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
