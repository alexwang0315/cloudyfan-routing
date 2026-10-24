"""Window opt-in preserves supported single-send model and external tool seams."""
from dataclasses import replace
import json

import httpx

from cloudyfan_jev.budget import EnforcedBound, ToolBound, ToolReceipt
from cloudyfan_jev.capacity_dispatch import CapacityRoute, CapacityToolRequest, CapacityToolTransport
from cloudyfan_jev.demo import ArithmeticEnvironment
from capacity_windows_fixture import attach_windows
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK


def tool_host(service, *, status=200):
    bundle = service.bundle
    wires = []
    def tool_response(request):
        wires.append(request.content)
        return httpx.Response(status, json={"answer": "2", "cost_usd": ".001", "receipt": "synthetic-window-tool-receipt"},
            headers={"retry-after": "30"})
    def decode(response):
        body = response.json()
        return ToolReceipt({"answer": body["answer"]}, body["cost_usd"], body["receipt"], True)
    transport = CapacityToolTransport(httpx.Client(transport=httpx.MockTransport(tool_response), trust_env=False),
        bundle.root / "window-tool-receipts", endpoint="https://synthetic-window-tool.invalid/sum",
        version="synthetic-window-tool-v1", decode=decode)
    class Environment(ArithmeticEnvironment):
        def tools_schema(self):
            return [{"type": "function", "function": {"name": "external_sum", "description": "Synthetic arithmetic",
                "parameters": {"type": "object", "properties": {}, "required": []}}}]
        def capacity_tool(self, name, arguments):
            return CapacityToolRequest(transport, {"arguments": arguments})
        def call(self, name, arguments):
            raise AssertionError("Only the registered actual transport may POST")
    bundle.upstream.fail_tiers = set()
    for key, contract in list(bundle.app.gateway.service.contracts.items()):
        contract.runner.tool_bounds["external_sum"] = ToolBound(EnforcedBound(".01", "synthetic-window-tool-bound"), external=True)
        contract.runner.core.limits = replace(contract.runner.core.limits, max_turns=2)
        contract.runner.core.execution_policy = replace(contract.runner.core.execution_policy, tools={"external_sum": "cloud:fixture"})
        bundle.app.gateway.service.contracts[key] = replace(contract,
            environment_factory=lambda workspace: Environment(), tool_names=("external_sum",))
        def upstream(request):
            body = bundle.upstream(request).json()
            if not any(m["role"] == "tool" for m in json.loads(request.content).get("messages", [])):
                body["choices"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "window-external-action", "type": "function", "function": {"name": "external_sum", "arguments": "{}"}}]}
            return httpx.Response(200, json=body)
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, _ = attach_windows(bundle, service.clock)
    controller.register("synthetic-window-tool-policy", configuration_version="synthetic-admission-config-v1",
        routes=[*controller.routes["synthetic-window-admission-v1"],
            CapacityRoute(transport, transport.config["endpoint"], "tool:external_sum", "synthetic-binding")],
        max_seconds=300, windows_version="synthetic-windows-v1")
    service.server.set_app(bundle.app)
    assert service.client("operator").post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-tool-policy"}).status_code == 200
    return bundle, wires


def test_window_policy_preserves_successful_registered_tool_receipt_and_original_fees(local_service):
    bundle, wires = tool_host(local_service)
    response = local_service.client().post("/v1/tasks", json={**TASK, "run_id": "window-tool-success"})
    assert response.status_code == 200 and response.json()["status"] == "passed", response.json()
    assert len(wires) == 1 and len(bundle.upstream.calls) == 2
    assert response.json()["costs"]["complete_api_usd"] == "0.0012"
    view = local_service.client("operator").get("/monitor/capacity").json()["dispatch"]
    assert view["windows"]["event_count"] == 0 and view["groups"][0]["concurrency_used"] == 0


def test_original_registered_tool_429_pauses_before_following_model_post_without_zero_fee(local_service):
    start = local_service.clock()
    bundle, wires = tool_host(local_service, status=429)
    client, operator = local_service.client(), local_service.client("operator")
    response = client.post("/v1/tasks", json={**TASK, "run_id": "actual-tool-rate-limit"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_window_wait"
    assert len(wires) == 1 and len(bundle.upstream.calls) == 1
    assert response.json()["costs"]["unknown_events"] == 0
    assert response.json()["costs"]["complete_api_usd"] == "0.0011"
    event = operator.get("/monitor/capacity").json()["dispatch"]["windows"]["events"][0]
    assert event["source_kind"] == "original_response" and event["not_before"] == start + 30
    original = response.json()
    assert client.post("/v1/tasks/actual-tool-rate-limit/resume", json={}).json() == original
    assert len(wires) == 1 and len(bundle.upstream.calls) == 1


def test_local_original_response_keeps_exact_transformed_wire_and_saved_window(local_service):
    from cloudyfan_jev.local import LocalBackend
    bundle = local_service.bundle
    actual = []
    def upstream(request):
        actual.append(json.loads(request.content))
        return httpx.Response(429, json={"model": "synthetic-local-window-v1", "choices": [],
            "error": {"code": 429}}, headers={"retry-after": "30"})
    backend = LocalBackend(httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False),
        bundle.root / "local-window-receipts", endpoint="http://127.0.0.1:11434/v1/chat/completions",
        model_version="synthetic-local-window-v1", quantization="fixture-q4", engine="synthetic-engine-v1",
        execution_location="home:fixture", no_model_fee_evidence="synthetic-local-fee-v1")
    for contract in bundle.app.gateway.service.contracts.values():
        original = contract.runner.core
        profiles = original.profiles
        profiles["open_source"].update(backend="local", model="synthetic-local-window-v1",
            model_version="synthetic-local-window-v1", accepted_models=["synthetic-local-window-v1"],
            quantization="fixture-q4", engine="synthetic-engine-v1", execution_location="home:fixture", effort="none")
        contract.runner.core = type(original)(original.client, profiles, original.decision,
            local_backend=backend, execution_policy=replace(original.execution_policy, origin="home:fixture"),
            limits=original.limits)
        contract.runner.model_bounds["synthetic-local-window-v1"] = EnforcedBound("0", "synthetic-local-fee-v1")
    attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    client, operator = local_service.client(), local_service.client("operator")
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    first = client.post("/v1/tasks", json={**TASK, "run_id": "local-original-rate-limit"})
    assert first.json()["costs"]["unknown_events"] == 0
    view = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert view["groups"][0]["status"] == "wait" and view["groups"][0]["not_before"] == local_service.clock() + 30
    assert len(actual) == 1 and set(actual[0]) == {"model", "messages", "max_tokens", "stream"}
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "local-window-excluded"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_wait"
    assert len(actual) == 1 and bundle.upstream.calls == []
