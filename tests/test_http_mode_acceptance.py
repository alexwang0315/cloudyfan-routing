"""Four-mode acceptance at the approved real HTTP boundary; no live qualification."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
import json
import os
from pathlib import Path
import hashlib
import threading

import pytest

from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.budget import BudgetedCore
from cloudyfan_jev.engine import Core
from cloudyfan_jev.http_api import RegisteredContract

from cloudyfan_monitor.integration import assemble_monitor
from cloudyfan_monitor.store import MODES

from test_category_host_integration import (
    CONTRACT, OPERATOR, assemble_category_host, category_fixture as category_fixture,
)
from test_local_http_integration import (
    Clock, LocalService, QuietHandler, ThreadedServer, loopback_network as loopback_network,
)


@dataclass
class ModeHost:
    app: object
    service: object
    budget: object
    credentials: dict = field(repr=False)
    root: Path
    pools: tuple
    values: dict
    calls: list
    clock: object
    operator_token: str = field(default=OPERATOR, repr=False)


def assemble_mode_host(root, values, calls, origin, *, clock=None):
    """Register actual CategoryCore plus existing baseline Core modes on one payer ledger.

    The second pool is another registered synthetic view, not a new qualification.
    Its CategoryCore contract has a distinct registry identity but the same exact
    calibrated descriptor. Existing jobs must still resolve their original ID.
    """
    root = Path(root)
    if clock is None:
        clock = Clock(datetime.fromisoformat(values["now"]).timestamp())
    original_app, service, budget, credentials = assemble_category_host(root, values, calls, clock=clock)
    category_contract = service.contracts["category-host-v1"]
    template = category_contract.runner.core
    profiles, decision = example_config()
    for profile in profiles.values():
        profile["execution_location"] = "cloud:fixture"
    baseline = Core(template.client, profiles, decision, limits=values["limits"],
                    execution_policy=values["privacy"], diagnostic_contract=values["diagnostics"])
    runner = BudgetedCore(baseline, budget, model_bounds=category_contract.runner.model_bounds,
                          tool_bounds={}, verify_bound=category_contract.runner.verify_bound)
    first = next(iter(original_app.gateway.registry))[0]
    second_view = original_app.store.pool(first)
    second_view.pop("id")
    second_view["source_kind"] = "second_registered_synthetic_category_host_view"
    second = original_app.store.save_pool(second_view)
    registry = {}
    for revision, pool in enumerate((first, second), 1):
        for mode in MODES:
            contract_id = f"mode-{mode}-v{revision}"
            if mode == "cloudyfan":
                contract = category_contract
            else:
                contract = RegisteredContract(f"synthetic-baseline-{mode}-v{revision}", runner,
                    lambda workspace: ArithmeticEnvironment(), mode=mode)
            service.contracts[contract_id] = contract
            registry[(pool, values["category"], mode)] = contract_id
    # Every configured ID has one role; the original helper's unused ID is not
    # left in the registry as a possible substitute for a frozen task.
    service.contracts.pop("category-host-v1")
    app = assemble_monitor(original_app.store, service, registry, original_app.identity_store,
        operator_token=OPERATOR, origin=origin, demo=True)
    return ModeHost(app, service, budget, credentials, root, (first, second), values, calls, clock)


@pytest.fixture
def mode_service(tmp_path, category_fixture, loopback_network, request):
    """Reuse the existing socket allowlist/server/client fixtures, capturing this host's calls."""
    values = deepcopy(category_fixture)
    calls = []
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        host = assemble_mode_host(tmp_path / "service", values, calls, origin)
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        service = LocalService(host, origin, stack, server, host.clock)
        # These are functional receipt/contract workflows, not latency checks.
        # Leave dedicated responsiveness fixtures at their existing 2s default.
        service.client = partial(service.client, timeout=10)
        try:
            yield service
        finally:
            destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                receipt = {
                    "schema_version": 1, "test": request.node.name,
                    "allowed_loopback_ports": sorted(loopback_network.ports),
                    "socket_attempts": [{"host": address[0], "port": address[1]}
                                        for address in loopback_network.connected],
                    "blocked_socket_attempts": [str(address) for address in loopback_network.blocked],
                    "http_responses": service.http_records,
                    "mock_upstream_call_count": len(calls),
                    "mock_upstream_calls": [{"model": payload.get("model"),
                        "kind": "classification" if "questions" in payload else "generation"} for payload in calls],
                    "observations": service.additional_workers,
                    "scope": "actual loopback HTTP; true CategoryCore and Core baselines; mocked upstream",
                }
                (directory / (request.node.name + ".json")).write_text(json.dumps(receipt, indent=2))
            assert not loopback_network.blocked
            assert loopback_network.connected


