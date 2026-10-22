from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import io
import json

import httpx
import pytest

from cloudyfan_monitor.app import App
from cloudyfan_monitor.demo import build_demo, ADMIN_TOKEN, TOKENS, CATEGORY
from cloudyfan_monitor.gateway import Gateway
from cloudyfan_monitor.pools import empty_view
from cloudyfan_monitor.store import MonitorError, Store


@pytest.fixture
def system(tmp_path):
    app, upstream = build_demo(tmp_path, seed=False, origin="http://monitor")
    admin = httpx.Client(transport=httpx.WSGITransport(app), base_url="http://monitor",
                         headers={"Authorization": "Bearer " + ADMIN_TOKEN})
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url="http://monitor",
                          headers={"Authorization": "Bearer " + TOKENS["studio"]})
    return app, upstream, admin, client


TASK = {"run_id": "sample", "category": CATEGORY, "prompt": "PRIVATE-PROMPT Return only 1+1."}


@pytest.mark.parametrize("mode,initial", [("cloudyfan", "open_source"), ("fixed_high", "high"),
                                         ("low_first", "low"), ("open_source_first", "open_source")])
def test_all_modes_control_actual_core_first_call(system, mode, initial):
    app, upstream, admin, client = system
    revision = app.store.control()["revision"]
    assert admin.post("/monitor/mode", json={"revision": revision, "mode": mode}).status_code == 200
    assert client.post("/v1/tasks", json=TASK).json()["status"] == "passed"
    assert upstream.calls[0]["model"] == "example/" + initial
    snapshot = admin.get("/monitor/snapshot").json()
    task = snapshot["tasks"][0]
    assert task["mode"] == mode
    if mode == "cloudyfan":
        assert task["classifier_calls"] == 2 and task["generation_attempts"] == 3
        assert snapshot["ledger"]["total_usd"] == "0.0053"
        assert snapshot["summary"]["jev_twice"] == 1
    else:
        assert task["classifier_calls"] == 0 and task["generation_attempts"] <= 2


def test_mode_change_does_not_resend_paid_job_or_modify_old_binding(system):
    app, upstream, admin, client = system
    assert client.post("/v1/tasks", json=TASK).status_code == 200
    before = len(upstream.calls)
    admin.post("/monitor/mode", json={"revision": app.store.control()["revision"], "mode": "fixed_high"})
    assert client.post("/v1/tasks", json=TASK).status_code == 200
    assert client.post("/v1/tasks/sample/resume", json={}).status_code == 200
    assert len(upstream.calls) == before
    assert app.store.binding("studio", "sample")["mode"] == "cloudyfan"
    assert client.post("/v1/tasks", json={**TASK, "prompt": "different"}).status_code == 409
    assert client.post("/v1/tasks/sample/resume", json={"mode": "fixed_high"}).status_code == 422
    assert client.post("/v1/tasks", json={**TASK, "run_id": "new"}).status_code == 200
    assert app.store.binding("studio", "new")["mode"] == "fixed_high"


def test_client_override_is_not_overwritten_by_global_default(system):
    app, upstream, admin, client = system
    for body in ({"client": "studio", "mode": "low_first"}, {"mode": "fixed_high"}):
        assert admin.post("/monitor/mode", json={"revision": app.store.control()["revision"], **body}).status_code == 200
    client.post("/v1/tasks", json=TASK)
    assert upstream.calls[0]["model"] == "example/low"
    admin.post("/monitor/mode", json={"revision": app.store.control()["revision"], "client": "studio", "mode": None})
    client.post("/v1/tasks", json={**TASK, "run_id": "inherit"})
    assert app.store.binding("studio", "inherit")["mode"] == "fixed_high"


def test_dashboard_polling_never_dispatches_and_keeps_privacy(system):
    app, upstream, admin, client = system
    client.post("/v1/tasks", json=TASK)
    before = len(upstream.calls)
    for _ in range(3):
        response = admin.get("/monitor/snapshot")
        assert response.status_code == 200
        assert "PRIVATE-PROMPT" not in response.text and TOKENS["studio"] not in response.text
        assert "answer" not in response.text
        assert len(response.json()["ledger"]["accounts"]) == 1  # shared four-mode budget is counted once
    assert len(upstream.calls) == before


def test_client_dashboard_cannot_see_another_client_or_global_ledger(system):
    app, upstream, admin, client = system
    client.post("/v1/tasks", json=TASK)
    other = httpx.Client(transport=httpx.WSGITransport(app), base_url="http://monitor",
                         headers={"Authorization": "Bearer " + TOKENS["agent"]})
    own = other.get("/v1/monitor").json()
    assert own["tasks"] == [] and [c["id"] for c in own["clients"]] == ["agent"]
    assert "ledger" not in own and own["audit"] == []
    assert other.get("/v1/tasks/sample/result").status_code == 404
    assert client.get("/monitor/snapshot").status_code == 401
    assert client.post("/monitor/mode", json={"revision": 0, "mode": "fixed_high"}).status_code == 401


