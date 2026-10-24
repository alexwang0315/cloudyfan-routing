"""Official native client -> Monitor admission -> real Core -> mock upstream."""
from concurrent.futures import ThreadPoolExecutor
import threading
import httpx
import ollama
import pytest
import time

from cloudyfan_jev.identity import IdentityStore

from cloudyfan_monitor.app import App
from cloudyfan_monitor.demo import ADMIN_TOKEN, CATEGORY, TOKENS, build_demo


ALIAS = "cloudyfan/arithmetic:v1"
PROMPT = "Return only the integer sum of 1 and 1."
MODELS = {ALIAS: {"category": CATEGORY, "contract_id": "demo-cloudyfan"}}


def native_system(tmp_path):
    original, upstream = build_demo(tmp_path, seed=False)
    app = App(original.gateway, admin_token=ADMIN_TOKEN, origin=original.origin,
              ollama_models=MODELS)
    return app, upstream


def http_client(app, token):
    return httpx.Client(transport=httpx.WSGITransport(app), base_url=app.origin,
                        headers={"Authorization": "Bearer " + token}, trust_env=False)


def sdk_client(app, run, token=TOKENS["studio"]):
    return ollama.Client(host=app.origin, transport=httpx.WSGITransport(app), trust_env=False,
                         follow_redirects=False, headers={"Authorization": "Bearer " + token,
                         "X-CloudyFan-Run-ID": run})


def test_official_sdk_native_request_is_an_observable_client_mrc_task(tmp_path):
    app, upstream = native_system(tmp_path)
    with sdk_client(app, "native-one") as sdk:
        assert [model.model for model in sdk.list().models] == [ALIAS]
        response = sdk.generate(model=ALIAS, prompt=PROMPT, stream=False)
        assert response.response == "2" and response.done is True
    with http_client(app, TOKENS["studio"]) as client, http_client(app, ADMIN_TOKEN) as admin:
        result = client.get("/v1/tasks/native-one/result")
        assert result.status_code == 200 and result.json()["final_public_pass"] is True
        own = client.get("/v1/monitor").json()
        assert [row["id"] for row in own["clients"]] == ["studio"]
        task = own["tasks"][0]
        assert task["run"] == "native-one" and task["mode"] == "cloudyfan"
        assert [item["tier"] for item in task["path"]] == ["open_source", "mid", "high"]
        assert task["classifier_calls"] == 2
        assert admin.get("/monitor/snapshot").json()["ledger"]["total_usd"] == "0.0053"
        assert len(upstream.calls) == 5


def test_native_ingress_keeps_the_actual_alias_and_endpoint_without_private_text(tmp_path):
    original, upstream = build_demo(tmp_path, seed=False)
    alias_two = "cloudyfan/arithmetic-copy:v1"
    app = App(original.gateway, admin_token=ADMIN_TOKEN,
              ollama_models={**MODELS, alias_two: MODELS[ALIAS]})
    upstream.fail_tiers = set()
    before = time.time()
    with http_client(app, TOKENS["studio"]) as client, http_client(app, ADMIN_TOKEN) as admin:
        for run, path, body in [
            ("ingress-generate", "/api/generate", {"model": ALIAS, "prompt": PROMPT, "stream": False}),
            ("ingress-chat", "/api/chat", {"model": alias_two,
             "messages": [{"role": "user", "content": PROMPT}], "stream": False}),
        ]:
            response = client.post(path, json=body, headers={"X-CloudyFan-Run-ID": run})
            assert response.status_code == 200, response.text
        snapshot = admin.get("/monitor/snapshot")
        tasks = {task["run"]: task for task in snapshot.json()["tasks"]}
        first = tasks["ingress-generate"]["ingress"]
        assert {key: first[key] for key in ("protocol", "endpoint", "alias")} == {
            "protocol": "ollama", "endpoint": "/api/generate", "alias": ALIAS}
        assert tasks["ingress-chat"]["ingress"]["alias"] == alias_two
        assert tasks["ingress-chat"]["ingress"]["endpoint"] == "/api/chat"
        assert before <= first["received_at"] <= time.time()
        assert before <= tasks["ingress-chat"]["ingress"]["received_at"] <= time.time()
        assert PROMPT not in snapshot.text and TOKENS["studio"] not in snapshot.text


