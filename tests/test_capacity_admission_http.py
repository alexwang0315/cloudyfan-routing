"""Immediate controlled admission through the agreed actual HTTP seam."""
import hashlib
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_admission_fixture import attach_admission
from test_candidate_health_http import health_http as health_http


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


def test_mismatched_actual_identity_cannot_reduce_tpm_or_release_concurrency(local_service):
    import httpx
    from cloudyfan_jev.capacity_dispatch import TokenUpperBound
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()

    class SyntheticProof:
        version = "synthetic-mismatch-token-v1"
        def __call__(self, body, scope):
            return TokenUpperBound(1000, scope["wire_sha256"], scope["scope_sha256"], self.version,
                                   "synthetic-token-contract", local_service.clock() + 300)

    for contract in bundle.app.gateway.service.contracts.values():
        def upstream(request):
            response = bundle.upstream(request)
            body = response.json()
            body["model"] = "different-unaccepted-model"
            return httpx.Response(200, json=body)
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_admission(bundle, local_service.clock, rpm=10, tpm=2000, token_adapter=SyntheticProof())
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "identity-mismatch"})
    assert response.status_code == 503
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocations"][0]["state"] == "uncertain"
    assert view["groups"][0]["tpm_reserved"] == 1000 and view["groups"][0]["concurrency_used"] == 1
    assert len(bundle.upstream.calls) == 1


def test_applicable_local_generation_uses_transformed_wire_and_capacity(local_service):
    from copy import deepcopy
    from dataclasses import replace
    import httpx
    import json
    from cloudyfan_jev.local import LocalBackend
    from cloudyfan_jev.engine import Core
    from cloudyfan_jev.budget import EnforcedBound
    bundle = local_service.bundle
    actual = []
    def local_upstream(request):
        actual.append(request.content)
        return httpx.Response(200, json={"model": "synthetic-local-v1", "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "choices": [{"message": {"role": "assistant", "content": "2"}, "finish_reason": "stop"}]})
    local = LocalBackend(httpx.Client(transport=httpx.MockTransport(local_upstream), trust_env=False),
        bundle.root / "local-receipts", endpoint="http://127.0.0.1:11434/v1/chat/completions",
        model_version="synthetic-local-v1", quantization="fixture-q4", engine="synthetic-engine-v1",
        execution_location="home:fixture", no_model_fee_evidence="synthetic-local-fee-v1")
    for contract in bundle.app.gateway.service.contracts.values():
        runner, original = contract.runner, contract.runner.core
        profiles = deepcopy(original.profiles)
        profiles["open_source"].update(backend="local", model="synthetic-local-v1", model_version="synthetic-local-v1",
            accepted_models=["synthetic-local-v1"], quantization="fixture-q4", engine="synthetic-engine-v1", execution_location="home:fixture", effort="none")
        runner.core = Core(original.client, profiles, original.decision, limits=original.limits,
            local_backend=local, execution_policy=replace(original.execution_policy, origin="home:fixture"),
            diagnostic_contract=original.diagnostic_contract)
        runner.model_bounds["synthetic-local-v1"] = EnforcedBound("0", "synthetic-local-fee-v1")
    attach_admission(bundle, local_service.clock, rpm=1)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    first = client.post("/v1/tasks", json={**TASK, "run_id": "controlled-local"})
    assert first.status_code == 200 and first.json()["status"] == "passed"
    assert len(actual) == 1 and bundle.upstream.calls == []
    assert json.loads(actual[0])["stream"] is False
    second = client.post("/v1/tasks", json={**TASK, "run_id": "controlled-local-full"})
    assert second.status_code == 429 and second.json()["error"]["code"] == "capacity_rpm_exhausted"
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert len(actual) == 1 and view["allocations"][0]["wire_sha256"] == hashlib.sha256(actual[0]).hexdigest()
    assert view["groups"][0]["rpm_used"] == 1


def test_external_tool_refusal_precedes_action_intent_and_fee_event(local_service):
    from dataclasses import replace
    import httpx
    from cloudyfan_jev.budget import EnforcedBound, ToolBound, ToolReceipt
    from cloudyfan_jev.demo import ArithmeticEnvironment
    bundle = local_service.bundle
    actual_tool = []

    class ExternalEnvironment(ArithmeticEnvironment):
        def tools_schema(self):
            return [{"type": "function", "function": {"name": "external_sum", "description": "Synthetic external arithmetic",
                "parameters": {"type": "object", "properties": {}, "required": []}}}]
        def call(self, name, arguments):
            actual_tool.append(arguments)
            return ToolReceipt({"answer": "2"}, ".001", "synthetic-tool-1", True)

    service = bundle.app.gateway.service
    for key, contract in list(service.contracts.items()):
        contract.runner.tool_bounds["external_sum"] = ToolBound(EnforcedBound(".01", "synthetic-tool-bound"), external=True)
        contract.runner.core.execution_policy = replace(contract.runner.core.execution_policy, tools={"external_sum": "cloud:fixture"})
        service.contracts[key] = replace(contract, environment_factory=lambda workspace: ExternalEnvironment(), tool_names=("external_sum",))
        def upstream(request):
            response = bundle.upstream(request)
            body = response.json()
            body["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-synthetic-external", "type": "function", "function": {"name": "external_sum", "arguments": "{}"}}]}
            return httpx.Response(200, json=body)
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "unregistered-external-tool"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_tool_protocol_unavailable"
    assert actual_tool == [] and len(bundle.upstream.calls) == 1
    ledger = operator.get("/monitor/snapshot").json()["ledger"]
    assert len(ledger["rows"]) == 1 and ledger["unknown_events"] == 0
    assert not list((bundle.root / "receipts" / "runs").glob("**/action-*-intent.json"))


