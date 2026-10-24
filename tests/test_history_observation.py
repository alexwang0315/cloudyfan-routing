"""Historical decisions at the approved real loopback HTTP boundary."""
import json
from contextlib import ExitStack
from datetime import timedelta
from functools import partial
import os
from pathlib import Path
import threading

import httpx
import pytest

from cloudyfan_monitor.demo import CATEGORY
from test_local_http_integration import (
    Clock, LocalService, QuietHandler, ThreadedServer,
    local_service as local_service, loopback_network as loopback_network,
)
from history_fixture import assemble_backup_host
from candidate_observation_fixture import assemble_candidate_host
from test_candidate_observation_http import candidate_http as candidate_http


@pytest.fixture
def history_service(tmp_path, loopback_network, request):
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        options = getattr(request, "param", {})
        host = assemble_backup_host(tmp_path / "service", origin, **options)
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        service = LocalService(host, origin, stack, server, Clock(host.values["now"].timestamp()))
        # Functional receipt verification, separate from the explicit long-task latency tests.
        service.client = partial(service.client, timeout=10)
        try:
            yield service
        finally:
            destination = os.environ.get("CLOUDYFAN_MONITOR_EVIDENCE_DIR")
            if destination:
                directory = Path(destination)
                directory.mkdir(parents=True, exist_ok=True)
                receipt = {"schema_version": 1, "test": request.node.name,
                    "scope": "real backup Core/BudgetedCore/TaskService/HealthStore; actual HTTP; synthetic evidence",
                    "http_responses": service.http_records,
                    "mock_upstream_calls": [{"model": call.get("model"),
                        "kind": "classification" if "questions" in call else "generation"} for call in host.calls],
                    "mock_upstream_call_count": len(host.calls),
                    "socket_attempts": [{"host": item[0], "port": item[1]} for item in loopback_network.connected],
                    "blocked_socket_attempts": [str(item) for item in loopback_network.blocked]}
                (directory / (request.node.name + ".json")).write_text(json.dumps(receipt, indent=2))
        assert not loopback_network.blocked
        assert loopback_network.connected


def create_history(service, run="history-backup"):
    host = service.bundle
    response = service.client().post("/v1/tasks", json={"run_id": run,
        "category": host.values["plan"]["category_id"], "prompt": "Return only 1+1. private-history-marker"})
    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "passed"
    snapshot = service.client("operator").get("/monitor/snapshot").json()
    return snapshot, snapshot["tasks"][0]["operator_observation"]["task_decision"]


def test_historical_direct_choice_explains_missing_calibration_and_saved_candidates(history_service):
    snapshot, history = create_history(history_service)
    assert history["status"] == "available"
    assert history["candidate_evidence"] == "saved_selection"
    assert [item["configuration"]["model_version"] for item in history["attempts"]] == [
        "example/open_source-0", "example/mid-0"]
    assert history["attempts"][1]["transition"] == "quality_recovery"
    selected = history["stages"][1]
    assert selected["stage"] == "low_mid"
    assert selected["action"] == "direct"
    assert selected["reason"] == "exact_classifier_calibration_missing"
    assert selected["economics"]["status"] == "not_provided"
    assert selected["candidates"][0]["health"]["observed_at"] is not None
    assert history["stages"][-1]["state"] == "not_entered"
    assert history["costs"]["complete_api_usd"] == "0.02"
    assert snapshot["tasks"][0]["classifier_calls"] == 0


@pytest.mark.parametrize("history_service", [{"classifiers": ("low_mid",)}], indirect=True)
def test_saved_classification_compares_complete_estimates_and_keeps_actual_fee_separate(history_service):
    snapshot, history = create_history(history_service)
    assert history["status"] == "available"
    stage = history["stages"][1]
    assert stage["action"] == "classify" and stage["classifier_called"] is True
    assert stage["reason"] == "conservative_net_saving_positive"
    assert stage["trigger"] == "public_validation_failed"
    assert stage["economics"]["direct_paths_usd"] == {
        "low": {"lower": ".20", "upper": ".20"}, "mid": {"lower": ".10", "upper": ".10"}}
    assert stage["economics"]["classifier_fee_estimate_usd"] == {"lower": ".001", "upper": ".001"}
    assert stage["economics"]["net_saving_lower_usd"] == "0.080"
    assert stage["economics"]["calibration_version"] is None
    assert stage["economics"]["calibration_evidence_sha256"] == "b" * 64
    assert stage["economics"]["classifier_contract_sha256"] is not None
    assert stage["economics"]["calibration_evidence_sha256"] != stage["economics"]["classifier_contract_sha256"]
    assert stage["classifier_api_usd"] == "0.001"
    assert history["costs"]["complete_api_usd"] == "0.021"
    assert snapshot["tasks"][0]["classifier_calls"] == 1
    classifier = [decision for decision in snapshot["tasks"][0]["trace"]["decisions"] if decision.get("model")][0]
    assert classifier["fallback"] is False
    assert classifier["probabilities"] == {"low": "0.9", "mid": "0.1"}
    assert len(history_service.bundle.calls) == 3