def test_native_model_read_and_task_write_scopes_remain_separate(tmp_path):
    app, upstream = native_system(tmp_path)
    upstream.fail_tiers = set()
    identity = IdentityStore(tmp_path / "identities.sqlite", pepper=b"native-synthetic-pepper-only-0000")
    identity.create_principal("studio")
    app.identity_store = identity
    listing = identity.issue("studio", scopes={"models:read"}, expires_at=time.time() + 3600)
    writing = identity.issue("studio", scopes={"tasks:write"}, expires_at=time.time() + 3600)
    with sdk_client(app, "scope-list", listing.token) as sdk:
        assert sdk.list().models[0].model == ALIAS
        with pytest.raises(ollama.ResponseError) as denied:
            sdk.generate(model=ALIAS, prompt=PROMPT, stream=False)
        assert denied.value.status_code == 401
    assert upstream.calls == []
    with sdk_client(app, "scope-write", writing.token) as sdk:
        with pytest.raises(ollama.ResponseError) as denied:
            sdk.list()
        assert denied.value.status_code == 401
        assert sdk.chat(model=ALIAS, messages=[{"role": "user", "content": PROMPT}], stream=False).done
    identity.set_active("studio", False)
    with sdk_client(app, "scope-disabled", writing.token) as sdk:
        with pytest.raises(ollama.ResponseError) as denied:
            sdk.generate(model=ALIAS, prompt=PROMPT, stream=False)
        assert denied.value.status_code == 401
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("headers,change,status,error", [
    ({"Host": "other.invalid"}, {}, 403, "host_not_allowed"),
    ({"Origin": "https://other.invalid"}, {}, 403, "origin_not_allowed"),
    ({}, {"model": "unregistered:model"}, 404, "model_not_registered"),
    ({}, {"stream": True}, 422, "only_explicit_nonstream_text_supported"),
])
def test_native_rejections_preserve_safe_headers_and_native_errors(tmp_path, headers, change, status, error):
    app, upstream = native_system(tmp_path)
    with http_client(app, TOKENS["studio"]) as client:
        response = client.post("/api/generate", json={"model": ALIAS, "prompt": PROMPT,
            "stream": False, **change}, headers={"X-CloudyFan-Run-ID": "rejected", **headers})
    assert response.status_code == status and response.json() == {"error": error}
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert ADMIN_TOKEN not in response.text and upstream.calls == []