def test_registered_external_tool_posts_once_through_same_capacity_and_fee_guards(local_service):
    from dataclasses import replace
    import httpx
    import json
    from cloudyfan_jev.capacity_dispatch import CapacityRoute, CapacityToolTransport, CapacityToolRequest
    from cloudyfan_jev.budget import EnforcedBound, ToolBound, ToolReceipt
    from cloudyfan_jev.demo import ArithmeticEnvironment
    bundle = local_service.bundle
    actual_tool = []
    def tool_upstream(request):
        actual_tool.append(request.content)
        return httpx.Response(200, json={"answer": "2", "cost_usd": ".001", "receipt": "synthetic-tool-receipt"})
    def decode(response):
        body = response.json()
        return ToolReceipt({"answer": body["answer"]}, body["cost_usd"], body["receipt"], True)
    transport = CapacityToolTransport(httpx.Client(transport=httpx.MockTransport(tool_upstream), trust_env=False),
        bundle.root / "tool-receipts", endpoint="https://synthetic-tool.invalid/sum", version="synthetic-tool-v1", decode=decode)
    class ExternalEnvironment(ArithmeticEnvironment):
        def tools_schema(self):
            return [{"type": "function", "function": {"name": "external_sum", "description": "Synthetic external arithmetic",
                "parameters": {"type": "object", "properties": {}, "required": []}}}]
        def capacity_tool(self, name, arguments):
            return CapacityToolRequest(transport, {"arguments": arguments})
        def call(self, name, arguments):
            raise AssertionError("Controlled tool transport must own the actual POST")
    service = bundle.app.gateway.service
    bundle.upstream.fail_tiers = set()
    for key, contract in list(service.contracts.items()):
        contract.runner.tool_bounds["external_sum"] = ToolBound(EnforcedBound(".01", "synthetic-tool-bound"), external=True)
        contract.runner.core.limits = replace(contract.runner.core.limits, max_turns=2)
        contract.runner.core.execution_policy = replace(contract.runner.core.execution_policy, tools={"external_sum": "cloud:fixture"})
        service.contracts[key] = replace(contract, environment_factory=lambda workspace: ExternalEnvironment(), tool_names=("external_sum",))
        def upstream(request):
            payload = json.loads(request.content)
            response = bundle.upstream(request)
            body = response.json()
            if not any(message["role"] == "tool" for message in payload.get("messages", [])):
                body["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call-synthetic-external", "type": "function", "function": {"name": "external_sum", "arguments": "{}"}}]}
            return httpx.Response(200, json=body)
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller = attach_admission(bundle, local_service.clock, rpm=10)
    controller.register("synthetic-tool-policy-v2", configuration_version="synthetic-admission-config-v1",
        routes=[*controller.routes["synthetic-admission-v1"], CapacityRoute(transport, transport.config["endpoint"], "tool:external_sum", "synthetic-binding")], max_seconds=300)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-tool-policy-v2"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "registered-tool"})
    assert response.status_code == 200 and response.json()["status"] == "passed"
    assert len(actual_tool) == 1 and len(bundle.upstream.calls) == 2
    ledger = operator.get("/monitor/snapshot").json()["ledger"]
    assert len(ledger["rows"]) == 3 and ledger["unknown_events"] == 0
    assert [row["cost"] for row in ledger["rows"] if row["kind"] == "tool"] == ["0.001"]
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["groups"][0]["rpm_used"] == 3 and view["groups"][0]["concurrency_used"] == 0
    assert {row["state"] for row in view["allocations"]} == {"complete"}
    transport.http.headers["Authorization"] = "Bearer SYNTHETIC_PRIVATE_DIFFERENT_ACCOUNT"
    changed = client.post("/v1/tasks", json={**TASK, "run_id": "changed-tool-account"})
    assert changed.status_code == 429 and changed.json()["error"]["code"] == "capacity_transport_binding_changed"
    assert len(actual_tool) == 1
    after = operator.get("/monitor/snapshot").json()["ledger"]
    assert after["unknown_events"] == 0 and len(after["rows"]) == 4
    assert "SYNTHETIC_PRIVATE_DIFFERENT_ACCOUNT" not in operator.get("/monitor/capacity").text