@pytest.mark.parametrize("history_service", [{"classifiers": ("low_mid",), "economic": False}], indirect=True)
def test_saved_insufficient_net_saving_is_distinct_from_missing_calibration(history_service):
    _, history = create_history(history_service)
    stage = history["stages"][1]
    assert stage["reason"] == "classification_not_economic"
    assert stage["action"] == "direct" and stage["classifier_called"] is False
    assert stage["economics"]["status"] == "available"
    assert stage["economics"]["net_saving_lower_usd"] == "-0.119"
    assert len(history_service.bundle.calls) == 2


@pytest.mark.parametrize("history_service", [{"classifiers": ("low_mid", "mid_high")}], indirect=True)
def test_both_quality_recovery_stages_keep_the_actual_previous_configuration(history_service):
    history_service.bundle.scenario = "quality_chain"
    snapshot, history = create_history(history_service)
    assert history["status"] == "available"
    assert [attempt["configuration"]["model_version"] for attempt in history["attempts"]] == [
        "example/open_source-0", "example/low-0", "example/high-0"]
    assert [stage["stage"] for stage in history["stages"]] == ["initial_or_availability", "low_mid", "mid_high"]
    assert history["stages"][2]["trigger"] == "public_validation_failed"
    assert history["stages"][2]["classifier_called"] is True
    assert history["costs"]["complete_api_usd"] == "0.032"
    assert snapshot["tasks"][0]["classifier_calls"] == 2
    classifiers = [decision for decision in snapshot["tasks"][0]["trace"]["decisions"] if decision["model"]]
    assert [(decision["stage"], decision["previous_tier"], decision["trigger"]) for decision in classifiers] == [
        ("low_mid", "open_source", "public_validation_failed"),
        ("mid_high", "low", "public_validation_failed")]
    assert len(history_service.bundle.calls) == 5


def test_same_pool_availability_fallback_keeps_original_health_after_new_observation(history_service):
    host = history_service.bundle
    host.scenario = "availability"
    snapshot, history = create_history(history_service)
    assert [attempt["transition"] for attempt in history["attempts"]] == ["initial", "availability_fallback"]
    assert [attempt["passed"] for attempt in history["attempts"]] == [None, True]
    fallback = history["stages"][1]
    assert fallback["trigger"] == "availability_failure"
    assert fallback["selected_tier"] == "open_source"
    assert fallback["candidates"][0]["state"] == "excluded_before_dispatch"
    assert "provider_failed_this_run" in fallback["candidates"][0]["reasons"]
    budget = host.budget.report()
    original = json.dumps(history, sort_keys=True)
    # The public health store changes now; history must retain saved assessments.
    host.values["now"] += timedelta(seconds=20)
    row = host.values["plan"]["pools"]["open_source"][0]
    observed = host.values["module"].observation(now=host.values["now"],
        provider=row["entry"]["profile"]["provider"], configuration=row["configuration_sha256"])
    observed["health"]["status"] = 4
    observed["health"]["status_meaning"] = "degraded"
    host.health.publish([observed])
    health_bytes = host.health.path.read_bytes()
    for _ in range(3):
        updated = history_service.client("operator").get("/monitor/snapshot").json()
        assert json.dumps(updated["tasks"][0]["operator_observation"]["task_decision"], sort_keys=True) == original
        assert updated["tasks"][0]["trace"] == snapshot["tasks"][0]["trace"]
    assert host.health.path.read_bytes() == health_bytes
    assert host.budget.report() == budget
    assert len(host.calls) == 2


def test_changed_saved_choice_is_unknown_without_discarding_other_verified_history(history_service):
    _, history = create_history(history_service)
    host = history_service.bundle
    internal = host.service.internal_id("studio", "history-backup")
    path = host.root / "receipts" / "runs" / internal / "backup-selection-1.json"
    saved = json.loads(path.read_text())
    saved["economic"]["reason"] = "private-upstream-error-marker"
    path.write_text(json.dumps(saved))
    budget = host.budget.report()
    response = history_service.client("operator").get("/monitor/snapshot")
    assert response.status_code == 200
    task = response.json()["tasks"][0]
    changed = task["operator_observation"]["task_decision"]
    assert changed["status"] == "partial"
    assert changed["stages"][0] == history["stages"][0]
    assert changed["stages"][1]["state"] == "unknown"
    assert task["trace"]["attempts"]
    assert "private-upstream-error-marker" not in response.text
    assert host.budget.report() == budget
    assert len(host.calls) == 2


def test_changed_saved_health_timestamp_cannot_be_presented_as_historical_evidence(history_service):
    create_history(history_service)
    host = history_service.bundle
    internal = host.service.internal_id("studio", "history-backup")
    path = host.root / "receipts" / "runs" / internal / "backup-selection-1.json"
    saved = json.loads(path.read_text())
    saved["health"]["low"][0]["assessment"]["observation"]["observed_at"] = "1999-01-01T00:00:00Z"
    path.write_text(json.dumps(saved))
    task = history_service.client("operator").get("/monitor/snapshot").json()["tasks"][0]
    history = task["operator_observation"]["task_decision"]
    assert history["status"] == "partial"
    assert history["stages"][1]["state"] == "unknown"
    assert "1999-01-01" not in json.dumps(task["operator_observation"])
    assert len(host.calls) == 2


