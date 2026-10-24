"""Capacity sources at the approved real loopback HTTP seam; no live suppliers."""
from test_local_http_integration import (
    local_service as local_service,
    loopback_network as loopback_network,
)
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

import pytest

from capacity_sources_fixture import MockCapacitySources, attach_capacity, configuration, limit


def test_unconfigured_capacity_view_is_operator_only_and_does_not_change_dispatch(local_service):
    operator = local_service.client("operator")
    before = operator.get("/monitor/snapshot").json()
    response = operator.get("/monitor/capacity")
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "capacity-sources-v1", "configured": False,
        "execution_enabled": False, "configuration_version": None,
        "bindings": [], "groups": [], "reason_codes": ["capacity_not_configured"],
    }
    for _ in range(2):
        assert operator.get("/monitor/capacity").json() == response.json()
    assert local_service.client().get("/monitor/capacity").status_code == 401
    own = local_service.client().get("/v1/monitor").json()
    assert "capacity" not in own
    after = operator.get("/monitor/snapshot").json()
    assert after["control"] == before["control"]
    assert after["ledger"] == before["ledger"]
    assert local_service.bundle.upstream.calls == []


def test_explicit_bounded_key_refresh_saves_separate_dollars_and_retains_partial_success(local_service):
    upstream = MockCapacitySources()
    observer = attach_capacity(local_service.bundle, local_service.clock, sources=upstream.configured())
    local_service.server.set_app(local_service.bundle.app)
    operator = local_service.client("operator")
    original = operator.get("/monitor/snapshot").json()
    assert upstream.requests == []
    refreshed = operator.post("/monitor/capacity/refresh", json={})
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "available"
    assert refreshed.json()["request_count"] == 2
    assert [(row["method"], row["path"]) for row in upstream.requests] == [("GET", "/api/v1/key")] * 2
    assert all(row["timeout"]["read"] <= refreshed.json()["max_seconds"] for row in upstream.requests)
    key = next(group for group in refreshed.json()["snapshot"]["groups"] if group["id"] == "key-a")
    usd = next(item for item in key["supplier_limits"] if item["dimension"] == "usd")
    assert (usd["value"], usd["remaining"], usd["usage"]) == ("20", "16", "4")
    assert usd["reset_at"] is None and usd["reset_period"] == "daily"
    assert usd["valid_until_basis"] == "local_freshness"
    assert usd["include_byok_in_limit"] is False
    assert {item["status"] for item in key["supplier_limits"] if item["dimension"] != "usd"} == {"unknown"}
    first_observed = usd["observed_at"]
    local_service.clock.now += 5
    upstream.failure.add("credential-a")
    partial = operator.post("/monitor/capacity/refresh", json={}).json()
    assert partial["status"] == "partial"
    key = next(group for group in partial["snapshot"]["groups"] if group["id"] == "key-a")
    assert key["retained_after_refresh_failure"] is True
    assert key["last_refresh"]["status"] == "unavailable"
    assert key["last_refresh"]["error_code"] == "openrouter_accounting_forbidden"
    assert next(item for item in key["supplier_limits"] if item["dimension"] == "usd")["observed_at"] == first_observed
    assert len(key["refresh_history"]) == 2
    digest = hashlib.sha256(observer.store.path.read_bytes()).hexdigest()
    for _ in range(3):
        visible = operator.get("/monitor/capacity")
        assert visible.status_code == 200
        assert "PRIVATE_" not in visible.text
        assert all(secret not in visible.text for secret in upstream.credentials)
        assert str(local_service.bundle.root) not in visible.text
    assert hashlib.sha256(observer.store.path.read_bytes()).hexdigest() == digest
    assert len(upstream.requests) == 4
    after = operator.get("/monitor/snapshot").json()
    assert after["control"] == original["control"] and after["ledger"] == original["ledger"]
    assert local_service.bundle.upstream.calls == []
    assert "capacity-sources-v1" not in json.dumps(local_service.client().get("/v1/monitor").json())