def test_backwards_clock_after_intent_still_records_proven_unsent_zero_and_releases(local_service):
    bundle = local_service.bundle
    accepted = local_service.clock()
    class BackwardsAfterIntent:
        def __call__(self, *args):
            return True
        def verify_dispatch(self, *args):
            return True
        def verify_post(self, *args):
            local_service.clock.now = accepted - 10
            return True
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.verify_bound = BackwardsAfterIntent()
    attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "clock-before-post"})
    # Authentication and reads also fail closed while the host clock is invalid.
    assert response.status_code in (429, 409, 503)
    local_service.clock.now = accepted + 1
    state = client.get("/v1/tasks/clock-before-post").json()
    assert state["costs"]["unknown_events"] == 0 and state["costs"]["known_api_usd"] == "0"
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocations"][0]["state"] == "released"
    assert bundle.upstream.calls == []
    assert operator.get("/monitor/snapshot").json()["ledger"]["rows"][0]["cost_source"] == "not_dispatched"


def test_authoritative_usage_above_token_upper_invalidates_policy_and_keeps_actual_quantity(local_service):
    from cloudyfan_jev.capacity_dispatch import TokenUpperBound
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    class BrokenSyntheticUpper:
        version = "synthetic-broken-upper-v1"
        def __call__(self, body, scope):
            return TokenUpperBound(1, scope["wire_sha256"], scope["scope_sha256"], self.version,
                                   "synthetic-broken-upper", local_service.clock() + 300)
    attach_admission(bundle, local_service.clock, rpm=10, tpm=1000, token_adapter=BrokenSyntheticUpper())
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    first = client.post("/v1/tasks", json={**TASK, "run_id": "bad-upper-one"})
    assert first.status_code == 429 and first.json()["error"]["code"] == "capacity_token_bound_violated"
    assert first.json()["costs"]["unknown_events"] == 0
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["groups"][0]["tpm_reserved"] == 21 and view["groups"][0]["concurrency_used"] == 1
    second = client.post("/v1/tasks", json={**TASK, "run_id": "bad-upper-two"})
    assert second.status_code == 429 and second.json()["error"]["code"] == "capacity_token_proof_invalidated"
    assert len(bundle.upstream.calls) == 1
    assert any(event["kind"] == "bound_violation" for event in view["events"])