def test_operator_reads_frozen_task_identity_without_exposing_it_to_clients(local_service):
    host = local_service.bundle
    host.upstream.fail_tiers = set()
    client, operator = local_service.client(), local_service.client("operator")
    payload = {"run_id": "history-original", "category": CATEGORY,
               "prompt": "Return only 2. private-history-prompt"}
    assert client.post("/v1/tasks", json=payload).json()["status"] == "passed"
    before = operator.get("/monitor/snapshot").json()
    task = before["tasks"][0]
    observation = task["operator_observation"]
    assert observation["schema_version"] == "mrc-observation-v1"
    history = observation["task_decision"]
    assert history["binding"] == {
        "pool_version": task["pool"], "category": CATEGORY, "mode": "cloudyfan",
        "contract_id": "demo-cloudyfan", "contract_version": "monitor-arithmetic-v1",
        "routing_version": "hierarchical-sequential-v1", "bound_at": task["created"],
    }
    assert history["status"] == "partial"
    assert history["candidate_evidence"] == "not_provided"
    assert history["attempts"][0]["configuration"]["model_version"] == "example/open_source"
    assert history["costs"]["complete_api_usd"] == "0.0001"
    assert history["costs"]["routing_estimates"] == "not_provided"
    assert "private-history-prompt" not in json.dumps(observation)
    assert str(host.root) not in json.dumps(observation)
    client_view = client.get("/v1/monitor").json()
    assert "operator_observation" not in json.dumps(client_view)
    calls = len(host.upstream.calls)
    for _ in range(3):
        repeated = operator.get("/monitor/snapshot").json()
        assert repeated["tasks"][0]["operator_observation"] == observation
    assert len(host.upstream.calls) == calls == 1


@pytest.mark.parametrize("classify", [False, True], ids=["direct", "classified"])
def test_category_qcs_history_preserves_saved_reason_and_partial_forecasts(candidate_http, classify):
    original, operator = candidate_http
    host = assemble_candidate_host(original.root / "qcs-history", str(operator.base_url).rstrip("/"),
        clock=original.clock, fail_first=True, classify=classify)
    original.server.set_app(host.app)
    with httpx.Client(base_url=operator.base_url, trust_env=False, timeout=10,
            headers={"Authorization": "Bearer " + host.credentials["studio"].token}) as client:
        created = client.post("/v1/tasks", json={"run_id": "category-history", "category": host.category,
            "prompt": "Return only 1+1. PRIVATE-CATEGORY-HISTORY"})
        assert created.status_code == 200 and created.json()["status"] == "passed"
        snapshot = operator.get("/monitor/snapshot")
        assert snapshot.status_code == 200
        task = snapshot.json()["tasks"][0]
        history = task["operator_observation"]["task_decision"]
        assert history["binding"]["routing_version"] == "hierarchical-qcs-v1"
        assert history["status"] == "partial"
        stage = history["stages"][0]
        assert stage["stage"] == "low_mid"
        assert stage["trigger"] == "public_validation_failed"
        assert stage["action"] == ("classify" if classify else "direct")
        assert stage["selected_tier"] == ("low" if classify else "mid")
        assert stage["reason"] == ("conservative_net_saving_positive" if classify else "classified_flow_estimate_unknown")
        assert stage["classifier_called"] is classify
        assert stage["economics"]["net_saving_lower_usd"] == ("0.080" if classify else "-0.01")
        assert stage["economics"]["direct_paths_usd"]["mid"] == {"lower": "0.1", "upper": "0.1"}
        if classify:
            assert stage["classifier_api_usd"] == "0.001"
            assert stage["economics"]["classified_paths_usd"]["low"] == {"lower": ".01", "upper": ".01"}
        else:
            assert stage["economics"]["classified_paths_usd"] is None
        assert stage["economics"]["calibration_version"] is None
        assert stage["economics"]["calibration_evidence_sha256"] == "a" * 64
        assert stage["candidates"][0 if classify else 1]["state"] == "dispatched"
        assert all(candidate["health"]["status"] == "not_provided" for candidate in stage["candidates"])
        assert history["costs"]["routing_estimates"] == "partial"
        assert history["costs"]["complete_api_usd"] == ("0.021" if classify else "0.02")
        assert "PRIVATE-CATEGORY-HISTORY" not in snapshot.text
        assert str(host.root) not in json.dumps(task["operator_observation"])
        budget = host.budget.report()
        for _ in range(2):
            assert operator.get("/monitor/snapshot").json()["tasks"][0]["operator_observation"] == task["operator_observation"]
        assert len(host.calls) == (3 if classify else 2) and host.budget.report() == budget
        assert "operator_observation" not in client.get("/v1/monitor").text