def task(host, run_id):
    return {"run_id": run_id, "category": host.values["category"], "prompt": "Return only 1+1."}


def switch(operator, mode, client=None):
    snapshot = operator.get("/monitor/snapshot").json()
    response = operator.post("/monitor/mode", json={"revision": snapshot["control"]["revision"],
        "mode": mode, **({"client": client} if client is not None else {})})
    assert response.status_code == 200, response.json()


@pytest.mark.parametrize("mode,first_model,routing_version", [
    ("cloudyfan", "example/open_source", "hierarchical-qcs-v1"),
    ("fixed_high", "example/high", "hierarchical-sequential-v1"),
    ("low_first", "example/low", "hierarchical-sequential-v1"),
    ("open_source_first", "example/open_source", "hierarchical-sequential-v1"),
], ids=["cloudyfan", "fixed_high", "low_first", "open_source_first"])
def test_http_four_modes_dispatch_registered_runners_on_one_budget(
        mode_service, mode, first_model, routing_version):
    host = mode_service.bundle
    operator = mode_service.client("operator")
    client = mode_service.client()
    switch(operator, mode)
    payload = task(host, "actual-" + mode)
    response = client.post("/v1/tasks", json=payload)
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "passed"
    result = client.get(f"/v1/tasks/{payload['run_id']}/result")
    assert result.status_code == 200, result.json()
    outcome = result.json()
    assert outcome["answer"] == "2"
    assert outcome["provenance"]["routing_version"] == routing_version
    assert outcome["provenance"]["attempts"][0]["calls"][0]["actual_model"] == first_model
    assert [call["model"] for call in host.calls] == [first_model]
    snapshot = operator.get("/monitor/snapshot").json()
    observed = snapshot["tasks"][0]
    assert (observed["mode"], observed["generation_attempts"], observed["classifier_calls"]) == (mode, 1, 0)
    assert len(snapshot["ledger"]["accounts"]) == 1
    assert snapshot["ledger"]["known_usd"] == "0.01"
    mode_service.additional_workers.append({"mode": mode, "first_model": first_model,
        "routing_version": outcome["provenance"]["routing_version"], "budget_accounts": 1,
        "known_api_usd": snapshot["ledger"]["known_usd"]})


def test_http_category_tasks_bind_each_client_identity_and_server_workload(mode_service):
    host = mode_service.bundle
    operator = mode_service.client("operator")
    payload = task(host, "same-public-run")
    internal_ids = set()
    for principal in ("studio", "agent"):
        client = mode_service.client(principal)
        response = client.post("/v1/tasks", json=payload)
        assert response.status_code == 200 and response.json()["status"] == "passed", response.json()
        result = client.get("/v1/tasks/same-public-run/result")
        assert result.status_code == 200 and result.json()["answer"] == "2"
        assert result.json()["provenance"]["contract_version"] == CONTRACT
        internal = host.service.internal_id(principal, payload["run_id"])
        internal_ids.add(internal)
        # The public Core receipt is part of the approved audit evidence, not
        # an alternate task entry point or a replacement for the HTTP outcome.
        manifest_path = host.root / "receipts" / "runs" / internal / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        assert manifest["mode"] == "cloudyfan"
        assert manifest["task"]["category"] == "artificial-analysis:coding_index"
        assert manifest["task"]["workload_id"] == "arithmetic-small"
        assert manifest["task"]["category_binding"]["category_id"] == "artificial-analysis:coding_index"
        assert manifest["task"]["category_binding"]["matrix_id"] == host.values["matrix"]["matrix_id"]
        dispatches = len(host.calls)
        for prohibited in ({"workload_id": "client-selected-workload"},
                           {"category_binding": {"matrix_id": "client-selected-matrix"}},
                           {"contract_id": "client-selected-contract"}):
            rejected = client.post("/v1/tasks", json={**payload, "run_id": "untrusted", **prohibited})
            assert rejected.status_code == 422
        assert len(host.calls) == dispatches
        mode_service.additional_workers.append({"client": principal, "public_run": payload["run_id"],
            "internal_run": internal, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "host_workload": manifest["task"]["workload_id"],
            "category_binding": manifest["task"]["category_binding"]})
    assert len(internal_ids) == 2
    snapshot = operator.get("/monitor/snapshot").json()
    assert len(host.calls) == 2
    assert snapshot["summary"]["passed"] == 2
    assert snapshot["ledger"]["known_usd"] == "0.02"
    assert snapshot["ledger"]["by_client_known_usd"] == {"studio": "0.01", "agent": "0.01"}
    assert len(snapshot["ledger"]["accounts"]) == 1
    assert {event["run"] for event in host.budget.report()["events"]} == internal_ids


