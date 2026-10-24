"""Operator candidate observations at the approved real loopback HTTP seam."""
import json
from contextlib import ExitStack
from datetime import datetime
import threading

import httpx
import pytest

from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import assemble_monitor

from test_local_http_integration import (
    Clock, QuietHandler, ThreadedServer,
    local_service as local_service,
    loopback_network as loopback_network,
)
from candidate_observation_fixture import assemble_candidate_host


@pytest.fixture
def candidate_http(tmp_path, loopback_network):
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        host = assemble_candidate_host(tmp_path / "host", origin, clock=clock)
        host.server = server
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        operator = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=3,
            headers={"Authorization": "Bearer " + host.operator_token}))
        yield host, operator


def test_operator_distinguishes_candidate_registration_from_qualification(local_service):
    operator = local_service.client("operator")
    before = operator.get("/monitor/snapshot").json()
    calls_before = len(local_service.bundle.upstream.calls)

    response = operator.get("/monitor/pools", params={"category": CATEGORY, "mode": "cloudyfan"})
    assert response.status_code == 200
    pool = response.json()
    assert "operator_observation" in pool, "The operator cannot inspect candidate evidence through the pool page"
    assert pool["operator_observation"]["schema_version"] == "mrc-observation-v1"
    observed = pool["operator_observation"]["candidates"]
    assert observed["context"]["pool_id"] == before["control"]["catalog_pool"]
    assert observed["context"]["active"] is True
    assert observed["context"]["category"] == CATEGORY
    assert observed["context"]["mode"] == "cloudyfan"
    assert observed["context"]["contract_registered"] is True
    candidate = observed["tiers"]["open_source"][0]
    assert candidate["configuration"]["model"] == "example/open_source"
    assert candidate["registration"]["status"] == "registered"
    assert candidate["qualification"]["status"] == "not_provided"
    assert candidate["qualification"]["formal_status"] == "unknown"
    assert candidate["scope"] == "synthetic"
    assert candidate["health"]["status"] == "not_provided"
    assert candidate["quota"]["status"] == "unknown"
    assert candidate["dispatch_authorization"]["status"] == "not_checked"
    assert observed["tiers"]["open_source"][1]["listing_status"] == "missing"
    assert len(observed["tiers"]["open_source"]) == 3

    for _ in range(2):
        operator.get("/monitor/pools", params={"category": CATEGORY, "mode": "cloudyfan"}).raise_for_status()
    after = operator.get("/monitor/snapshot").json()
    assert after["control"] == before["control"]
    assert after["ledger"] == before["ledger"]
    assert len(local_service.bundle.upstream.calls) == calls_before
    own = local_service.client().get("/v1/monitor").json()
    assert "operator_observation" not in own
    assert "mrc-observation-v1" not in json.dumps(own)
    denied = local_service.client().get("/monitor/pools")
    assert denied.status_code == 401


def test_operator_sees_qcs_evidence_scope_without_promoting_synthetic_candidates(candidate_http):
    host, operator = candidate_http
    response = operator.get("/monitor/pools", params={"id": host.pools[0],
        "category": host.category, "mode": "cloudyfan"})
    assert response.status_code == 200
    observed = response.json()["operator_observation"]["candidates"]
    candidate = observed["tiers"]["open_source"][0]
    qualification = candidate["qualification"]
    assert qualification["status"] == "evidence_recorded"
    assert qualification["formal_status"] == "unknown"
    assert candidate["scope"] == "synthetic"
    assert qualification["evidence"]["category_id"] == host.category
    assert qualification["evidence"]["workload_id"] == "arithmetic-small"
    assert qualification["evidence"]["complete_cost_usd"] == {"lower": "0.1", "upper": "0.1"}
    assert qualification["evidence"]["speed"] == {"metric": "flow_ms", "statistic": "p95", "upper": "100"}
    assert qualification["evidence"]["valid_until"] == "2026-10-22T12:00:00+00:00"
    assert qualification["evidence"]["quality_sufficient"] is True
    proposed = observed["tiers"]["low"][1]
    assert proposed["identity"]["missing_fields"] == ["model_version", "effort", "provider"]
    assert proposed["registration"]["candidate_matches_contract"] is False
    assert proposed["qualification"]["status"] == "not_provided"
    assert proposed["dispatch_authorization"]["status"] == "not_checked"
    assert {v["pool_id"] for v in observed["versions"]} >= set(host.pools)
    inactive = operator.get("/monitor/pools", params={"id": host.pools[1],
        "category": host.category, "mode": "cloudyfan"}).json()["operator_observation"]["candidates"]
    assert inactive["context"]["active"] is False
    assert inactive["context"]["contract_registered"] is False
    assert inactive["tiers"]["open_source"][0]["qualification"]["status"] == "not_provided"
    assert host.calls == []


