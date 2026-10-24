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