def test_http_switches_and_reopen_preserve_original_binding_receipts_and_deadline(mode_service):
    host = mode_service.bundle
    operator = mode_service.client("operator")
    studio = mode_service.client()
    agent = mode_service.client("agent")
    original = task(host, "original-category")
    assert studio.post("/v1/tasks", json=original).json()["status"] == "passed"
    result = studio.get("/v1/tasks/original-category/result").json()
    internal = host.service.internal_id("studio", original["run_id"])
    paths = [host.root / "receipts" / "runs" / internal / "manifest.json",
             host.service.root / internal / "job.json"]
    original_bytes = {path: path.read_bytes() for path in paths}
    lifecycle = deepcopy(host.service.retention.lookup(internal))
    original_row = operator.get("/monitor/snapshot").json()["tasks"][0]

    switch(operator, "fixed_high")
    assert agent.post("/v1/tasks", json=task(host, "global-after")).json()["status"] == "passed"
    switch(operator, "low_first", "studio")
    assert studio.post("/v1/tasks", json=task(host, "override-after")).json()["status"] == "passed"
    activate = operator.post("/monitor/pools/activate", json={"pool": host.pools[1],
        "revision": operator.get("/monitor/snapshot").json()["control"]["revision"]})
    assert activate.status_code == 200, activate.json()
    assert studio.post("/v1/tasks", json=task(host, "pool-after")).json()["status"] == "passed"
    switch(operator, None, "studio")
    switch(operator, "open_source_first")
    assert agent.post("/v1/tasks", json=task(host, "latest-after")).json()["status"] == "passed"
    snapshot = operator.get("/monitor/snapshot").json()
    rows = {row["run"]: row for row in snapshot["tasks"]}
    assert {run: (row["mode"], row["pool"]) for run, row in rows.items()} == {
        "original-category": ("cloudyfan", host.pools[0]),
        "global-after": ("fixed_high", host.pools[0]),
        "override-after": ("low_first", host.pools[0]),
        "pool-after": ("low_first", host.pools[1]),
        "latest-after": ("open_source_first", host.pools[1]),
    }
    assert [call["model"] for call in host.calls] == ["example/open_source", "example/high",
        "example/low", "example/low", "example/open_source"]
    changed = studio.post("/v1/tasks", json={**original, "prompt": "Changed original content"})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "frozen_task_changed"
    assert studio.post("/v1/tasks/original-category/resume", json={"mode": "fixed_high"}).status_code == 422

    host.clock.now += 86400
    reopened = assemble_mode_host(host.root, host.values, host.calls, mode_service.origin, clock=host.clock)
    mode_service.server.set_app(reopened.app)
    before = len(host.calls)
    assert studio.get("/v1/tasks/original-category/result").json() == result
    assert studio.post("/v1/tasks", json=original).json()["status"] == "passed"
    assert studio.post("/v1/tasks/original-category/resume", json={}).json()["status"] == "passed"
    retained = operator.get("/monitor/snapshot").json()
    retained_row = next(row for row in retained["tasks"] if row["run"] == original["run_id"])
    assert {key: retained_row[key] for key in ("client", "run", "mode", "pool", "category", "created")} == {
        key: original_row[key] for key in ("client", "run", "mode", "pool", "category", "created")}
    assert retained["control"]["routing_pool"] == host.pools[1]
    assert retained["control"]["mode"] == "open_source_first"
    assert retained["ledger"]["known_usd"] == "0.05"
    assert len(retained["ledger"]["accounts"]) == 1
    assert reopened.budget.report()["known_usd"] == "0.05"
    assert len(host.calls) == before == 5
    assert all(path.read_bytes() == content for path, content in original_bytes.items())
    assert reopened.service.retention.lookup(internal)["expires_at"] == lifecycle["expires_at"]
    assert lifecycle["expires_at"] - lifecycle["created_at"] == 30 * 86400
    mode_service.additional_workers.append({"kind": "reopened_application", "prior_mock_calls": before,
        "post_reopen_mock_calls": len(host.calls), "known_api_usd": retained["ledger"]["known_usd"],
        "original_pool": original_row["pool"], "active_pool": retained["control"]["routing_pool"],
        "original_expires_at": lifecycle["expires_at"], "immutable_receipts_preserved": True})


