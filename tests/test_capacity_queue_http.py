"""Versioned bounded capacity queue at the agreed real HTTP seam."""
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_admission_fixture import attach_admission
from capacity_queue_fixture import attach_window_protocol, WindowProtocolFixture


def test_queued_acceptance_is_202_idempotent_cancellable_and_never_starts_paid_work(local_service):
    from capacity_queue_fixture import attach_queue
    bundle = local_service.bundle
    attach_queue(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-queued-admission-v1"}).status_code == 200
    payload = {**TASK, "run_id": "queued-cancelled"}
    accepted = client.post("/v1/tasks", json=payload)
    assert accepted.status_code == 202, accepted.text
    view = accepted.json()
    assert view["status"] == "queued" and view["reason"] == "queue_waiting_worker"
    assert view["queue"]["protocol"] == "capacity-queue-v1"
    assert view["queue"]["limits"] == {"capacity": 8, "concurrency": 2, "per_principal": 4, "max_wait_seconds": 120}
    assert client.post("/v1/tasks", json=payload).status_code == 202
    assert client.post("/v1/tasks", json={**payload, "prompt": "changed"}).status_code == 409
    assert client.post("/v1/tasks/queued-cancelled/cancel", json={}).json()["status"] == "cancelled"
    assert client.post("/v1/tasks", json=payload).json()["status"] == "cancelled"
    snapshot = operator.get("/monitor/queue").json()
    assert snapshot["jobs"][0]["status"] == "cancelled"
    assert local_service.client("agent").get("/v1/tasks/queued-cancelled").status_code == 404
    assert bundle.upstream.calls == []
    assert list((bundle.root / "receipts/calls").glob("*/intent.json")) == []
    ledger = operator.get("/monitor/snapshot").json()["ledger"]
    assert ledger["rows"] == [] and ledger["reserved_usd"] == "0"


def test_window_protocol_is_explicitly_not_applicable_on_original_immediate_policy(local_service):
    bundle = local_service.bundle
    attach_admission(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    response = local_service.client("operator").get("/monitor/capacity")
    assert response.status_code == 200
    assert response.json()["dispatch"]["windows"] == {
        "configured": False, "protocol": "capacity-windows-v1", "status": "not_applicable"}


def test_enabled_window_wait_and_missing_backend_refuse_before_paid_intent(local_service):
    bundle = local_service.bundle
    previous = attach_admission(bundle, local_service.clock, rpm=10)
    backend = WindowProtocolFixture(local_service.clock, status="wait")
    controller = attach_window_protocol(bundle, previous, backend)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-enabled-v2"}).status_code == 200
    waiting = client.post("/v1/tasks", json={**TASK, "run_id": "window-spi-wait"})
    assert waiting.status_code == 429 and waiting.json()["error"]["code"] == "capacity_window_wait"
    assert waiting.json()["costs"]["unknown_events"] == 0
    controller.window_backend = None
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "window-spi-missing"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_backend_unavailable"
    assert bundle.upstream.calls == []
    assert list((bundle.root / "receipts/calls").glob("*/intent.json")) == []
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocation_count"] == 0
    assert view["windows"]["status"] == "unknown"


def test_enabled_window_original_header_envelope_is_bounded_and_follows_durable_response(local_service):
    import httpx
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    previous = attach_admission(bundle, local_service.clock, rpm=10)
    backend = WindowProtocolFixture(local_service.clock)
    def advancing_clock():
        local_service.clock.now += .001
        return local_service.clock()
    previous.store.clock = advancing_clock
    attach_window_protocol(bundle, previous, backend)
    for contract in bundle.app.gateway.service.contracts.values():
        def response(request):
            original = bundle.upstream(request)
            return httpx.Response(original.status_code, json=original.json(),
                headers=[("retry-after", "30"), ("Retry-After", "40"), ("date", "Wed, 21 Oct 2026 00:00:00 GMT")])
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(response), trust_env=False)
    # Credentials and http transport are pinned at registration; re-register the
    # same already controlled transport objects before task acceptance.
    controller = bundle.app.capacity_controller
    controller.register("synthetic-window-enabled-v2", configuration_version="synthetic-admission-config-v1",
        routes=previous.routes["synthetic-admission-v1"], max_seconds=300, windows_version=backend.version)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-enabled-v2"}).status_code == 200
    completed = client.post("/v1/tasks", json={**TASK, "run_id": "window-spi-receipt"})
    assert completed.status_code == 200 and completed.json()["status"] == "passed", completed.text
    assert len(bundle.upstream.calls) == len(backend.responses) == 1
    observation = backend.responses[0]
    headers = observation["response"]["window_headers"]
    assert headers["protocol"] == "capacity-window-headers-v1" and headers["invalid"] is True
    assert len(headers["values"]) <= 8 and all(len(value) <= 160 for name, value in headers["values"])
    assert observation["scope"]["wire_sha256"] is not None
    assert observation["scope"]["physical_groups"][0]["group"] == "shared"