def test_unknown_fee_stops_dispatch_and_never_displays_zero_total(system):
    app, upstream, admin, client = system
    upstream.unknown_cost = True
    first = client.post("/v1/tasks", json=TASK)
    assert first.status_code == 409
    view = admin.get("/monitor/snapshot").json()
    assert view["ledger"]["total_usd"] is None and view["ledger"]["unknown_events"] == 1
    assert view["summary"]["passed"] == 0 and view["summary"]["unknown"] == 1
    before = len(upstream.calls)
    client.post("/v1/tasks", json={**TASK, "run_id": "blocked"})
    assert len(upstream.calls) == before


def test_generation_and_classifier_failure_fees_are_in_the_ledger(system):
    app, upstream, admin, client = system
    client.post("/v1/tasks", json=TASK)
    ledger = admin.get("/monitor/snapshot").json()["ledger"]
    rows = ledger["rows"]
    assert len(rows) == 5
    assert sum(Decimal(r["cost"]) for r in rows) == Decimal(".0053")
    assert ledger["by_kind_known_usd"] == {"classification": "0.0002", "generation": "0.0051"}
    assert all(r.get("model") and r.get("provider") and r.get("tokens") for r in rows)
    assert sum(r["request_kind"] == "classification" for r in rows) == 2


def test_registered_pool_activation_changes_only_future_tasks(system):
    app, upstream, admin, client = system
    client.post("/v1/tasks", json=TASK)
    old = app.store.binding("studio", "sample")
    new_view = app.store.pool(old["pool"])
    new_view.pop("id")
    new_view["source_kind"] = "second_registered_mock_revision"
    new_id = app.store.save_pool(new_view)
    registry = dict(app.gateway.registry)
    registry.update({(new_id, category, mode): contract for (pool, category, mode), contract in registry.copy().items()
                     if pool == old["pool"]})
    app.gateway = Gateway(app.store, app.gateway.service, registry, demo=True)
    revision = app.store.control()["revision"]
    assert admin.post("/monitor/pools/activate", json={"revision": revision, "pool": new_id}).status_code == 200
    count = len(upstream.calls)
    assert client.post("/v1/tasks/sample/resume", json={}).status_code == 200
    assert len(upstream.calls) == count and app.store.binding("studio", "sample")["pool"] == old["pool"]
    assert client.post("/v1/tasks", json={**TASK, "run_id": "new-pool"}).status_code == 200
    assert app.store.binding("studio", "new-pool")["pool"] == new_id


def test_mode_statistics_do_not_mix_classifier_denominators(system):
    app, upstream, admin, client = system
    client.post("/v1/tasks", json=TASK)
    app.gateway.switch(app.store.control()["revision"], "low_first")
    client.post("/v1/tasks", json={**TASK, "run_id": "cheap"})
    view = admin.get("/monitor/snapshot").json()
    assert view["summary"]["tasks"] == 2
    assert view["by_mode"]["cloudyfan"]["tasks"] == 1
    assert view["by_mode"]["cloudyfan"]["jev_twice"] == 1
    assert view["by_mode"]["low_first"]["jev_twice"] == 0
    assert view["by_mode"]["cloudyfan"]["complete_api_usd"] == "0.0053"


def test_interrupted_validation_resume_does_not_resend_model(system):
    from dataclasses import replace
    from cloudyfan_jev.demo import ArithmeticEnvironment
    app, upstream, admin, client = system
    upstream.fail_tiers = set()
    class Once(ArithmeticEnvironment):
        failed = False
        def validate(self, text):
            if not type(self).failed:
                type(self).failed = True
                raise RuntimeError("PRIVATE answer and stack")
            return super().validate(text)
    contract = app.gateway.service.contracts["demo-cloudyfan"]
    app.gateway.service.contracts["demo-cloudyfan"] = replace(contract, environment_factory=lambda w: Once())
    assert client.post("/v1/tasks", json=TASK).status_code == 503
    assert admin.get("/monitor/snapshot").json()["summary"]["unknown"] == 1
    before = len(upstream.calls)
    assert client.post("/v1/tasks/sample/resume", json={}).json()["status"] == "passed"
    view = admin.get("/monitor/snapshot").json()
    assert len(upstream.calls) == before and view["summary"]["passed"] == 1
    assert view["tasks"][0]["wall_seconds"] is None  # resume is not original wall latency


def test_conflicting_admin_changes_are_rejected_atomically(system):
    app, upstream, admin, client = system
    revision = app.store.control()["revision"]
    def change(mode):
        try:
            app.gateway.switch(revision, mode)
            return True
        except MonitorError as error:
            assert error.status == 409
            return False
    with ThreadPoolExecutor(2) as executor:
        result = list(executor.map(change, ["fixed_high", "low_first"]))
    assert sorted(result) == [False, True] and upstream.calls == []


def test_restart_preserves_modes_pool_versions_and_idempotency(system, tmp_path):
    app, upstream, admin, client = system
    client.post("/v1/tasks", json=TASK)
    app.gateway.switch(app.store.control()["revision"], "fixed_high")
    restored = Store(app.store.path)
    gateway = Gateway(restored, app.gateway.service, app.gateway.registry, demo=True)
    before = len(upstream.calls)
    assert gateway.execute("studio", "sample")["status"] == "passed"
    assert len(upstream.calls) == before
    assert restored.control()["mode"] == "fixed_high"
    assert restored.binding("studio", "sample")["mode"] == "cloudyfan"