def test_http_interrupted_category_resumes_original_contract_and_keeps_original_expiry(mode_service):
    host = mode_service.bundle
    operator = mode_service.client("operator")
    client = mode_service.client()

    class InterruptedValidation(ArithmeticEnvironment):
        interrupted = False

        def validate(self, text):
            if not type(self).interrupted:
                type(self).interrupted = True
                raise RuntimeError("Synthetic validation interruption")
            return super().validate(text)

    original_id = "mode-cloudyfan-v1"
    original_contract = host.service.contracts[original_id]
    host.service.contracts[original_id] = replace(original_contract,
        environment_factory=lambda workspace: InterruptedValidation())
    payload = task(host, "interrupted-original-category")
    interrupted = client.post("/v1/tasks", json=payload)
    assert interrupted.status_code == 503 and interrupted.json()["status"] == "engineering_interrupted"
    assert len(host.calls) == 1
    internal = host.service.internal_id("studio", payload["run_id"])
    manifest_path = host.root / "receipts" / "runs" / internal / "manifest.json"
    job_path = host.service.root / internal / "job.json"
    manifest_bytes, job_bytes = manifest_path.read_bytes(), job_path.read_bytes()
    lifecycle = deepcopy(host.service.retention.lookup(internal))

    switch(operator, "fixed_high")
    switch(operator, "low_first", "studio")
    assert operator.post("/monitor/pools/activate", json={"pool": host.pools[1],
        "revision": operator.get("/monitor/snapshot").json()["control"]["revision"]}).status_code == 200
    host.clock.now += 2 * 86400
    reopened = assemble_mode_host(host.root, host.values, host.calls, mode_service.origin, clock=host.clock)
    reopened.service.contracts[original_id] = replace(reopened.service.contracts[original_id],
        environment_factory=lambda workspace: InterruptedValidation())
    mode_service.server.set_app(reopened.app)
    repeated = client.post("/v1/tasks", json=payload)
    assert repeated.status_code == 503 and repeated.json()["status"] == "engineering_interrupted"
    assert len(host.calls) == 1
    resumed = client.post(f"/v1/tasks/{payload['run_id']}/resume", json={})
    assert resumed.status_code == 200 and resumed.json()["status"] == "passed", resumed.json()
    outcome = client.get(f"/v1/tasks/{payload['run_id']}/result").json()
    assert outcome["answer"] == "2"
    assert outcome["provenance"]["contract_version"] == CONTRACT
    assert outcome["provenance"]["routing_version"] == "hierarchical-qcs-v1"
    assert len(host.calls) == 1
    assert reopened.budget.report()["known_usd"] == "0.01"
    assert manifest_path.read_bytes() == manifest_bytes
    assert job_path.read_bytes() == job_bytes
    assert reopened.service.retention.lookup(internal)["expires_at"] == lifecycle["expires_at"]

    host.clock.now = lifecycle["expires_at"] - 1
    assert client.get(f"/v1/tasks/{payload['run_id']}/result").status_code == 200
    host.clock.now += 1
    for method, path, body in (
        ("GET", f"/v1/tasks/{payload['run_id']}/result", None),
        ("POST", "/v1/tasks", payload),
        ("POST", f"/v1/tasks/{payload['run_id']}/resume", {}),
    ):
        expired = client.request(method, path, json=body)
        assert expired.status_code == 410 and expired.json()["error"]["code"] == "task_expired"
    assert len(host.calls) == 1
    retained = operator.get("/monitor/snapshot").json()
    assert retained["summary"]["expired"] == 1
    assert retained["summary"]["passed"] == 1
    assert retained["ledger"]["known_usd"] == "0.01"
    mode_service.additional_workers.append({"kind": "resumed_original_category_after_reopen",
        "mock_calls_before_resume": 1, "mock_calls_after_resume": len(host.calls),
        "original_expires_at": lifecycle["expires_at"], "expired_http_status": 410,
        "original_contract_version": outcome["provenance"]["contract_version"],
        "known_api_usd": retained["ledger"]["known_usd"]})