def test_operator_can_inspect_imported_shared_limits_without_enabling_execution(local_service):
    attach_capacity(local_service.bundle, local_service.clock)
    local_service.server.set_app(local_service.bundle.app)
    operator = local_service.client("operator")
    before = operator.get("/monitor/snapshot").json()
    view = operator.get("/monitor/capacity").json()
    assert view["configuration_version"] == "synthetic-capacity-v1"
    assert view["execution_enabled"] is False
    assert view["bindings"][0]["groups"][0] == view["bindings"][1]["groups"][0] == "account-shared"
    shared = next(group for group in view["groups"] if group["id"] == "account-shared")
    supplier = {item["dimension"]: item for item in shared["supplier_limits"]}
    local = {item["dimension"]: item for item in shared["local_limits"]}
    assert supplier["rpm"]["value"] == 120
    assert supplier["rpm"]["unit"] == "requests"
    assert supplier["rpm"]["window_seconds"] == 60
    assert supplier["tpm"]["value"] == 30000
    assert supplier["concurrency"]["status"] == "unknown"
    assert local["rpm"]["value"] == 10
    assert local["concurrency"]["value"] == 2
    assert shared["source"]["kind"] == "controlled_import"
    assert supplier["rpm"]["observed_at"] == local_service.clock.now
    assert supplier["rpm"]["reset_at"] == local_service.clock.now + 60
    assert supplier["rpm"]["valid_until"] == local_service.clock.now + 300
    assert operator.post("/monitor/capacity/refresh", json={"api_key": uuid4().hex}).status_code == 422
    assert operator.get("/monitor/capacity", params={"source": "/private/forbidden"}).status_code == 422
    after = operator.get("/monitor/snapshot").json()
    assert after["control"] == before["control"]
    assert after["ledger"] == before["ledger"]
    assert local_service.bundle.upstream.calls == []


def test_source_uncertainty_preserves_original_evidence_instead_of_available_capacity(local_service):
    now = local_service.clock.now
    config = configuration(now)
    shared = config["groups"][0]
    shared["supplier_limits"] = [
        limit("rpm", 120, now), limit("rpm", 121, now),
        limit("tpm", 30000, now, scope={"kind": "account", "ref": "another-account"}),
        limit("concurrency", 9, now - 400),
        limit("usd", None, now, status="not_applicable"),
    ]
    observer = attach_capacity(local_service.bundle, local_service.clock, config=config)
    local_service.server.set_app(local_service.bundle.app)
    operator = local_service.client("operator")
    response = operator.get("/monitor/capacity")
    assert response.status_code == 200
    group = next(group for group in response.json()["groups"] if group["id"] == "account-shared")
    limits = {item["dimension"]: item for item in group["supplier_limits"]}
    assert limits["rpm"]["status"] == "contradictory"
    assert limits["rpm"]["value"] is None
    assert [item["value"] for item in limits["rpm"]["observations"]] == [120, 121]
    assert limits["tpm"]["status"] == "mismatched"
    assert limits["concurrency"]["status"] == "stale"
    assert limits["concurrency"]["observed_at"] == now - 400
    assert limits["usd"]["status"] == "not_applicable"
    unsupported = next(group for group in response.json()["groups"] if group["id"] == "unsupported-group")
    assert unsupported["supplier_limits"][0]["status"] == "unsupported"
    assert next(item for item in unsupported["local_limits"] if item["dimension"] == "concurrency")["status"] == "not_applicable"
    config["groups"][0]["local_limits"][0]["value"] = 999
    with pytest.raises(ValueError, match="cannot change"):
        observer.store.configure(config)
    assert operator.get("/monitor/capacity").json() == response.json()
    local_service.clock.now = now - 1
    backwards = operator.get("/monitor/capacity").json()
    group = next(group for group in backwards["groups"] if group["id"] == "account-shared")
    assert group["local_limits"][0]["status"] == "unverifiable"
    assert "capacity_observation_from_future" in group["local_limits"][0]["reason_codes"]
    local_service.clock.now = float("nan")
    invalid = operator.get("/monitor/capacity")
    assert invalid.status_code == 200
    assert invalid.json()["reason_codes"] == ["capacity_clock_invalid"]
    assert local_service.bundle.upstream.calls == []


def test_refresh_deadline_and_request_budget_do_not_accept_late_source_evidence(local_service):
    upstream = MockCapacitySources()
    elapsed = [0]
    upstream.before_response = lambda _: elapsed.__setitem__(0, elapsed[0] + 2)
    attach_capacity(local_service.bundle, local_service.clock, sources=upstream.configured(),
                    max_requests=1, max_seconds=1, monotonic=lambda: elapsed[0])
    local_service.server.set_app(local_service.bundle.app)
    operator = local_service.client("operator")
    result = operator.post("/monitor/capacity/refresh", json={})
    assert result.status_code == 200
    assert result.json()["request_count"] == 1
    assert result.json()["status"] == "unavailable"
    groups = {group["id"]: group for group in result.json()["snapshot"]["groups"]}
    assert groups["key-a"]["last_refresh"]["error_code"] == "capacity_refresh_deadline_exceeded"
    assert groups["key-b"]["last_refresh"]["error_code"] == "capacity_refresh_budget_exhausted"
    assert next(item for item in groups["key-a"]["supplier_limits"] if item["dimension"] == "usd")["status"] == "unknown"
    assert len(upstream.requests) == 1
    local_service.clock.now -= 1
    rejected = operator.post("/monitor/capacity/refresh", json={})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "capacity_clock_backwards"
    assert len(upstream.requests) == 1