def test_registered_contract_cannot_supply_another_categories_qualification(candidate_http):
    host, operator = candidate_http
    view = host.app.store.pool(host.pools[0])
    view.pop("id")
    other_category = "design-arena:models:website"
    view["categories"][other_category]["pools"]["open_source"][0] = view["categories"][host.category]["pools"]["open_source"][0]
    other_pool = host.app.store.save_pool(view)
    registry = {**host.app.gateway.registry, (other_pool, other_category, "cloudyfan"): "category-host-v1"}
    app = assemble_monitor(host.app.store, host.service, registry, host.app.identity_store,
        operator_token=host.operator_token, origin=str(operator.base_url).rstrip("/"), demo=True)
    host.server.set_app(app)
    response = operator.get("/monitor/pools", params={"id": other_pool,
        "category": other_category, "mode": "cloudyfan"})
    assert response.status_code == 200
    candidate = response.json()["operator_observation"]["candidates"]["tiers"]["open_source"][0]
    assert candidate["registration"]["status"] == "registered"
    assert candidate["qualification"]["status"] == "unverifiable"
    assert candidate["qualification"]["evidence"] is None
    assert "qualification_category_mismatch" in candidate["reason_codes"]
    assert host.calls == []


def test_candidate_query_cannot_select_private_sources_or_ambiguous_categories(candidate_http):
    host, operator = candidate_http
    for params in ({"evidence_path": "/private/synthetic-secret"}, {"category": "search"},
                   {"mode": "not-a-mode"}, [("mode", "cloudyfan"), ("mode", "fixed_high")]):
        denied = operator.get("/monitor/pools", params=params)
        assert denied.status_code == 422
        assert "synthetic-secret" not in denied.text
    response = operator.get("/monitor/pools", params={"category": host.category, "mode": "cloudyfan"})
    projection = json.dumps(response.json()["operator_observation"])
    assert str(host.root) not in projection
    assert "receipt_root" not in projection
    assert "budget_store" not in projection
    assert "capability_card" not in projection
    assert "source_url" not in projection
    assert host.calls == []


def test_preparation_exclusions_keep_the_saved_identifier_without_inventing_a_profile(candidate_http):
    host, operator = candidate_http
    replacement = assemble_candidate_host(host.root / "excluded", str(operator.base_url).rstrip("/"),
                                          clock=host.clock, exclude_low=True)
    host.server.set_app(replacement.app)
    response = operator.get("/monitor/pools", params={"category": replacement.category, "mode": "cloudyfan"})
    assert response.status_code == 200
    observed = response.json()["operator_observation"]["candidates"]
    assert observed["excluded"] == [{"tier": "low", "config_id": "low",
                                      "reason_codes": ["quality_evidence_insufficient"]}]
    assert "profile" not in observed["excluded"][0]
    assert observed["tiers"]["low"][0]["listing_status"] == "missing"
    assert replacement.calls == []


def test_candidate_reads_preserve_original_task_cost_and_expiry_without_exposing_private_inputs(candidate_http):
    host, operator = candidate_http
    marker = "SYNTHETIC_PRIVATE_TASK_MARKER_NOT_PUBLIC"
    task = {"run_id": "original-candidate-task", "category": host.category,
            "prompt": "Return only 1+1. " + marker}
    with httpx.Client(base_url=operator.base_url, trust_env=False, timeout=3,
                     headers={"Authorization": "Bearer " + host.credentials["studio"].token}) as client:
        created = client.post("/v1/tasks", json=task)
        assert created.status_code == 200 and created.json()["status"] == "passed"
        original = client.get("/v1/tasks/" + task["run_id"] + "/result").json()
        before = operator.get("/monitor/snapshot").json()
        calls = len(host.calls)
        for _ in range(3):
            response = operator.get("/monitor/pools", params={"category": host.category, "mode": "cloudyfan"})
            assert response.status_code == 200
            assert marker not in response.text
            assert str(host.root) not in json.dumps(response.json()["operator_observation"])
        assert client.get("/v1/tasks/" + task["run_id"] + "/result").json() == original
        host.clock.now += 30 * 86400
        current = operator.get("/monitor/pools", params={"category": host.category, "mode": "cloudyfan"}).json()
        assert current["operator_observation"]["candidates"]["tiers"]["open_source"][0]["qualification"]["status"] == "expired"
        expired = client.get("/v1/tasks/" + task["run_id"] + "/result")
        assert expired.status_code == 410
        assert client.post("/v1/tasks", json=task).status_code == 410
        after = operator.get("/monitor/snapshot").json()
        assert after["control"] == before["control"]
        assert after["ledger"] == before["ledger"]
        assert after["summary"]["passed"] == 1
        assert marker not in json.dumps(after)
        assert len(host.calls) == calls == 1


def test_future_dated_qualification_is_not_presented_as_current_evidence(candidate_http):
    host, operator = candidate_http
    host.clock.now -= 1
    response = operator.get("/monitor/pools", params={"category": host.category, "mode": "cloudyfan"})
    assert response.status_code == 200
    candidate = response.json()["operator_observation"]["candidates"]["tiers"]["open_source"][0]
    assert candidate["qualification"]["status"] == "unverifiable"
    assert "qualification_evidence_from_future" in candidate["reason_codes"]
    assert host.calls == []
