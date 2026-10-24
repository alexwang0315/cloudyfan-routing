"""Current health through the approved operator-only real loopback HTTP seam."""
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import sqlite3
import threading

import httpx
import pytest

from candidate_health_fixture import assemble_health_host
from cloudyfan_monitor.integration import assemble_monitor
from test_local_http_integration import (
    Clock, QuietHandler, ThreadedServer,
    loopback_network as loopback_network,
)


@pytest.fixture
def health_http(tmp_path, loopback_network, request):
    with ExitStack() as stack:
        server = ThreadedServer(("127.0.0.1", 0), QuietHandler)
        stack.callback(server.server_close)
        loopback_network.ports.add(server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        clock = Clock(datetime.fromisoformat("2026-10-21T00:00:00+00:00").timestamp())
        host = assemble_health_host(tmp_path / "host", origin, clock=clock, **getattr(request, "param", {}))
        host.server = server
        server.set_app(host.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        stack.callback(thread.join, 2)
        stack.callback(server.shutdown)
        operator = stack.enter_context(httpx.Client(base_url=origin, trust_env=False, timeout=3,
            headers={"Authorization": "Bearer " + host.operator_token}))
        yield host, operator


def candidate_view(operator, host, **query):
    response = operator.get("/monitor/pools", params={"id": host.pools[0],
        "category": host.category, "mode": "cloudyfan", **query})
    assert response.status_code == 200
    return response.json()["operator_observation"]["candidates"]


def test_operator_can_verify_current_exact_health_separately_from_qualification(health_http):
    host, operator = health_http
    before = hashlib.sha256(host.health.path.read_bytes()).hexdigest()
    budget = host.budget.report()
    candidate = candidate_view(operator, host)["tiers"]["open_source"][0]
    health = candidate["health"]
    assert health["status"] == "healthy", "Operator cannot inspect the controlled HealthStore assessment"
    assert health["policy_eligible"] is True
    assert health["freshness"] == "fresh"
    assert health["observed_at"] == "2026-10-21T00:00:00+00:00"
    assert health["assessed_at"] == "2026-10-21T00:00:00+00:00"
    assert len(health["snapshot_id"]) == 64
    assert len(health["policy_sha256"]) == 64
    assert health["configuration_sha256"] == candidate["configuration"]["configuration_sha256"]
    assert health["provider"] == "provider-0"
    assert health["ttl_seconds"] == 300
    assert health["circuit"] == {"open": False, "failures": 0, "last_failure_at": None, "cooldown_until": None}
    assert health["reason_codes"] == []
    assert health["endpoint_metrics"]["latency_last_30m"] == {"p50": 1424, "p99": 9234}
    assert "p95" not in health["endpoint_metrics"]["latency_last_30m"]
    assert candidate["qualification"]["formal_status"] == "unknown"
    assert candidate["quota"]["status"] == "unknown"
    assert candidate["dispatch_authorization"]["status"] == "not_checked"
    assert host.calls == []
    assert host.budget.report() == budget
    assert hashlib.sha256(host.health.path.read_bytes()).hexdigest() == before
    with httpx.Client(base_url=operator.base_url, trust_env=False,
        headers={"Authorization": "Bearer " + host.credentials["studio"].token}) as customer:
        own = customer.get("/v1/monitor")
        assert own.status_code == 200
        assert "operator_observation" not in json.dumps(own.json())
        assert customer.get("/monitor/pools").status_code == 401


def test_current_health_expires_at_original_ttl_without_renewing_qualification(health_http):
    host, operator = health_http
    original = candidate_view(operator, host)["tiers"]["open_source"][0]
    host.clock.now += 299
    fresh = candidate_view(operator, host)["tiers"]["open_source"][0]
    assert fresh["health"]["status"] == "healthy"
    host.clock.now += 1
    stale = candidate_view(operator, host)["tiers"]["open_source"][0]
    assert stale["health"]["status"] == "stale", "The original health TTL must expire at age == 300 seconds"
    assert stale["health"]["policy_eligible"] is False
    assert stale["health"]["freshness"] == "stale"
    assert stale["health"]["reason_codes"] == ["observation_stale"]
    assert stale["health"]["observed_at"] == original["health"]["observed_at"]
    assert stale["health"]["assessed_at"] == "2026-10-21T00:05:00+00:00"
    assert stale["health"]["snapshot_id"] == original["health"]["snapshot_id"]
    assert stale["qualification"] == original["qualification"]
    assert host.calls == []


def test_elapsed_cooldown_stays_open_until_explicit_fresh_recovery(health_http):
    host, operator = health_http
    identity = host.observation()
    configuration, provider = identity["configuration_sha256"], identity["provider"]
    for number in (1, 2):
        host.health.record(f"synthetic-availability-failure-{number}", configuration, provider)
    cooling = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
    assert cooling["status"] == "cooldown", "An open circuit must show its original cooldown"
    assert cooling["reason_codes"] == ["cooldown_active"]
    assert cooling["circuit"]["cooldown_until"] == "2026-10-21T00:01:00+00:00"
    assert cooling["circuit"]["failures"] == 2
    assert cooling["circuit"]["open"] is True
    host.clock.now += 60
    waiting = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
    assert waiting["status"] == "recovery_required"
    assert waiting["freshness"] == "fresh"
    assert waiting["reason_codes"] == ["recovery_required"]
    newer = host.health.publish([host.observation()])
    before_reads = hashlib.sha256(host.health.path.read_bytes()).hexdigest()
    for _ in range(3):
        pending = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
        assert pending["status"] == "recovery_required"
        assert pending["policy_eligible"] is False
        assert pending["circuit"]["open"] is True
        assert pending["snapshot_id"] == newer
    assert hashlib.sha256(host.health.path.read_bytes()).hexdigest() == before_reads
    # Only controlled fixture setup performs recovery; no product read does it.
    host.health.recover(configuration, provider, newer)
    recovered = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
    assert recovered["status"] == "healthy"
    assert recovered["policy_eligible"] is True
    assert recovered["circuit"]["open"] is False
    assert host.calls == []


@pytest.mark.parametrize("health_http", [{"publish_initial": False}], indirect=True)
def test_missing_exact_provider_health_and_all_original_degradation_reasons_are_visible(health_http):
    host, operator = health_http
    missing = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
    assert missing["status"] == "missing", "Missing health must stay unknown rather than fail the entire candidate view"
    assert missing["freshness"] == "missing"
    assert missing["policy_eligible"] is False
    assert missing["snapshot_id"] is None and missing["observed_at"] is None
    assert missing["reason_codes"] == ["health_observation_missing"]
    foreign = host.observation()
    foreign["provider"] = "another-provider"
    host.health.publish([foreign])
    assert candidate_view(operator, host)["tiers"]["open_source"][0]["health"] == missing
    degraded = host.observation()
    degraded["health"].update(status=4, status_meaning="degraded", uptime_last_5m=98)
    degraded["speed"].update(latency_unit="PRIVATE-HEALTH-UNIT", throughput_last_30m={"p99": 100})
    snapshot = host.health.publish([degraded])
    response = candidate_view(operator, host)
    health = response["tiers"]["open_source"][0]["health"]
    assert health["status"] == "degraded"
    assert health["freshness"] == "fresh"
    assert health["snapshot_id"] == snapshot
    assert health["policy_eligible"] is False
    assert health["reason_codes"] == ["status_degraded", "uptime_last_5m_below_minimum",
                                      "latency_unit_unknown_or_invalid", "throughput_p50_unknown"]
    assert "PRIVATE-HEALTH-UNIT" not in json.dumps(response)
    assert health["endpoint_metrics"]["throughput_last_30m"] == {"p99": 100}
    assert host.calls == []


def test_one_unreadable_health_batch_does_not_turn_other_candidates_healthy_or_hide_them(health_http):
    host, operator = health_http
    row = host.observation()
    # Corrupt only disposable source evidence by appending a bad claimed batch.
    # The next real HealthStore.assess verifies its original hash and refuses it.
    with sqlite3.connect(host.health.path) as db:
        db.execute("INSERT INTO snapshots(id,payload) VALUES (?,?)", ("f" * 64,
            json.dumps({"schema_version": "provider-health-v1", "private": "PRIVATE-HEALTH-ERROR /private/health"})))
        db.execute("INSERT INTO observations VALUES (?,?,?,?)", (
            row["configuration_sha256"], row["provider"], "f" * 64, json.dumps(row)))
    health_digest = hashlib.sha256(host.health.path.read_bytes()).hexdigest()
    observed = candidate_view(operator, host)
    affected = observed["tiers"]["open_source"][0]["health"]
    assert affected["status"] == "unavailable"
    assert affected["policy_eligible"] is None
    assert affected["reason_codes"] == ["health_evidence_unavailable"]
    assert "snapshot_id" not in affected or affected["snapshot_id"] is None
    assert observed["tiers"]["open_source"][1]["health"]["status"] == "healthy"
    assert observed["tiers"]["mid"][0]["health"]["status"] == "healthy"
    assert "PRIVATE-HEALTH-ERROR" not in json.dumps(observed)
    assert "/private/health" not in json.dumps(observed)
    assert str(host.root) not in json.dumps(observed)
    assert hashlib.sha256(host.health.path.read_bytes()).hexdigest() == health_digest
    assert host.calls == []


def test_health_cannot_be_borrowed_across_configuration_category_or_policy(health_http):
    host, operator = health_http
    view = host.app.store.pool(host.pools[0])
    view.pop("id")
    other_category = "design-arena:models:website"
    view["categories"][other_category]["pools"]["open_source"][0] = json.loads(json.dumps(
        view["categories"][host.category]["pools"]["open_source"][0]))
    view["categories"][host.category]["pools"]["open_source"][0]["profile"]["provider"] = "another-provider"
    changed = host.app.store.save_pool(view)
    registry = {**host.app.gateway.registry, (changed, host.category, "cloudyfan"): "health-backup-v1",
        (changed, other_category, "cloudyfan"): "health-backup-v1"}
    app = assemble_monitor(host.app.store, host.service, registry, host.app.identity_store,
        operator_token=host.operator_token, origin=str(operator.base_url).rstrip("/"), demo=True)
    host.server.set_app(app)
    for category in (host.category, other_category):
        candidate = candidate_view(operator, host, id=changed, category=category)["tiers"]["open_source"][0]
        assert candidate["health"]["status"] == "unverifiable"
        assert candidate["health"]["reason_codes"] == ["health_identity_unverifiable"]
        assert "snapshot_id" not in candidate["health"]
    assert candidate_view(operator, host)["tiers"]["open_source"][0]["health"]["status"] == "healthy"
    unregistered = candidate_view(operator, host, id=host.pools[1])["tiers"]["open_source"][0]
    assert unregistered["health"]["status"] == "not_provided"
    assert unregistered["registration"]["status"] == "not_registered"
    host.health.policy["ttl_seconds"] = 301
    policy_mismatch = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
    assert policy_mismatch["status"] == "unverifiable"
    assert policy_mismatch["reason_codes"] == ["health_policy_unverifiable"]
    assert "snapshot_id" not in policy_mismatch
    assert host.calls == []


def test_source_future_health_does_not_become_fresh_proof_by_waiting(health_http):
    host, operator = health_http
    observation = host.observation()
    observation["observed_at"] = "2026-10-21T00:00:01+00:00"
    snapshot = host.health.publish([observation])
    for seconds in (0, 2):
        host.clock.now += seconds
        health = candidate_view(operator, host)["tiers"]["open_source"][0]["health"]
        assert health["status"] == "unverifiable"
        assert health["freshness"] == "future"
        assert health["snapshot_id"] == snapshot
        assert health["reason_codes"] == ["observation_in_future"]
        assert health["policy_eligible"] is False
    assert host.calls == []


def test_health_reads_preserve_original_task_evidence_costs_and_access_expiry(health_http):
    host, operator = health_http
    task = {"run_id": "health-read-original", "category": host.category,
            "prompt": "Return only 1+1. PRIVATE-HEALTH-TASK"}
    with httpx.Client(base_url=operator.base_url, trust_env=False, timeout=3,
        headers={"Authorization": "Bearer " + host.credentials["studio"].token}) as customer:
        created = customer.post("/v1/tasks", json=task)
        assert created.status_code == 200 and created.json()["status"] == "passed"
        original = customer.get("/v1/tasks/health-read-original/result").json()
        internal = host.service.internal_id("studio", task["run_id"])
        evidence_root = host.root / "receipts" / "runs" / internal
        originals = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in evidence_root.rglob("*") if path.is_file()}
        originals[host.service.root / internal / "job.json"] = hashlib.sha256(
            (host.service.root / internal / "job.json").read_bytes()).hexdigest()
        assert len(originals) >= 3
        health_digest = hashlib.sha256(host.health.path.read_bytes()).hexdigest()
        budget, control = host.budget.report(), host.app.store.control()
        for _ in range(3):
            observed = candidate_view(operator, host)
            assert "PRIVATE-HEALTH-TASK" not in json.dumps(observed)
            assert str(host.root) not in json.dumps(observed)
            assert observed["tiers"]["open_source"][0]["health"]["status"] == "healthy"
            operator.get("/monitor/snapshot").raise_for_status()
        assert customer.get("/v1/tasks/health-read-original/result").json() == original
        host.clock.now += 30 * 86400
        assert candidate_view(operator, host)["tiers"]["open_source"][0]["health"]["status"] == "stale"
        assert customer.get("/v1/tasks/health-read-original/result").status_code == 410
        assert customer.post("/v1/tasks", json=task).status_code == 410
        summary = operator.get("/monitor/snapshot").json()["summary"]
        assert summary["passed"] == 1 and summary["known_api_usd"] == "0.01"
        assert host.budget.report() == budget
        assert host.app.store.control() == control
        assert len(host.calls) == 1 and host.calls[0]["method"] == "POST"
        assert hashlib.sha256(host.health.path.read_bytes()).hexdigest() == health_digest
        assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())