def test_bounded_capacity_history_keeps_exact_hot_totals_and_readonly_bytes(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    controller = attach_admission(bundle, local_service.clock, rpm=10)
    controller.history_limit = 2  # Trusted host projection limit, never request input.
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    for number in range(3):
        assert client.post("/v1/tasks", json={**TASK, "run_id": f"history-{number}"}).json()["status"] == "passed"
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocation_count"] == 3 and len(view["allocations"]) == 2
    assert view["allocation_history_complete"] is False
    assert view["event_count"] == 10 and len(view["events"]) == 2 and view["event_history_complete"] is False
    assert view["groups"][0]["rpm_used"] == 3
    before = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    for _ in range(2):
        assert operator.get("/monitor/capacity").json()["dispatch"] == view
    assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == before
    assert len(bundle.upstream.calls) == 3


def test_original_accepted_credential_revocation_stops_classifier_and_alternate_resume_is_readonly(local_service):
    bundle = local_service.bundle
    attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    original = bundle.credentials["studio"]
    alternate = bundle.identity.issue("studio", scopes={"tasks:read", "tasks:write"}, expires_at=local_service.clock() + 600)
    bundle.upstream.before_response = lambda _: bundle.identity.revoke(original.id)
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    result = client.post("/v1/tasks", json={**TASK, "run_id": "revoked-original"})
    assert result.status_code == 429 and result.json()["error"]["code"] == "capacity_credential_inactive"
    assert len(bundle.upstream.calls) == 1
    assert operator.get("/monitor/snapshot").json()["ledger"]["unknown_events"] == 0
    bundle.credentials["studio"] = alternate
    fresh = local_service.client()
    state = fresh.get("/v1/tasks/revoked-original").json()
    assert state["capacity"]["policy_version"] == "synthetic-admission-v1"
    for _ in range(2):
        assert fresh.post("/v1/tasks/revoked-original/resume", json={}).json() == state
        assert fresh.post("/v1/tasks", json={**TASK, "run_id": "revoked-original"}).json() == state
    assert len(bundle.upstream.calls) == 1


def test_historical_policy_binding_survives_safe_disable_after_window_and_direct_service_has_no_bypass(local_service):
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    from cloudyfan_jev.capacity_dispatch import CapacityDenied
    from cloudyfan_jev.demo import ArithmeticEnvironment
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    controller = attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    accepted = client.post("/v1/tasks", json={**TASK, "run_id": "frozen-policy"}).json()
    assert accepted["status"] == "passed"
    assert operator.post("/monitor/capacity/activate", json={"revision": 1, "version": None}).status_code == 409
    local_service.clock.now += 61
    assert operator.post("/monitor/capacity/activate", json={"revision": 1, "version": None}).status_code == 200
    assert client.post("/v1/tasks", json={**TASK, "run_id": "frozen-policy"}).json() == accepted
    assert client.post("/v1/tasks/frozen-policy/resume", json={}).json() == accepted
    disabled = client.post("/v1/tasks", json={**TASK, "run_id": "disabled-new"})
    assert disabled.status_code == 200 and disabled.json()["capacity"]["policy_version"] is None
    assert len(bundle.upstream.calls) == 2
    assert len(operator.get("/monitor/capacity").json()["dispatch"]["allocations"]) == 1
    service = bundle.app.gateway.service
    contract_id = next(iter(service.contracts))
    with pytest.raises(ServiceError, match="capacity_authenticated_ingress_required"):
        service.create("studio", {"run_id": "direct-bypass", "contract_id": contract_id, "prompt": "2"})
    runner = service.contracts[contract_id].runner
    with pytest.raises(CapacityDenied, match="capacity_adoption_missing"):
        runner.run("direct-bypass", {"prompt": "2"}, ArithmeticEnvironment, contract_version="direct")
    assert controller.own("direct-bypass") is None
    assert len(bundle.upstream.calls) == 2


def test_native_ingress_uses_same_frozen_admission_and_safe_refusal(local_service):
    from cloudyfan_monitor.ollama_gateway import OllamaGateway
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    attach_admission(bundle, local_service.clock, rpm=1)
    bundle.app.ollama = OllamaGateway(bundle.app.gateway, models={"cloudyfan/arithmetic:v1": {
        "category": TASK["category"], "contract_id": "demo-cloudyfan"}}, authenticate=bundle.app._native_authenticate)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    payload = {"model": "cloudyfan/arithmetic:v1", "prompt": TASK["prompt"], "stream": False}
    first = client.post("/api/generate", headers={"X-CloudyFan-Run-ID": "native-capacity-one"}, json=payload)
    assert first.status_code == 200 and first.json()["response"] == "2"
    second = client.post("/api/generate", headers={"X-CloudyFan-Run-ID": "native-capacity-full"}, json=payload)
    assert second.status_code == 429 and second.json()["error"] == "capacity_rpm_exhausted"
    assert len(bundle.upstream.calls) == 1
    state = client.get("/v1/tasks/native-capacity-full").json()
    assert state["status"] == "capacity_denied" and state["capacity"]["policy_version"] == "synthetic-admission-v1"
    assert client.post("/v1/tasks/native-capacity-full/resume", json={}).json() == state
    assert len(bundle.upstream.calls) == 1


def test_final_health_guard_rechecks_real_store_after_paid_intent_before_post(health_http):
    import httpx
    host, operator = health_http
    class CloseHealthAfterIntent:
        def __call__(self, *args):
            return True
        def verify_dispatch(self, *args):
            return True
        def verify_post(self, *args):
            for tier, records in host.values["plan"]["pools"].items():
                for position in range(len(records)):
                    observed = host.observation(tier, position)
                    for number in (1, 2):
                        host.health.record(f"synthetic-postguard-{tier}-{position}-{number}",
                            observed["configuration_sha256"], observed["provider"])
            return True
    for contract in host.service.contracts.values():
        contract.runner.verify_bound = CloseHealthAfterIntent()
    attach_admission(host, host.clock, rpm=10)
    host.server.set_app(host.app)
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    with httpx.Client(base_url=host.app.origin, headers={"Authorization": "Bearer " + host.credentials["studio"].token}, trust_env=False) as client:
        response = client.post("/v1/tasks", json={"run_id": "postguard-health", "category": host.category, "prompt": TASK["prompt"]})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_original_gate_denied"
    assert host.calls == []
    assert response.json()["costs"]["unknown_events"] == 0
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocations"][0]["state"] == "released"
    assert operator.get("/monitor/snapshot").json()["ledger"]["rows"][0]["cost_source"] == "not_dispatched"


def test_invalid_token_time_is_safe_preintent_refusal_without_fee_unknown(local_service):
    from cloudyfan_jev.capacity_dispatch import TokenUpperBound
    class InvalidTimeProof:
        version = "synthetic-invalid-time-v1"
        def __call__(self, body, scope):
            return TokenUpperBound(100, scope["wire_sha256"], scope["scope_sha256"], self.version,
                                   "synthetic-invalid-time", float("nan"))
    bundle = local_service.bundle
    attach_admission(bundle, local_service.clock, rpm=10, tpm=1000, token_adapter=InvalidTimeProof())
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "invalid-token-time"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_token_bound_invalid"
    assert bundle.upstream.calls == [] and response.json()["costs"]["unknown_events"] == 0
    view = operator.get("/monitor/capacity").json()["dispatch"]
    assert view["allocations"] == []


def test_all_original_generation_and_classifier_attempt_limits_remain_shared(local_service):
    bundle = local_service.bundle
    attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "original-global-limits"})
    assert response.status_code == 200 and response.json()["status"] == "passed"
    assert len(bundle.upstream.calls) == 5
    assert [payload["model"] for payload in bundle.upstream.calls if "questions" not in payload] == ["example/open_source", "example/mid", "example/high"]
    assert sum("questions" in payload for payload in bundle.upstream.calls) == 2
    result = client.get("/v1/tasks/original-global-limits/result").json()
    assert len(result["provenance"]["attempts"]) == 3 and len(result["provenance"]["classifier_calls"]) == 2
    before = operator.get("/monitor/capacity").json()["dispatch"]
    assert len(before["allocations"]) == 5 and before["groups"][0]["rpm_used"] == 5
    assert before["groups"][0]["concurrency_used"] == 0
    assert client.post("/v1/tasks/original-global-limits/resume", json={}).json() == response.json()
    assert operator.get("/monitor/capacity").json()["dispatch"] == before and len(bundle.upstream.calls) == 5