def test_pool_refresh_preview_publish_and_runtime_activation_are_distinct(system):
    app, upstream, admin, client = system
    old = app.store.control()
    result = admin.post("/monitor/pools/refresh", json={})
    assert result.status_code == 200
    staged = result.json()["pool"]
    assert app.store.control() == old and upstream.calls == []
    view = admin.get("/monitor/pools", params={"id": staged}).json()
    assert len(view["categories"]) == 23
    assert sum(len(v["pools"]) for v in view["categories"].values()) == 92
    assert sum(len(p) for c in view["categories"].values() for p in c["pools"].values()) == 276
    assert admin.post("/monitor/pools/publish", json={"revision": old["revision"], "pool": staged}).status_code == 200
    assert app.store.control()["catalog_pool"] == staged and app.store.control()["routing_pool"] == old["routing_pool"]
    assert admin.post("/monitor/pools/activate", json={"revision": app.store.control()["revision"], "pool": staged}).status_code == 409
    assert admin.post("/monitor/pools/publish", json={"revision": app.store.control()["revision"], "pool": old["catalog_pool"]}).status_code == 200


def test_failed_refresh_keeps_original_pool(system):
    app, upstream, admin, client = system
    class Broken:
        def refresh(self):
            raise RuntimeError("SECRET")
    app.source = Broken()
    old = app.store.control()
    response = admin.post("/monitor/pools/refresh", json={})
    assert response.status_code == 503 and "SECRET" not in response.text
    assert app.store.control() == old and upstream.calls == []


@pytest.mark.parametrize("headers", [{"Origin": "https://attacker.example"}, {"Host": "attacker.example"}])
def test_origin_and_host_guards(system, headers):
    _, upstream, admin, _ = system
    assert admin.get("/monitor/snapshot", headers=headers).status_code == 403
    assert upstream.calls == []


@pytest.mark.parametrize("payload", [{**TASK,"mode":"fixed_high"}, {**TASK,"principal":"agent"},
                                    {**TASK,"model":"arbitrary"}, {**TASK,"run_id":"../../private"},
                                    {**TASK,"category":"not-a-category"}])
def test_client_cannot_override_registered_policy(system, payload):
    _, upstream, _, client = system
    assert client.post("/v1/tasks", json=payload).status_code == 422
    assert upstream.calls == []


def test_live_mode_never_exposes_operator_token_or_demo_login(system):
    app, _, _, _ = system
    app.gateway.demo = False
    live = App(app.gateway, admin_token="X"*40, origin="http://monitor")
    client = httpx.Client(transport=httpx.WSGITransport(live),base_url="http://monitor")
    assert client.get("/demo/session").status_code == 404
    assert client.get("/monitor/snapshot").status_code == 401
    assert client.get("/").status_code == 200
    assert "X"*40 not in client.get("/").text
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_invalid_or_missing_pool_evidence_is_not_success(system):
    app, _, admin, client = system
    client.post("/v1/tasks", json=TASK)
    core = app.gateway.service.contracts["demo-cloudyfan"].runner.core
    core.read_completed = lambda *args: (_ for _ in ()).throw(ValueError("incomplete"))
    result = admin.get("/monitor/snapshot").json()
    assert result["summary"]["passed"] == 0 and result["summary"]["unknown"] == 1


def test_unknown_empty_arrays_and_quantiles_are_explicit(system):
    _, _, admin, _ = system
    data = admin.get("/monitor/snapshot").json()
    assert data["summary"]["final_rate"] is None and data["summary"]["latency_p95"] is None


def test_pool_integrity_and_shape_rejected(system):
    app, _, _, _ = system
    view=empty_view("fixture", "2026-10-22T00:00:00+00:00")
    view["categories"].pop(CATEGORY)
    with pytest.raises(MonitorError, match="23_categories"):
        app.store.save_pool(view)
    key=app.store.control()["catalog_pool"]
    with app.store.db() as db:
        db.execute("UPDATE pools SET data=? WHERE id=?", (json.dumps({"corrupted": True}), key))
    with pytest.raises(MonitorError, match="integrity"):
        app.store.pool(key)


def test_duplicate_json_keys_rejected(system):
    app, upstream, _, _ = system
    body=b'{"revision":0,"revision":1,"mode":"fixed_high"}'
    env={"REQUEST_METHOD":"POST", "PATH_INFO":"/monitor/mode", "HTTP_HOST":"monitor",
         "HTTP_AUTHORIZATION":"Bearer "+ADMIN_TOKEN, "CONTENT_TYPE":"application/json",
         "CONTENT_LENGTH":str(len(body)), "wsgi.input":io.BytesIO(body)}
    result=[]
    app(env,lambda status,headers:result.append(status))
    assert result[0].startswith("422") and upstream.calls == []
