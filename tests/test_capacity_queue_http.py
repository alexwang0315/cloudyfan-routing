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


def test_worker_claims_original_exact_request_before_execution_and_duplicate_is_read_only(local_service):
    from capacity_queue_fixture import attach_queue
    import hashlib
    import json
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    queue = attach_queue(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-queued-admission-v1"})
    payload = {**TASK, "run_id": "queued-completed"}
    assert client.post("/v1/tasks", json=payload).status_code == 202
    # A direct service execution is a lookup, never a queue bypass.
    assert queue.service.execute("studio", payload["run_id"])["status"] == "queued"
    assert bundle.upstream.calls == []
    completed = queue.dispatch_next()
    assert completed["status"] == "passed"
    assert len(bundle.upstream.calls) == 1
    saved = next((bundle.root / "receipts/calls").glob("*/intent.json"))
    intent = json.loads(saved.read_text())
    assert intent["dispatch"]["capacity_allocation"]
    allocation = operator.get("/monitor/capacity").json()["dispatch"]["allocations"][0]
    assert allocation["state"] == "complete"
    assert allocation["wire_sha256"] == intent["dispatch"]["wire_sha256"]
    before = hashlib.sha256(saved.read_bytes()).hexdigest()
    assert client.post("/v1/tasks", json=payload).json()["status"] == "passed"
    assert client.post("/v1/tasks/queued-completed/resume", json={}).json()["status"] == "passed"
    assert queue.dispatch_next() is None
    assert len(bundle.upstream.calls) == 1 and hashlib.sha256(saved.read_bytes()).hexdigest() == before
    assert client.get("/v1/tasks/queued-completed/result").json()["answer"] == "2"


def test_sequential_waits_before_next_intent_preserving_original_reserve_and_global_counts(local_service):
    from capacity_queue_fixture import attach_queue
    import hashlib
    bundle = local_service.bundle
    queue = attach_queue(bundle, local_service.clock, rpm=1)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-queued-admission-v1"})
    payload = {**TASK, "run_id": "queued-sequential"}
    assert client.post("/v1/tasks", json=payload).status_code == 202
    first = queue.dispatch_next()
    assert first["status"] == "queued" and len(bundle.upstream.calls) == 1
    assert list((bundle.root / "receipts/calls").glob("*/intent.json"))[0].parent.name.endswith("-a0-t0")
    assert queue.dispatch_next() is None
    waiting = client.get("/v1/tasks/queued-sequential").json()
    assert waiting["reason"] == "queue_waiting_capacity" and waiting["capacity_reason"] == "capacity_rpm_exhausted"
    report = queue.service.contracts["demo-cloudyfan"].runner.store.report()
    assert len(report["reservations"]) == 1 and len(report["events"]) == 1 and report["unknown_events"] == 0
    files = [queue.path, bundle.root / "capacity.sqlite", bundle.root / "budget.sqlite",
             *[path for path in (bundle.root / "receipts").rglob("*") if path.is_file()]]
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    for _ in range(3):
        client.get("/v1/tasks/queued-sequential")
        operator.get("/monitor/queue")
        client.post("/v1/tasks", json=payload)
        client.post("/v1/tasks/queued-sequential/resume", json={})
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    assert len(bundle.upstream.calls) == 1
    # Queue's original120s deadline, not a new per-call timeout, stays fixed.
    local_service.clock.now += 61
    second = queue.dispatch_next()
    assert second["status"] == "queued" and len(bundle.upstream.calls) == 2
    assert "questions" in bundle.upstream.calls[-1]
    assert second["queue"]["deadline"] == first["queue"]["deadline"]
    assert queue.dispatch_next() is None
    local_service.clock.now += 60
    assert client.get("/v1/tasks/queued-sequential").json()["status"] == "expired"
    assert queue.dispatch_next() is None and len(bundle.upstream.calls) == 2


def test_shared_host_reopen_retains_original_identity_configuration_and_next_request(local_service):
    from capacity_queue_fixture import build_queue_host
    host = build_queue_host(local_service.bundle.root / 'reopened', clock=local_service.clock,
                            origin=local_service.origin, options={'separate_budgets': True})
    local_service.bundle = host.bundle
    local_service.server.set_app(host.app)
    operator, client = local_service.client('operator'), local_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'reopened-queue'}).status_code == 202
    original = client.get('/v1/tasks/reopened-queue').json()
    local_service.clock.now += 10
    reopened = build_queue_host(host.bundle.root, clock=local_service.clock,
                                origin=local_service.origin, options={'separate_budgets': True})
    try:
        assert reopened.credential == host.credential
        assert reopened.queue.path == host.queue.path and reopened.queue.service.root == host.queue.service.root
        local_service.bundle = reopened.bundle
        local_service.server.set_app(reopened.app)
        assert client.get('/v1/tasks/reopened-queue').json()['queue']['accepted_at'] == original['queue']['accepted_at']
        assert reopened.queue.dispatch_next()['status'] == 'passed'
        assert len(reopened.upstream.calls) == 1 and len(host.upstream.calls) == 0
        assert client.get('/v1/tasks/reopened-queue/result').json()['answer'] == '2'
    finally:
        host.close()
        reopened.close()


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
