"""Resilient operator observations through the approved real HTTP interface."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import uuid

import httpx
import pytest

from resilience_fixture import assemble_resilience_host
from cloudyfan_monitor.candidate_observation import CandidateObserver
from cloudyfan_monitor.history_observation import TaskDecisionObserver
from cloudyfan_monitor.integration import assemble_monitor
from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
from cloudyfan_monitor.pools import OpenRouterPoolSource
from cloudyfan_jev.demo import ArithmeticEnvironment
from test_local_http_integration import (
    Clock, LocalService, QuietHandler, ThreadedServer,
    local_service as local_service,
    loopback_network as loopback_network,
)


@pytest.fixture
def resilient_service(tmp_path, loopback_network, request):
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        host = assemble_resilience_host(tmp_path / "service", origin)
        for client in host.observation_http:
            stack.callback(client.close)
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        service = LocalService(host, origin, stack, server, Clock(host.values["now"].timestamp()))
        try:
            yield service
        finally:
            output = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
            if output:
                destination = Path(output)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / (request.node.name + ".json")).write_text(json.dumps({
                    "test": request.node.name,
                    "scope": "synthetic real Core/BudgetedCore/TaskService/Store/HealthStore; real loopback HTTP",
                    "http_responses": service.http_records,
                    "mock_upstream_call_count": len(host.calls),
                    "upstream_requests": host.requests,
                    "source_requests": host.side_requests["source"],
                    "accounting_requests": host.side_requests["accounting"],
                    "mock_upstream_requests": [{"model": call.get("model"),
                        "kind": "classification" if "questions" in call else "generation"} for call in host.calls],
                    "socket_attempts": [{"host": item[0], "port": item[1]} for item in loopback_network.connected],
                    "blocked_socket_attempts": [str(item) for item in loopback_network.blocked],
                }, indent=2))
            assert not loopback_network.blocked


def pool_read(service):
    return service.client("operator").get("/monitor/pools", params={
        "id": service.bundle.pools[0], "category": service.bundle.category, "mode": "cloudyfan"})


@pytest.mark.parametrize("damage", ["interval", "evidence_shape", "speed", "raw_hash", "valid_until"])
def test_damaged_candidate_qualification_is_unknown_without_losing_other_rows(resilient_service, damage):
    host = resilient_service.bundle
    before = pool_read(resilient_service)
    assert before.status_code == 200
    good = before.json()["operator_observation"]["candidates"]["tiers"]["open_source"][1]
    assert good["qualification"]["status"] == "evidence_recorded"
    # Corrupt only disposable controlled source data. Read behavior is observed
    # over HTTP; neither Core nor the projection is replaced by a mock.
    record = host.service.contracts["history-backup-v1"].runner.core._backup["pools"]["open_source"][0]
    if damage == "interval":
        record["evidence"]["complete_cost_usd"] = {"lower": ".2", "upper": ".1"}
    elif damage == "evidence_shape":
        record["evidence"] = ["PRIVATE_DAMAGED_EVIDENCE"]
    elif damage == "speed":
        record["evidence"]["speed"] = {"upper": "NaN"}
    else:
        record["evidence"]["provenance"]["raw_sha256" if damage == "raw_hash" else "valid_until"] = "PRIVATE_DAMAGED_EVIDENCE"
    response = pool_read(resilient_service)
    assert response.status_code == 200
    observed = response.json()["operator_observation"]["candidates"]
    assert observed["status"] == "partial"
    damaged = observed["tiers"]["open_source"][0]
    assert damaged["qualification"]["status"] == "unverifiable"
    assert damaged["qualification"]["evidence"] is None
    assert observed["tiers"]["open_source"][1] == good
    assert "PRIVATE_DAMAGED_EVIDENCE" not in response.text
    assert host.calls == []


def test_whole_contract_failure_keeps_pool_list_and_other_console_data(resilient_service):
    host = resilient_service.bundle
    before = pool_read(resilient_service).json()
    host.service.contracts["history-backup-v1"].runner.core.execution_policy = None
    response = pool_read(resilient_service)
    assert response.status_code == 200
    body = response.json()
    assert body["categories"] == before["categories"]
    observed = body["operator_observation"]["candidates"]
    assert observed["status"] == "unavailable"
    assert observed["reason"] == "candidate_observation_unavailable"
    assert observed["context"]["pool_id"] == host.pools[0]
    assert observed["tiers"] is None
    assert "execution_policy" not in json.dumps(observed)
    assert resilient_service.client("operator").get("/monitor/snapshot").status_code == 200
    assert host.calls == []


@pytest.mark.parametrize("observer_type, keyword", [
    (CandidateObserver, "candidate_observer"), (TaskDecisionObserver, "task_observer")])
def test_foreign_host_observer_cannot_be_attached_to_this_http_service(resilient_service, tmp_path,
                                                                     observer_type, keyword):
    service, host = resilient_service, resilient_service.bundle
    foreign = assemble_resilience_host(tmp_path / "foreign", service.origin)
    source = observer_type(foreign.app.store, foreign.service, foreign.app.gateway.registry) if keyword == "candidate_observer" else (
        observer_type(foreign.service, foreign.app.store, foreign.app.gateway.registry))
    with pytest.raises(ValueError, match="same host"):
        assemble_monitor(host.app.store, host.service, host.app.gateway.registry, host.app.identity_store,
            operator_token=host.operator_token, origin=service.origin, demo=True, **{keyword: source})
    assert pool_read(service).status_code == 200
    assert "foreign" not in json.dumps(pool_read(service).json()["operator_observation"])
    assert host.calls == foreign.calls == []


def create_task(service, run, principal="studio"):
    return service.client(principal, timeout=12).post("/v1/tasks", json={
        "run_id": run, "category": service.bundle.category,
        "prompt": "Return only 1+1. PRIVATE_RESILIENCE_PROMPT"})


def original_hashes(host):
    return {str(path.relative_to(host.root)): sha256(path.read_bytes()).hexdigest()
            for directory in (host.root / "receipts", host.root / "tasks")
            for path in directory.rglob("*.json") if path.is_file()}


def test_current_and_saved_observations_finish_before_pending_model_response(resilient_service):
    service, host = resilient_service, resilient_service.bundle
    host.scenario = "first_pass"
    assert create_task(service, "already-completed").status_code == 200
    entered, release = threading.Event(), threading.Event()

    def hold(_):
        entered.set()
        assert release.wait(15), "Synthetic model response was not released"

    host.before_response = hold
    with ThreadPoolExecutor(3) as workers:
        pending = workers.submit(create_task, service, "still-running", "agent")
        try:
            assert entered.wait(4)
            original, budget = original_hashes(host), host.budget.report()
            calls = len(host.calls)
            pool_future = workers.submit(pool_read, service)
            snapshot_future = workers.submit(service.client("operator").get, "/monitor/snapshot")
            assert pool_future.result(timeout=3).status_code == 200
            response = snapshot_future.result(timeout=3)
            assert response.status_code == 200
            tasks = {row["run"]: row for row in response.json()["tasks"]}
            assert tasks["already-completed"]["operator_observation"]["task_decision"]["status"] == "available"
            assert tasks["still-running"]["operator_observation"]["task_decision"]["reason"] == "historical_evidence_not_complete"
            assert not release.is_set()
            rejected = create_task(service, "no-observer-slot", "studio")
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "service_busy"
            assert original_hashes(host) == original
            assert host.budget.report() == budget
            assert len(host.calls) == calls == 2
            assert host.side_requests == {"source": [], "accounting": []}
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200


def test_observation_at_access_expiry_keeps_original_deadline_and_retained_metadata(resilient_service):
    service, host = resilient_service, resilient_service.bundle
    host.scenario = "first_pass"
    assert create_task(service, "expiry-original").status_code == 200
    operator, customer = service.client("operator"), service.client()
    original = operator.get("/monitor/snapshot").json()
    internal = host.service.internal_id("studio", "expiry-original")
    lifecycle = host.service.retention.lookup(internal)
    hashes, budget, requests = original_hashes(host), host.budget.report(), list(host.requests)
    host.values["now"] += timedelta(days=30, seconds=-1)
    assert customer.get("/v1/tasks/expiry-original/result").status_code == 200
    pool_read(service).raise_for_status()
    host.values["now"] += timedelta(seconds=1)
    for _ in range(2):
        pool_read(service).raise_for_status()
        after = operator.get("/monitor/snapshot").json()
        assert after["tasks"][0]["status"] == "expired"
        history = after["tasks"][0]["operator_observation"]["task_decision"]
        assert history["binding"]["bound_at"] == original["tasks"][0]["created"]
        assert history["reason"] == "historical_evidence_not_complete"
        assert after["retention_policy"]["evidence_min_days"] == 365
        assert customer.get("/v1/tasks/expiry-original/result").status_code == 410
        assert customer.post("/v1/tasks/expiry-original/resume", json={}).status_code == 410
    retained = host.service.retention.lookup(internal)
    assert retained["created_at"] == lifecycle["created_at"]
    assert retained["expires_at"] == lifecycle["expires_at"]
    assert after["summary"]["known_task_api_usd"] == original["summary"]["known_task_api_usd"]
    assert original_hashes(host) == hashes
    assert host.budget.report() == budget
    assert host.requests == requests
    assert host.side_requests == {"source": [], "accounting": []}


def test_invalid_executable_candidate_identity_remains_a_local_unknown(resilient_service):
    service, host = resilient_service, resilient_service.bundle
    view = host.app.store.pool(host.pools[0])
    view.pop("id")
    view["categories"][host.category]["pools"]["open_source"][0]["profile"]["reasoning_parameter"] = "unsupported"
    pool = host.app.store.save_pool(view)
    app = assemble_monitor(host.app.store, host.service, {(pool, host.category, "cloudyfan"): "history-backup-v1"},
        host.app.identity_store, operator_token=host.operator_token, origin=service.origin, demo=True)
    service.server.set_app(app)
    response = service.client("operator").get("/monitor/pools", params={"id": pool, "category": host.category})
    assert response.status_code == 200
    rows = response.json()["operator_observation"]["candidates"]["tiers"]["open_source"]
    assert rows[0]["qualification"]["status"] == "unverifiable"
    assert rows[0]["configuration"]["configuration_sha256"] is None
    assert rows[1]["qualification"]["status"] == "evidence_recorded"
    assert host.calls == []


def test_corrupt_task_history_preserves_other_task_and_never_exports_private_evidence(resilient_service):
    service, host = resilient_service, resilient_service.bundle
    host.scenario = "private_answer"
    assert create_task(service, "damaged-task").status_code == 200
    host.scenario = "first_pass"
    assert create_task(service, "good-task", "agent").status_code == 200
    operator = service.client("operator")
    before = operator.get("/monitor/snapshot").json()
    tasks = {row["run"]: row for row in before["tasks"]}
    good = tasks["good-task"]["operator_observation"]
    internal = host.service.internal_id("studio", "damaged-task")
    saved = host.root / "receipts" / "runs" / internal / "backup-selection-0.json"
    saved.write_text('{"PRIVATE_UPSTREAM_ERROR":')
    originals, budget, requests = original_hashes(host), host.budget.report(), list(host.requests)
    response = operator.get("/monitor/snapshot")
    assert response.status_code == 200
    changed = {row["run"]: row for row in response.json()["tasks"]}
    assert changed["good-task"]["operator_observation"] == good
    assert changed["damaged-task"]["operator_observation"]["task_decision"]["status"] == "partial"
    assert changed["damaged-task"]["trace"] == tasks["damaged-task"]["trace"]
    for private in ("PRIVATE_RESILIENCE_PROMPT", "PRIVATE_RESILIENCE_ANSWER", "PRIVATE_UPSTREAM_ERROR",
                    str(host.root), host.operator_token, host.credentials["studio"].token):
        assert private not in response.text
        assert private not in json.dumps(pool_read(service).json()["operator_observation"])
    assert original_hashes(host) == originals
    assert host.budget.report() == budget
    assert host.requests == requests
    assert host.side_requests == {"source": [], "accounting": []}


def test_one_bad_historical_health_row_keeps_independent_rows_visible(resilient_service):
    service, host = resilient_service, resilient_service.bundle
    assert create_task(service, "partial-history").status_code == 200
    internal = host.service.internal_id("studio", "partial-history")
    path = host.root / "receipts" / "runs" / internal / "backup-selection-1.json"
    saved = json.loads(path.read_text())
    saved["health"]["low"][1]["assessment"]["observation"]["observed_at"] = "PRIVATE_INVALID_TIMESTAMP"
    path.write_text(json.dumps(saved))
    response = service.client("operator").get("/monitor/snapshot")
    assert response.status_code == 200
    history = response.json()["tasks"][0]["operator_observation"]["task_decision"]
    assert history["status"] == "partial"
    stage = history["stages"][1]
    assert stage["state"] == "unknown"
    assert len(stage["candidates"]) == 4
    bad = next(row for row in stage["candidates"] if row["config_id"] == "low-1")
    good = next(row for row in stage["candidates"] if row["config_id"] == "low-0")
    assert bad["state"] == "unknown" and bad["health"] == {}
    assert good["state"] == "health_recorded" and good["health"]["snapshot_id"]
    assert "PRIVATE_INVALID_TIMESTAMP" not in response.text
    assert len(host.calls) == 2


def test_two_clients_scopes_and_revocation_cannot_obtain_operator_observations(resilient_service):
    service, host = resilient_service, resilient_service.bundle
    host.scenario = "first_pass"
    for principal in ("studio", "agent"):
        assert create_task(service, principal + "-owned", principal).status_code == 200
        client = service.client(principal)
        own = client.get("/v1/monitor")
        assert own.status_code == 200
        assert {task["client"] for task in own.json()["tasks"]} == {principal}
        assert "operator_observation" not in own.text
        assert "mrc-observation-v1" not in own.text
        for path in ("/monitor/snapshot", "/monitor/pools"):
            assert client.get(path).status_code == 401
    studio = service.client()
    assert studio.get("/v1/tasks/agent-owned/result").status_code == 404
    identity = host.app.identity_store
    reader = identity.issue("studio", scopes={"tasks:read"}, expires_at=host.values["now"].timestamp()+86400)
    writer = identity.issue("agent", scopes={"tasks:write"}, expires_at=host.values["now"].timestamp()+86400)
    for credential, readable in ((reader, True), (writer, False)):
        client = service.client()
        client.headers["Authorization"] = "Bearer " + credential.token
        assert client.get("/v1/monitor").status_code == (200 if readable else 401)
        assert client.get("/monitor/pools").status_code == 401
    identity.revoke(host.credentials["studio"].id)
    assert studio.get("/v1/monitor").status_code == 401
    invalid = service.client()
    invalid.headers["Authorization"] = "Bearer PRIVATE_INVALID_CREDENTIAL"
    assert invalid.get("/monitor/snapshot").status_code == 401
    assert service.client("operator").get("/monitor/snapshot").status_code == 200
    assert host.side_requests == {"source": [], "accounting": []}


@pytest.mark.parametrize("kind", ["unknown_fee", "engineering_interruption"])
def test_observation_never_resumes_or_reconciles_interrupted_tasks(local_service, kind):
    service, host = local_service, local_service.bundle
    requests = []
    def model(request):
        requests.append({"kind": "model", "method": request.method, "path": request.url.path})
        return host.upstream(request)
    def side(request):
        requests.append({"kind": "source_or_accounting", "method": request.method, "path": request.url.path})
        return httpx.Response(503, json={"error": "PRIVATE_TRANSPORT_ERROR"})
    model_http = service.stack.enter_context(httpx.Client(transport=httpx.MockTransport(model)))
    side_http = service.stack.enter_context(httpx.Client(transport=httpx.MockTransport(side)))
    host.app.gateway.service.contracts["demo-cloudyfan"].runner.core.client.http = model_http
    from test_sources import fixtures
    host.app.source = OpenRouterPoolSource(side_http, fixtures()[2])
    host.app.gateway.billing.source = OpenRouterAccounting(side_http, api_key=uuid.uuid4().hex)
    if kind == "unknown_fee":
        host.upstream.unknown_cost = True
    else:
        class Interrupted(ArithmeticEnvironment):
            def validate(self, final):
                raise RuntimeError("PRIVATE_TRANSPORT_ERROR")
        contract = host.app.gateway.service.contracts["demo-cloudyfan"]
        host.app.gateway.service.contracts["demo-cloudyfan"] = replace(contract,
            version="synthetic-resilience-interrupted-v1", environment_factory=lambda workspace: Interrupted())
    host.upstream.fail_tiers = set()
    customer, operator = service.client(), service.client("operator", timeout=10)
    submitted = customer.post("/v1/tasks", json={"run_id": "interrupted-original",
        "category": "artificial-analysis:coding_index", "prompt": "Return only 1+1. PRIVATE_INTERRUPTED_PROMPT"})
    assert submitted.status_code == (409 if kind == "unknown_fee" else 503)
    assert submitted.json()["status"] == ("accounting_interrupted" if kind == "unknown_fee" else "engineering_interrupted")
    budget_store = host.app.gateway.service.contracts["demo-cloudyfan"].runner.store
    budget, originals, original_requests = budget_store.report(), original_hashes(host), list(requests)
    for _ in range(3):
        response = operator.get("/monitor/snapshot")
        assert response.status_code == 200
        task = response.json()["tasks"][0]
        assert task["complete_api_usd"] == (None if kind == "unknown_fee" else "0.0001")
        assert task["operator_observation"]["task_decision"]["reason"] == "historical_evidence_not_complete"
        assert "PRIVATE_INTERRUPTED_PROMPT" not in response.text
        assert "PRIVATE_TRANSPORT_ERROR" not in response.text
        assert operator.get("/monitor/pools").status_code == 200
    service.clock.now += 30 * 86400
    assert customer.get("/v1/tasks/interrupted-original/result").status_code == 410
    preview = operator.post("/monitor/retention/preview", json={"client": "studio", "run_id": "interrupted-original"})
    assert preview.status_code == 200
    assert preview.json()["policy"]["evidence_min_days"] == 365
    if kind == "unknown_fee":
        assert budget["unknown_events"] == 1 and budget["dispatch_blocked"] is True
        assert "unknown_accounting" in preview.json()["blockers"]
    assert requests == original_requests
    assert original_hashes(host) == originals
    assert budget_store.report() == budget
    service.additional_workers.append({"kind": "interrupted_observation_transport",
        "requests_before_observation": original_requests, "requests_after_observation": requests})