def test_original_whole_task_deadline_is_rechecked_after_intent_before_post(health_http):
    import httpx
    host, operator = health_http
    class CrossOriginalDeadline:
        def __call__(self, *args):
            return True
        def verify_dispatch(self, *args):
            return True
        def verify_post(self, *args):
            host.clock.now += 10000
            return True
    for contract in host.service.contracts.values():
        contract.runner.verify_bound = CrossOriginalDeadline()
    attach_admission(host, host.clock, rpm=10, seconds=86400)
    host.server.set_app(host.app)
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    with httpx.Client(base_url=host.app.origin, headers={"Authorization": "Bearer " + host.credentials["studio"].token}, trust_env=False) as client:
        response = client.post("/v1/tasks", json={"run_id": "postguard-original-deadline", "category": host.category, "prompt": TASK["prompt"]})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_original_deadline_exceeded"
    assert host.calls == [] and response.json()["costs"]["unknown_events"] == 0
    assert operator.get("/monitor/capacity").json()["dispatch"]["allocations"][0]["state"] == "released"


def test_capacity_available_does_not_override_private_location_or_client_policy_input(local_service):
    from dataclasses import replace
    bundle = local_service.bundle
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.core.execution_policy = replace(contract.runner.core.execution_policy, mode="local_only")
    attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    override = client.post("/v1/tasks", json={**TASK, "run_id": "client-policy-override", "capacity": {"enabled": False, "tokens": 0}})
    assert override.status_code == 422
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "private-location"})
    assert denied.status_code == 403 and denied.json()["status"] == "policy_denied"
    assert denied.json()["costs"]["unknown_events"] == 0 and bundle.upstream.calls == []
    assert operator.get("/monitor/capacity").json()["dispatch"]["allocations"] == []