def test_http_missing_original_and_unregistered_category_reject_before_dispatch(mode_service):
    host = mode_service.bundle
    client = mode_service.client()
    operator = mode_service.client("operator")
    payload = task(host, "missing-original")
    assert client.post("/v1/tasks", json=payload).json()["status"] == "passed"
    unregistered = client.post("/v1/tasks", json={**payload, "run_id": "missing-category-mode",
        "category": "design-arena:models:website"})
    assert unregistered.status_code == 409
    assert unregistered.json()["error"]["code"] == "routing_configuration_unavailable"
    assert len(host.calls) == 1

    assert operator.post("/monitor/pools/activate", json={"pool": host.pools[1],
        "revision": operator.get("/monitor/snapshot").json()["control"]["revision"]}).status_code == 200
    reopened = assemble_mode_host(host.root, host.values, host.calls, mode_service.origin, clock=host.clock)
    registry = {key: value for key, value in reopened.app.gateway.registry.items()
                if value != "mode-cloudyfan-v1"}
    reopened.service.contracts.pop("mode-cloudyfan-v1")
    reopened.app = assemble_monitor(reopened.app.store, reopened.service, registry,
        reopened.app.identity_store, operator_token=OPERATOR, origin=mode_service.origin, demo=True)
    mode_service.server.set_app(reopened.app)
    before = len(host.calls)
    for method, path, body in (
        ("GET", "/v1/tasks/missing-original/result", None),
        ("POST", "/v1/tasks", payload),
        ("POST", "/v1/tasks/missing-original/resume", {}),
    ):
        rejected = client.request(method, path, json=body)
        assert rejected.status_code == 409, rejected.json()
        assert rejected.json()["error"]["code"] == "frozen_runtime_contract_unavailable"
    assert len(host.calls) == before == 1
    # A compatible latest CategoryCore remains available for new work; it must
    # never be used to substitute for the missing original registry identity.
    latest = client.post("/v1/tasks", json=task(host, "latest-contract-only"))
    assert latest.status_code == 200 and latest.json()["status"] == "passed", latest.json()
    assert len(host.calls) == 2
    snapshot = operator.get("/monitor/snapshot").json()
    matrix = snapshot["capabilities"]["categories"]
    assert len(matrix) == 23
    assert sum(entry["contract_registered"] for row in matrix.values() for entry in row.values()) == 4
    assert all(entry["reason"] == "routing_configuration_unavailable"
               for category, row in matrix.items() if category != host.values["category"]
               for entry in row.values())
    assert snapshot["ledger"]["known_usd"] == "0.02"
    assert len(snapshot["ledger"]["accounts"]) == 1
    mode_service.additional_workers.append({"kind": "missing_original_contract",
        "rejected_without_dispatch": 3, "calls_before_rejections": before,
        "calls_after_rejections": before, "calls_after_new_latest_task": len(host.calls),
        "registered_category_modes": 4, "unregistered_category_modes": 88,
        "known_api_usd": snapshot["ledger"]["known_usd"]})