def test_native_and_task_ingress_share_capacity_and_replay_keeps_its_exact_contract(tmp_path):
    app, upstream = native_system(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def pause(request):
        entered.set()
        assert release.wait(10)
    upstream.before_response = pause
    body = {"model": ALIAS, "prompt": PROMPT, "stream": False}
    def first_request():
        with http_client(app, TOKENS["studio"]) as client:
            return client.post("/api/generate", json=body, headers={"X-CloudyFan-Run-ID": "capacity-one"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first_request)
        try:
            assert entered.wait(10)
            with http_client(app, TOKENS["agent"]) as other, http_client(app, ADMIN_TOKEN) as admin:
                busy = other.post("/api/generate", json=body, headers={"X-CloudyFan-Run-ID": "capacity-two"})
                assert busy.status_code == 429 and busy.json()["error"] == "service_busy"
                busy = other.post("/v1/tasks", json={"run_id": "capacity-three", "category": CATEGORY, "prompt": PROMPT})
                assert busy.status_code == 429 and busy.json()["error"]["code"] == "service_busy"
                assert admin.post("/monitor/mode", json={"revision": app.store.control()["revision"],
                                  "mode": "fixed_high"}).status_code == 200
        finally:
            release.set()
        assert future.result(timeout=15).status_code == 200
    before = len(upstream.calls)
    with http_client(app, TOKENS["studio"]) as client:
        replay = client.post("/api/generate", json=body, headers={"X-CloudyFan-Run-ID": "capacity-one"})
        assert replay.status_code == 200 and replay.json()["response"] == "2"
        changed = client.post("/api/generate", json=body, headers={"X-CloudyFan-Run-ID": "contract-changed"})
        assert changed.status_code == 409 and changed.json()["error"] == "ollama_contract_changed"
        tasks = client.get("/v1/monitor").json()["tasks"]
        assert len(tasks) == 1 and tasks[0]["mode"] == "cloudyfan"
    assert len(upstream.calls) == before == 5


def test_native_task_freezes_its_customer_tariff_and_replay_is_not_charged_twice(tmp_path):
    app, upstream = native_system(tmp_path)
    upstream.fail_tiers = set()
    with http_client(app, ADMIN_TOKEN) as admin:
        prices = {"revision": 0, "enabled": True, "multiplier": "1", "client_multipliers": {},
                  "open_source_prices": {"example/open_source": {"input_per_million": "0.4", "output_per_million": "0.8"}}}
        assert admin.post("/monitor/billing/settings", json=prices).status_code == 200
        with sdk_client(app, "priced-native") as sdk:
            assert sdk.generate(model=ALIAS, prompt=PROMPT, stream=False).done
            assert admin.post("/monitor/billing/settings", json={**prices, "revision": 1, "multiplier": "10"}).status_code == 200
            assert sdk.generate(model=ALIAS, prompt=PROMPT, stream=False).done
        assert admin.post("/monitor/billing/reconcile", json={}).json()["charged"] == 1
        assert admin.post("/monitor/billing/reconcile", json={}).json()["charged"] == 0
    with http_client(app, TOKENS["studio"]) as client, http_client(app, TOKENS["agent"]) as other:
        charge = client.get("/v1/billing").json()["entries"][0]
        assert charge["client"] == "studio" and charge["run_id"] == "priced-native"
        assert charge["pricing_revision"] == 1 and charge["amount_usd"] == "0.0000088"
        assert other.get("/v1/billing").json()["entries"] == []
        assert other.get("/v1/tasks/priced-native/result").status_code == 404
    assert len(upstream.calls) == 1


def test_simulation_start_requires_operator_and_an_enabled_demo_controller(tmp_path):
    app, upstream = native_system(tmp_path)
    with http_client(app, TOKENS["studio"]) as client, http_client(app, ADMIN_TOKEN) as admin:
        assert client.post("/monitor/simulation/start", json={}).status_code == 401
        response = admin.post("/monitor/simulation/start", json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "simulation_not_available"
    assert upstream.calls == []


def test_operator_snapshot_lists_the_real_idle_controller_without_starting_a_batch(tmp_path):
    from cloudyfan_monitor.simulation import ALIAS as SIMULATION_ALIAS, CUSTOMER, build_simulation
    fixture = build_simulation(tmp_path / "simulation")
    with http_client(fixture.app, ADMIN_TOKEN) as admin:
        response = admin.get("/monitor/snapshot")
        assert response.status_code == 200
        control = response.json()["simulation"]
        assert control["status"] == "idle" and control["completed"] == 0 and control["total"] == 50
        assert control["alias"] == SIMULATION_ALIAS and control["customer"] == CUSTOMER
        assert control["can_start"] is True and control["demo"] is True
        assert admin.post("/monitor/simulation/start", json={"batch_id": "caller-change"}).status_code == 422
        fixture.app.gateway.demo = False
        assert admin.post("/monitor/simulation/start", json={}).status_code == 409
    assert fixture.controller.descriptor()["status"] == "idle" and fixture.upstream.calls == []


def test_native_replay_of_a_ready_binding_does_not_implicitly_resume(tmp_path):
    app, upstream = native_system(tmp_path)
    task = {"run_id": "ready-native", "category": CATEGORY, "prompt": PROMPT}
    binding = app.store.bind("studio", task, app.gateway.registry)
    app.gateway.service.create("studio", {"run_id": task["run_id"], "prompt": PROMPT,
                                         "contract_id": binding["contract"]})
    app.gateway.capture(binding)
    with http_client(app, TOKENS["studio"]) as client:
        replay = client.post("/api/generate", json={"model": ALIAS, "prompt": PROMPT, "stream": False},
                             headers={"X-CloudyFan-Run-ID": task["run_id"]})
        assert replay.status_code == 409 and replay.json()["cloudyfan"]["status"] == "ready"
        assert upstream.calls == []
        assert client.post("/v1/tasks/ready-native/resume", json={}).status_code == 200
        assert client.get("/v1/tasks/ready-native/result").json()["final_public_pass"] is True
    assert len(upstream.calls) == 5


def test_identity_engineering_failure_is_unavailable_while_invalid_credentials_remain_unauthorized(tmp_path):
    app, upstream = native_system(tmp_path)
    clock = [1000.0]
    identity = IdentityStore(tmp_path / "identity.sqlite", pepper=b"synthetic-rollback-pepper-only-0000",
                             clock=lambda: clock[0])
    identity.create_principal("studio")
    credential = identity.issue("studio", scopes={"tasks:write"}, expires_at=2000)
    app.identity_store = identity
    clock[0] = 999.0
    with http_client(app, credential.token) as client:
        failed = client.post("/api/chat", json={"model": ALIAS, "messages": [{"role": "user", "content": PROMPT}],
                             "stream": False}, headers={"X-CloudyFan-Run-ID": "clock-blocked"})
        assert failed.status_code == 503 and failed.json() == {"error": "identity_authentication_unavailable"}
        invalid = client.post("/api/generate", json={"model": ALIAS, "prompt": PROMPT, "stream": False},
                              headers={"Authorization": "Bearer invalid-fixture", "X-CloudyFan-Run-ID": "invalid-blocked"})
        assert invalid.status_code == 401 and invalid.json() == {"error": "unauthorized"}
    assert upstream.calls == []