def test_capacity_reads_stay_responsive_and_keep_original_busy_task_contract(local_service):
    upstream = MockCapacitySources()
    attach_capacity(local_service.bundle, local_service.clock, sources=upstream.configured())
    local_service.server.set_app(local_service.bundle.app)
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    entered, release = threading.Event(), threading.Event()

    def hold(_):
        entered.set()
        assert release.wait(8)

    bundle.upstream.before_response = hold
    client = local_service.client(timeout=10)
    operator, second = local_service.client("operator"), local_service.client("agent")
    task = {"run_id": "capacity-still-synchronous", "category": "artificial-analysis:coding_index",
            "prompt": "Return only the integer sum of 1 and 1. PRIVATE_TASK_MARKER"}
    with ThreadPoolExecutor(1) as pool:
        submitted = pool.submit(client.post, "/v1/tasks", json=task)
        try:
            assert entered.wait(3)
            before = operator.get("/monitor/snapshot").json()
            for _ in range(3):
                response = operator.get("/monitor/capacity")
                assert response.status_code == 200
                assert "PRIVATE_TASK_MARKER" not in response.text
                assert response.json()["execution_enabled"] is False
            rejected = second.post("/v1/tasks", json={**task, "run_id": "capacity-original-busy"})
            assert rejected.status_code == 429 and rejected.json()["error"]["code"] == "service_busy"
            after = operator.get("/monitor/snapshot").json()
            assert after["control"] == before["control"] and after["ledger"] == before["ledger"]
            assert len(bundle.upstream.calls) == 1 and upstream.requests == []
        finally:
            release.set()
        assert submitted.result(3).json()["status"] == "passed"


def test_source_refresh_is_exclusive_but_saved_reads_are_available_and_private(local_service):
    upstream = MockCapacitySources()
    attach_capacity(local_service.bundle, local_service.clock, sources=upstream.configured())
    local_service.server.set_app(local_service.bundle.app)
    entered, release = threading.Event(), threading.Event()

    def hold(_):
        entered.set()
        assert release.wait(8)

    upstream.before_response = hold
    writer, reader = local_service.client("operator", timeout=10), local_service.client("operator")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(writer.post, "/monitor/capacity/refresh", json={})
        try:
            assert entered.wait(3)
            assert reader.get("/monitor/capacity").status_code == 200
            duplicate = reader.post("/monitor/capacity/refresh", json={})
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "capacity_source_refresh_busy"
            client = local_service.client()
            assert client.get("/monitor/capacity").status_code == 401
            assert client.post("/monitor/capacity/refresh", json={}).status_code == 401
            local_service.bundle.identity.revoke(local_service.bundle.credentials["studio"].id)
            assert client.get("/monitor/capacity").status_code == 401
            assert client.get("/v1/monitor").status_code == 401
            assert len(upstream.requests) == 1
        finally:
            release.set()
        assert pending.result(3).json()["status"] == "available"


def test_controlled_import_rejects_mismatched_bindings_without_rewriting_current_sources(local_service):
    observer = attach_capacity(local_service.bundle, local_service.clock)
    local_service.server.set_app(local_service.bundle.app)
    operator = local_service.client("operator")
    before = operator.get("/monitor/capacity").json()
    config = configuration(local_service.clock())
    config["version"] = "mismatched-account-v2"
    config["bindings"][0]["account_ref"] = "another-account"
    with pytest.raises(ValueError, match="binding"):
        observer.store.configure(config)
    assert operator.get("/monitor/capacity").json() == before
    config = configuration(local_service.clock())
    config["version"] = "private-field-v2"
    config["groups"][0]["source"]["prompt"] = "PRIVATE_IMPORT_MARKER"
    with pytest.raises(ValueError):
        observer.store.configure(config)
    result = operator.get("/monitor/capacity")
    assert result.json() == before and "PRIVATE_IMPORT_MARKER" not in result.text


def test_expired_newer_observation_cannot_replace_still_valid_supplier_limit(local_service):
    now = local_service.clock()
    config = configuration(now)
    config["groups"][0]["supplier_limits"] = [
        limit("rpm", 120, now - 400, expires=1000),
        limit("rpm", 999, now - 100, expires=50),
    ]
    attach_capacity(local_service.bundle, local_service.clock, config=config)
    local_service.server.set_app(local_service.bundle.app)
    operator = local_service.client("operator")
    response = operator.get("/monitor/capacity")
    assert response.status_code == 200
    group = next(group for group in response.json()["groups"] if group["id"] == "account-shared")
    rpm = next(item for item in group["supplier_limits"] if item["dimension"] == "rpm")
    assert rpm["status"] == "known"
    assert rpm["value"] == 120
    assert rpm["observed_at"] == now - 400
    assert [item["value"] for item in rpm["observations"]] == [120, 999]
    assert local_service.bundle.upstream.calls == []