def test_required_supplier_proof_is_not_inferred_from_local_caps(local_service):
    from dataclasses import replace
    bundle = local_service.bundle
    controller = attach_admission(bundle, local_service.clock, rpm=10)
    controller.register("synthetic-require-supplier-v2", configuration_version="synthetic-admission-config-v1",
        routes=[replace(route, require_supplier=("rpm",)) for route in controller.routes["synthetic-admission-v1"]], max_seconds=300)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-require-supplier-v2"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "supplier-proof-missing"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_supplier_evidence_unavailable"
    assert response.json()["costs"]["unknown_events"] == 0 and bundle.upstream.calls == []
    assert operator.get("/monitor/capacity").json()["dispatch"]["allocations"] == []


def test_optin_rejects_mutating_http_hooks_before_paid_intent(local_service):
    bundle = local_service.bundle
    def forbidden_hook(request):
        request.url = "https://different-account.invalid/changed"
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http.event_hooks["request"] = [forbidden_hook]
    attach_admission(bundle, local_service.clock, rpm=10)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "unsupported-hook"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_transport_protocol_unsupported"
    assert response.json()["costs"]["unknown_events"] == 0 and bundle.upstream.calls == []


def test_rate_window_starts_at_actual_post_after_guard_delay(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    start = local_service.clock()
    class DelayFirstPost:
        calls = 0
        def __call__(self, *args):
            return True
        def verify_dispatch(self, *args):
            return True
        def verify_post(self, *args):
            self.calls += 1
            if self.calls == 1:
                local_service.clock.now += 30
            return True
    verifier = DelayFirstPost()
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.verify_bound = verifier
    attach_admission(bundle, local_service.clock, rpm=1, concurrency=2)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    assert client.post("/v1/tasks", json={**TASK, "run_id": "delayed-post"}).json()["status"] == "passed"
    assert local_service.clock() == start + 30
    local_service.clock.now = start + 61
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "inside-real-window"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_rpm_exhausted"
    assert len(bundle.upstream.calls) == 1
    local_service.clock.now = start + 91
    assert client.post("/v1/tasks", json={**TASK, "run_id": "after-real-window"}).json()["status"] == "passed"
    assert len(bundle.upstream.calls) == 2


def test_optin_rejects_retrying_http_client_subclass_before_any_physical_post(local_service):
    import httpx
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    class RetryingClient(httpx.Client):
        def send(self, request, **kwargs):
            response = super().send(request, **kwargs)
            return super().send(request, **kwargs) if response.status_code == 503 else response
    def upstream(request):
        response = bundle.upstream(request)
        return httpx.Response(503 if len(bundle.upstream.calls) == 1 else 200, json=response.json())
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = RetryingClient(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_admission(bundle, local_service.clock, rpm=1)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    response = client.post("/v1/tasks", json={**TASK, "run_id": "retrying-subclass"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_transport_protocol_unsupported"
    assert bundle.upstream.calls == [] and response.json()["costs"]["unknown_events"] == 0


def test_explicit_source_refresh_preserves_active_policy_projection(local_service):
    bundle = local_service.bundle
    attach_admission(bundle, local_service.clock, rpm=1)
    local_service.server.set_app(bundle.app)
    operator = local_service.client("operator")
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-admission-v1"}).status_code == 200
    expected = operator.get("/monitor/capacity").json()["dispatch"]
    refreshed = operator.post("/monitor/capacity/refresh", json={})
    assert refreshed.status_code == 200
    assert refreshed.json()["execution_enabled"] is True
    assert refreshed.json()["snapshot"]["dispatch"] == expected
    assert refreshed.json()["snapshot"]["execution_enabled"] is True
    assert refreshed.json()["request_count"] == 0 and bundle.upstream.calls == []
