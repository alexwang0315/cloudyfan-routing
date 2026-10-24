"""Response-derived windows at the approved actual loopback HTTP seam."""
import hashlib
from email.utils import formatdate
import math
import httpx
import pytest
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK
from capacity_windows_fixture import attach_windows, window_configuration


def test_original_response_pauses_shared_group_without_probe_or_paid_retry(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    start = local_service.clock()
    def upstream(request):
        response = bundle.upstream(request)
        if len(bundle.upstream.calls) != 1:
            return response
        body = response.json()
        body["choices"] = []
        body["error"] = {"code": 429, "message": "SYNTHETIC_PRIVATE_WINDOW_ERROR"}
        return httpx.Response(429, json=body, headers={"Retry-After": "30", "X-Private-Provider": "SYNTHETIC_PRIVATE_WINDOW_HEADER"})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, windows = attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    first = client.post("/v1/tasks", json={**TASK, "run_id": "actual-rate-limited"})
    assert len(bundle.upstream.calls) == 1 and first.json()["costs"]["unknown_events"] == 0
    view = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert view["groups"][0]["status"] == "wait" and view["groups"][0]["not_before"] == start + 30
    response = client.post("/v1/tasks", json={**TASK, "run_id": "excluded-before-post"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_window_wait"
    assert response.json()["costs"]["unknown_events"] == 0 and len(bundle.upstream.calls) == 1
    original = {str(p.relative_to(bundle.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (bundle.root / "receipts").rglob("*.json")}
    capacity = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    for _ in range(2):
        assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == view
        assert client.post("/v1/tasks/excluded-before-post/resume", json={}).json() == response.json()
    assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == capacity
    assert original == {str(p.relative_to(bundle.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (bundle.root / "receipts").rglob("*.json")}
    assert "SYNTHETIC_PRIVATE_WINDOW" not in operator.get("/monitor/capacity").text
    local_service.clock.now = start + 30
    resumed = client.post("/v1/tasks", json={**TASK, "run_id": "new-after-window"})
    assert resumed.json()["status"] == "passed" and len(bundle.upstream.calls) == 2
    assert windows.snapshot()["events"][0]["not_before"] == start + 30


@pytest.mark.parametrize("name,grammar", [("retry-after", "retry_after"),
    ("x-ratelimit-reset-requests", "unix_seconds")])
def test_trusted_absolute_reset_uses_original_date_and_declared_units(local_service, name, grammar):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    start = math.ceil(local_service.clock())
    local_service.clock.now = start
    def upstream(request):
        response = bundle.upstream(request)
        body = response.json()
        body.update(choices=[], error={"code": 429, "message": "SYNTHETIC_PRIVATE_RESET"})
        return httpx.Response(429, json=body, headers={"date": formatdate(start, usegmt=True),
            name: formatdate(start + 30, usegmt=True) if grammar == "retry_after" else str(start + 30)})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_windows(bundle, local_service.clock, rules=[{"binding": "synthetic-binding", "group": "shared",
        "header": name, "format": grammar, "constraint": "requests"}])
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "absolute-reset"})
    observed = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert observed["groups"][0]["status"] == "wait" and observed["groups"][0]["not_before"] == start + 30
    assert observed["events"][0]["observed_at"] == start
    assert len(bundle.upstream.calls) == 1


def test_contradictory_saved_headers_remain_unknown_after_every_reported_time(local_service):
    bundle = local_service.bundle
    start = math.ceil(local_service.clock())
    local_service.clock.now = start
    def upstream(request):
        response = bundle.upstream(request)
        body = response.json()
        body.update(choices=[], error={"code": 429, "message": "SYNTHETIC_PRIVATE_CONTRADICTION"})
        return httpx.Response(429, json=body, headers={"date": formatdate(start, usegmt=True),
            "retry-after": "30", "x-ratelimit-reset-requests": str(start + 90)})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_windows(bundle, local_service.clock, rules=[
        {"binding": "synthetic-binding", "group": "shared", "header": name, "format": grammar, "constraint": "requests"}
        for name, grammar in [("retry-after", "retry_after"), ("x-ratelimit-reset-requests", "unix_seconds")]])
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "contradictory-original"})
    saved = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert saved["groups"][0]["status"] == "unknown"
    assert saved["groups"][0]["reason_code"] == "capacity_window_source_contradictory"
    assert saved["groups"][0]["not_before"] is None
    local_service.clock.now = start + 100
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "unknown-not-expired"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_source_contradictory"
    assert denied.json()["costs"]["unknown_events"] == 0 and len(bundle.upstream.calls) == 1
    assert operator.post("/monitor/capacity/activate", json={"revision": 1, "version": None}).status_code == 409
    assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == saved


def test_long_window_blocks_policy_disable_and_rebinding_after_ordinary_rate_horizon(local_service):
    bundle = local_service.bundle
    start = local_service.clock()
    def upstream(request):
        response = bundle.upstream(request)
        body = response.json()
        body.update(choices=[], error={"code": 429})
        return httpx.Response(429, json=body, headers={"retry-after": "3600"})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, _ = attach_windows(bundle, local_service.clock)
    controller.register("synthetic-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=controller.routes["synthetic-window-admission-v1"], max_seconds=300)
    controller.register("synthetic-window-alternate", configuration_version="synthetic-admission-config-v1",
        routes=controller.routes["synthetic-window-admission-v1"], max_seconds=300, windows_version="synthetic-windows-v1")
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0, "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "long-original-window"})
    local_service.clock.now = start + 61
    original = operator.get("/monitor/capacity").json()
    assert original["dispatch"]["groups"][0]["rpm_used"] == 0
    assert original["dispatch"]["windows"]["groups"][0]["not_before"] == start + 3600
    for version in [None, "synthetic-admission-v1", "synthetic-window-alternate"]:
        denied = operator.post("/monitor/capacity/activate", json={"revision": 1, "version": version})
        assert denied.status_code == 409 and denied.json()["error"]["code"] == "capacity_live_state_blocks_change"
    renamed = window_configuration(start)
    renamed["version"] = "synthetic-renamed-v2"
    renamed["bindings"][0]["groups"] = ["renamed-shared"]
    renamed["groups"][0]["id"] = "renamed-shared"
    with pytest.raises(ValueError, match="Live capacity prevents configuration change"):
        controller.store.configure(renamed)
    assert operator.get("/monitor/capacity").json() == original
    assert len(bundle.upstream.calls) == 1


def test_explicit_refresh_imports_exact_controlled_rate_source_without_any_upstream_call(local_service):
    bundle = local_service.bundle
    start = local_service.clock()
    source = {"binding": "synthetic-binding", "group": "shared",
        "scope": {"kind": "account", "ref": "synthetic-shared-account"},
        "source_version": "synthetic-reviewed-v1", "evidence_ref": "synthetic-original-rate-source-v1",
        "status": "rate_limited", "observed_at": start, "valid_until": start + 300,
        "values": [["retry-after", "30"]]}
    controller, _ = attach_windows(bundle, local_service.clock, controlled_sources=[source])
    for version, enabled in [("synthetic-source-legacy", False), ("synthetic-source-alternate", True)]:
        controller.register(version, configuration_version="synthetic-admission-config-v1",
            routes=controller.routes["synthetic-window-admission-v1"], max_seconds=300,
            **({"windows_version": "synthetic-windows-v1"} if enabled else {}))
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    original = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    for _ in range(2):
        assert operator.get("/monitor/capacity").json()["dispatch"]["windows"]["groups"] == []
    assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == original
    pending = client.post("/v1/tasks", json={**TASK, "run_id": "source-not-yet-imported"})
    assert pending.status_code == 429 and pending.json()["error"]["code"] == "capacity_window_source_not_imported"
    assert bundle.upstream.calls == []
    for version in [None, "synthetic-source-legacy", "synthetic-source-alternate"]:
        disabled = operator.post("/monitor/capacity/activate", json={"revision": 1, "version": version})
        assert disabled.status_code == 409 and disabled.json()["error"]["code"] == "capacity_live_state_blocks_change"
    renamed = window_configuration(start)
    renamed["version"] = "synthetic-pending-source-renamed"
    renamed["bindings"][0]["groups"] = ["renamed-shared"]
    renamed["groups"][0]["id"] = "renamed-shared"
    with pytest.raises(ValueError, match="Live capacity prevents configuration change"):
        controller.store.configure(renamed)
    refreshed = operator.post("/monitor/capacity/refresh", json={})
    assert refreshed.status_code == 200 and refreshed.json()["request_count"] == 0
    response = client.post("/v1/tasks", json={**TASK, "run_id": "controlled-source-exclusion"})
    assert response.status_code == 429 and response.json()["error"]["code"] == "capacity_window_wait"
    event = operator.get("/monitor/capacity").json()["dispatch"]["windows"]["events"][0]
    assert event["source_kind"] == "controlled_import" and event["source_version"] == source["source_version"]
    assert event["observed_at"] == start and event["not_before"] == start + 30
    assert response.json()["costs"]["unknown_events"] == 0 and bundle.upstream.calls == []
    imported = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert operator.post("/monitor/capacity/refresh", json={}).json()["window_import"]["imported_count"] == 0
    assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == imported


def test_many_original_constraints_keep_bounded_reason_refs_without_opening_the_group(local_service):
    bundle = local_service.bundle
    start = local_service.clock()
    source = {"binding": "synthetic-binding", "group": "shared",
        "scope": {"kind": "account", "ref": "synthetic-shared-account"},
        "source_version": "synthetic-reviewed-v1", "evidence_ref": "synthetic-many-constraints-v1",
        "status": "rate_limited", "observed_at": start, "valid_until": start + 300,
        "values": [["retry-after", "30"]]}
    attach_windows(bundle, local_service.clock, controlled_sources=[source], rules=[
        {"binding": "synthetic-binding", "group": "shared", "header": "retry-after",
         "format": "retry_after", "constraint": f"requests-{number:03d}"} for number in range(128)])
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    assert operator.post("/monitor/capacity/refresh", json={}).json()["window_import"]["imported_count"] == 128
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "bounded-original-constraints"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_wait"
    view = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert view["event_count"] == 128 and len(view["events"]) == 100 and view["event_history_complete"] is False
    assert len(view["groups"]) == 1 and view["groups"][0]["not_before"] == start + 30
    assert bundle.upstream.calls == []


@pytest.mark.parametrize("source_case,reason", [
    ("stale", "capacity_window_source_stale_or_future"),
    ("future", "capacity_window_source_stale_or_future"),
    ("clock_reference", "capacity_window_clock_reference_unknown"),
    ("nonfinite_reset", "capacity_window_time_invalid"),
    ("contradictory", "capacity_window_source_contradictory")])
def test_invalid_controlled_source_stays_unknown_without_clearing_or_dispatch(local_service, source_case, reason):
    bundle = local_service.bundle
    start = local_service.clock()
    source = {"binding": "synthetic-binding", "group": "shared",
        "scope": {"kind": "account", "ref": "synthetic-shared-account"},
        "source_version": "synthetic-reviewed-v1", "evidence_ref": "synthetic-invalid-source-v1",
        "status": "rate_limited", "observed_at": start, "valid_until": start + 300,
        "values": [["retry-after", "30"]]}
    rules, sources = None, [source]
    if source_case == "stale":
        source.update(observed_at=start - 100, valid_until=start - 1)
    elif source_case == "future":
        source["observed_at"] = start + 10
    elif source_case == "clock_reference":
        source["values"].append(["date", formatdate(start - 100, usegmt=True)])
    elif source_case == "nonfinite_reset":
        source["values"] = [["date", formatdate(start, usegmt=True)], ["x-ratelimit-reset-requests", "NaN"]]
        rules = [{"binding": "synthetic-binding", "group": "shared", "header": "x-ratelimit-reset-requests",
            "format": "unix_seconds", "constraint": "requests"}]
    elif source_case == "contradictory":
        sources.append({**source, "evidence_ref": "synthetic-contradictory-source-v1", "values": [["retry-after", "60"]]})
    attach_windows(bundle, local_service.clock, controlled_sources=sources, rules=rules)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    refreshed = operator.post("/monitor/capacity/refresh", json={})
    assert refreshed.status_code == 200 and refreshed.json()["window_import"]["status"] == "unknown"
    original = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert original["groups"][0]["status"] == "unknown" and original["groups"][0]["reason_code"] == reason
    assert original["pending_sources"] == []
    local_service.clock.now += 120
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "invalid-source-stays-unknown"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == reason
    assert denied.json()["costs"]["unknown_events"] == 0 and bundle.upstream.calls == []
    assert operator.post("/monitor/capacity/refresh", json={}).json()["window_import"]["imported_count"] == 0
    assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == original


def test_backwards_clock_cannot_clear_saved_window_or_extend_original_task_deadline(local_service):
    bundle = local_service.bundle
    start = local_service.clock()
    bundle.upstream.fail_tiers = set()
    def upstream(request):
        response = bundle.upstream(request)
        local_service.clock.now = start + 61
        body = response.json()
        body.update(choices=[], error={"code": 429})
        return httpx.Response(429, json=body, headers={"retry-after": "30"})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, _ = attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    first = client.post("/v1/tasks", json={**TASK, "run_id": "original-deadline"}).json()
    assert first["capacity"]["accepted_at"] == start and first["capacity"]["deadline"] == start + 300
    saved = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert saved["groups"][0]["observed_at"] == start + 61 and saved["groups"][0]["not_before"] == start + 91
    local_service.clock.now = start + 10
    before = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    for _ in range(2):
        unavailable = operator.get("/monitor/capacity")
        assert unavailable.status_code == 409 and unavailable.json()["error"]["code"] == "capacity_clock_backwards"
    assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == before
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "clock-moved-backwards"})
    assert denied.status_code == 503 and denied.json()["error"]["code"] == "identity_authentication_unavailable"
    local_service.clock.now = start + 62
    assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == saved
    assert client.post("/v1/tasks/original-deadline/resume", json={}).json() == first
    assert len(bundle.upstream.calls) == 1


def test_original_reset_cannot_change_the_explicitly_registered_time_unit(local_service):
    bundle = local_service.bundle
    start = math.ceil(local_service.clock())
    local_service.clock.now = start
    def upstream(request):
        body = bundle.upstream(request).json()
        body.update(choices=[], error={"code": 429})
        return httpx.Response(429, json=body, headers={"date": formatdate(start, usegmt=True),
            "x-ratelimit-reset-requests": formatdate(start + 30, usegmt=True)})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    attach_windows(bundle, local_service.clock, rules=[{"binding": "synthetic-binding", "group": "shared",
        "header": "x-ratelimit-reset-requests", "format": "unix_seconds", "constraint": "requests"}])
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    client.post("/v1/tasks", json={**TASK, "run_id": "unexpected-reset-unit"})
    observed = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    assert observed["groups"][0]["status"] == "unknown"
    assert observed["groups"][0]["reason_code"] == "capacity_window_time_invalid"
    local_service.clock.now += 31
    denied = client.post("/v1/tasks", json={**TASK, "run_id": "unit-mismatch-remains-unknown"})
    assert denied.status_code == 429 and denied.json()["error"]["code"] == "capacity_window_time_invalid"
    assert len(bundle.upstream.calls) == 1


@pytest.mark.parametrize("invalid_clock", [float("nan"), float("inf")])
def test_nonfinite_wall_clock_refuses_without_losing_saved_window(local_service, invalid_clock):
    bundle = local_service.bundle
    start = local_service.clock()
    bundle.upstream.fail_tiers = set()
    def upstream(request):
        body = bundle.upstream(request).json()
        body.update(choices=[], error={"code": 429})
        return httpx.Response(429, json=body, headers={"retry-after": "30"})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, _ = attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client("operator"), local_service.client()
    assert operator.post("/monitor/capacity/activate", json={"revision": 0,
        "version": "synthetic-window-admission-v1"}).status_code == 200
    original = client.post("/v1/tasks", json={**TASK, "run_id": "saved-before-nonfinite-clock"}).json()
    saved = operator.get("/monitor/capacity").json()["dispatch"]["windows"]
    before = hashlib.sha256(controller.store.path.read_bytes()).hexdigest()
    local_service.clock.now = invalid_clock
    try:
        unavailable = operator.get("/monitor/capacity")
        assert unavailable.status_code == 409 and unavailable.json()["error"]["code"] == "capacity_clock_invalid"
        denied = client.post("/v1/tasks", json={**TASK, "run_id": "nonfinite-clock-not-dispatched"})
        assert denied.status_code == 503 and denied.json()["error"]["code"] == "identity_authentication_unavailable"
        assert hashlib.sha256(controller.store.path.read_bytes()).hexdigest() == before
    finally:
        local_service.clock.now = start + 1
    assert operator.get("/monitor/capacity").json()["dispatch"]["windows"] == saved
    assert client.post("/v1/tasks/saved-before-nonfinite-clock/resume", json={}).json() == original
    assert len(bundle.upstream.calls) == 1
