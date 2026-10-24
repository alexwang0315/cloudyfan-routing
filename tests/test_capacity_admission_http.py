"""Immediate controlled admission through the agreed actual HTTP seam."""
import hashlib
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_admission_fixture import attach_admission


def test_operator_optin_atomically_refuses_second_request_without_paid_unknown_and_replay_is_readonly(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    controller = attach_admission(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    before = operator.get("/monitor/capacity").json()["dispatch"]
    assert before["active_version"] is None and before["revision"] == 0
    enabled = operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"})
    assert enabled.status_code == 200 and enabled.json()["active_version"] == "synthetic-admission-v1"
    first = client.post("/v1/tasks", json={**TASK, "run_id": "admitted-one"})
    assert first.status_code == 200 and first.json()["status"] == "passed"
    second = client.post("/v1/tasks", json={**TASK, "run_id": "denied-two"})
    assert second.status_code == 429 and second.json()["status"] == "capacity_denied"
    assert second.json()["error"]["code"] == "capacity_rpm_exhausted"
    assert second.json()["costs"]["unknown_events"] == 0
    assert len(bundle.upstream.calls) == 1
    dispatch = operator.get("/monitor/capacity").json()["dispatch"]
    assert dispatch["groups"][0]["rpm_used"] == 1
    assert dispatch["groups"][0]["concurrency_used"] == 0
    assert len(dispatch["allocations"]) == 1
    capacity_hash = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    for _ in range(2):
        assert client.post("/v1/tasks", json={**TASK, "run_id": "denied-two"}).status_code == 429
        assert client.post("/v1/tasks/denied-two/resume", json={}).json() == second.json()
        assert client.get("/v1/tasks/denied-two").json() == second.json()
        assert operator.get("/monitor/capacity").json()["dispatch"] == dispatch
    assert client.post("/v1/tasks", json={**TASK, "run_id": "denied-two", "prompt": "changed"}).status_code == 409
    assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == capacity_hash
    assert len(bundle.upstream.calls) == 1
    assert operator.post("/monitor/capacity/activate", json={"revision": 1, "version": None}).status_code == 409
    assert local_service.client("agent").get("/v1/tasks/denied-two").status_code == 404
    assert len(operator.get("/monitor/snapshot").json()["ledger"]["rows"]) == 1


def test_tpm_requires_exact_versioned_wire_proof_and_keeps_reservation_when_usage_missing(local_service):
    import httpx
    from cloudyfan_jev.capacity_dispatch import TokenUpperBound

    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    controller = attach_admission(bundle, local_service.clock, rpm=10, tpm=1000)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "missing-token-proof"})
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "capacity_token_bound_unavailable"
    assert denied.json()["costs"]["unknown_events"] == 0 and bundle.upstream.calls == []
    assert operator.post("/monitor/capacity/activate", json={"revision": 1, "version": None}).status_code == 200
    proofs, actual = [], []

    class SyntheticTokenContract:
        version = "synthetic-token-envelope-v1"
        def __call__(self, body, scope):
            # Fixture's MockTransport contract: aggregate token use <=1000.
            proofs.append(body)
            return TokenUpperBound(1000, scope["wire_sha256"], scope["scope_sha256"], self.version,
                                   "synthetic-token-contract-not-production", local_service.clock() + 300)

    for contract in bundle.app.gateway.service.contracts.values():
        transport = contract.runner.client
        def upstream(request):
            actual.append(request.content)
            response = bundle.upstream(request)
            body = response.json()
            body["usage"] = {"cost": body["usage"]["cost"]}
            return httpx.Response(200, json=body)
        transport.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    # Register another immutable policy; this can select only controlled host routes.
    from dataclasses import replace
    routes = [replace(route, token_adapter=SyntheticTokenContract()) for route in controller.routes["synthetic-admission-v1"]]
    controller.register("synthetic-token-policy-v2", configuration_version="synthetic-admission-config-v1", routes=routes, max_seconds=300)
    assert operator.post("/monitor/capacity/activate", json={"revision": 2, "version": "synthetic-token-policy-v2"}).status_code == 200
    result = client.post("/v1/tasks", json={**TASK, "run_id": "token-proof", "prompt": "中文：請只回覆 2。"})
    assert result.status_code == 200 and result.json()["status"] == "passed"
    assert proofs == actual and len(actual) == 1
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocations"][0]["wire_sha256"] == hashlib.sha256(actual[0]).hexdigest()
    assert view["groups"][0]["tpm_reserved"] == 1000
    assert view["groups"][0]["concurrency_used"] == 0
    blocked = client.post("/v1/tasks", json={**TASK, "run_id": "tpm-full"})
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "capacity_tpm_exhausted"
    assert len(actual) == 1


def test_expired_token_proof_at_final_guard_releases_known_unsent_with_zero_receipt(local_service):
    from cloudyfan_jev.capacity_dispatch import TokenUpperBound
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()

    class SyntheticShortProof:
        version = "synthetic-short-token-v1"
        def __call__(self, body, scope):
            return TokenUpperBound(1000, scope["wire_sha256"], scope["scope_sha256"], self.version,
                                   "synthetic-expiring-proof", local_service.clock() + 1)

    class ExpireAfterIntent:
        def __call__(self, *args):
            return True
        def verify_dispatch(self, *args):
            return True
        def verify_post(self, *args):
            local_service.clock.now += 2
            return True

    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.verify_bound = ExpireAfterIntent()
    attach_admission(bundle, local_service.clock, rpm=10, tpm=2000, token_adapter=SyntheticShortProof())
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "expires-before-post"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "capacity_token_bound_expired"
    assert response.json()["costs"]["unknown_events"] == 0
    assert response.json()["costs"]["known_api_usd"] == "0"
    assert bundle.upstream.calls == []
    dispatch = operator.get("/monitor/capacity").json()["dispatch"]
    assert dispatch["allocations"][0]["state"] == "released"
    assert dispatch["groups"][0]["concurrency_used"] == dispatch["groups"][0]["rpm_used"] == dispatch["groups"][0]["tpm_reserved"] == 0
    ledger = operator.get("/monitor/snapshot").json()["ledger"]
    assert len(ledger["rows"]) == 1 and ledger["rows"][0]["cost_source"] == "not_dispatched"
