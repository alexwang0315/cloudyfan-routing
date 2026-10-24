import httpx
import hashlib
import json
import pytest
from concurrent.futures import ThreadPoolExecutor
import threading

from cloudyfan_monitor.demo import ADMIN_TOKEN, CATEGORY, TOKENS, build_demo
from cloudyfan_monitor.app import App
from cloudyfan_monitor.gateway import Gateway
from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting


# Explicit synthetic fixture; never a usable supplier credential.
SYNTHETIC_CREDENTIAL = 'synthetic-accounting-credential'


@pytest.mark.parametrize("client", [None, {}, [], True, "client with spaces"])
def test_invalid_credit_client_is_rejected_without_recording_money(tmp_path, client):
    app, _ = build_demo(tmp_path / "monitor", seed=False)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        result = admin.post("/monitor/billing/credits", json={"client": client, "amount_usd": "1",
            "idempotency_key": "invalid-client", "note": "Opening credit"})
        assert result.status_code == 422
        assert admin.get("/monitor/billing").json()["entries"] == []


def test_operator_opens_disabled_usd_billing_without_dispatching_models(tmp_path):
    app, upstream = build_demo(tmp_path / "monitor", seed=False)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        response = admin.get("/monitor/billing")
        assert response.status_code == 200
        view = response.json()
        assert view["currency"] == "USD"
        assert view["settings"]["enabled"] is False
        assert view["settings"]["multiplier"] == "1"
        assert view["settings"]["open_source_prices"] == {}
        assert {row["id"] for row in view["clients"]} == {"studio", "agent", "batch"}
        assert all(row["balance_usd"] == "0" for row in view["clients"])
        assert view["entries"] == []
        assert upstream.calls == []
        assert admin.get("/monitor/billing", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_operator_sets_versioned_prices_and_credits_a_client_exactly_once(tmp_path):
    app, _ = build_demo(tmp_path / "monitor", seed=False)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        settings = {"revision": 0, "enabled": True, "multiplier": "1.2",
                    "client_multipliers": {"studio": "0.9"},
                    "open_source_prices": {"mock-open_source": {
                        "input_per_million": "0.4", "output_per_million": "0.8"}}}
        assert admin.post("/monitor/billing/settings", json=settings).status_code == 200
        assert admin.post("/monitor/billing/settings", json=settings).status_code == 409
        topup = {"client": "studio", "amount_usd": "1", "idempotency_key": "initial-credit",
                 "note": "Internal opening credit"}
        assert admin.post("/monitor/billing/credits", json=topup).status_code == 200
        assert admin.post("/monitor/billing/credits", json=topup).status_code == 200
        assert admin.post("/monitor/billing/credits", json={**topup, "amount_usd": "2"}).status_code == 409
        view = admin.get("/monitor/billing").json()
        assert view["settings"]["revision"] == 1
        assert next(c for c in view["clients"] if c["id"] == "studio")["balance_usd"] == "1"
        assert len(view["entries"]) == 1
        own = admin.get("/v1/billing", headers={"Authorization": "Bearer " + TOKENS["studio"]}).json()
        assert [c["id"] for c in own["clients"]] == ["studio"]
        assert "settings" not in own and "source" not in own
        denied = admin.post("/monitor/billing/credits", json=topup,
                            headers={"Authorization": "Bearer " + TOKENS["studio"]})
        assert denied.status_code == 401


def test_new_task_keeps_its_open_source_sale_price_and_is_debited_once(tmp_path):
    root = tmp_path / "monitor"
    app, upstream = build_demo(root, seed=False)
    upstream.fail_tiers = set()
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        settings = {"revision": 0, "enabled": True, "multiplier": "1.2",
                    "client_multipliers": {"studio": "0.9"},
                    "open_source_prices": {"example/open_source": {
                        "input_per_million": "0.4", "output_per_million": "0.8"}}}
        assert admin.post("/monitor/billing/settings", json=settings).status_code == 200
        assert admin.post("/monitor/billing/credits", json={"client": "studio", "amount_usd": "1",
            "idempotency_key": "opening-credit", "note": "Opening credit"}).status_code == 200
        assert admin.post("/monitor/mode", json={"revision": app.store.control()["revision"],
            "mode": "open_source_first", "client": "studio"}).status_code == 200
        task = {"run_id": "bill-once", "category": CATEGORY, "prompt": "Return only 2."}
        created = admin.post("/v1/tasks", json=task,
            headers={"Authorization": "Bearer " + TOKENS["studio"]})
        assert created.status_code == 200
        assert admin.post("/monitor/billing/settings", json={**settings, "revision": 1,
            "client_multipliers": {"studio": "5"}}).status_code == 200
        settled = admin.post("/monitor/billing/reconcile", json={})
        assert settled.status_code == 200
        assert settled.json()["charged"] == 1
        assert admin.post("/monitor/billing/reconcile", json={}).json()["charged"] == 0
        view = admin.get("/monitor/billing").json()
        charge = next(e for e in view["entries"] if e["kind"] == "debit")
        assert charge["amount_usd"] == "0.00000792"
        assert charge["supplier_cost_usd"] == "0.0001"
        assert charge["pricing_revision"] == 1
        assert charge["basis"] == "open_source_token_price"
        assert charge["demo"] is True
        evidence = admin.get("/monitor/billing/evidence/" + charge["evidence_sha256"])
        assert evidence.status_code == 200
        assert evidence.json()["source"] == "synthetic_core_budget"
        assert hashlib.sha256(evidence.json()["evidence_json"].encode()).hexdigest() == charge["evidence_sha256"]
        assert next(c for c in view["clients"] if c["id"] == "studio")["balance_usd"] == "0.99999208"
        assert len(upstream.calls) == 1
    restarted, _ = build_demo(root, seed=False)
    assert restarted.gateway.billing.snapshot()["entries"] == view["entries"]


@pytest.mark.parametrize("supplier,multiplier,expected_charge", [
    ("0.02", "1.2", "0.024"),
    ("999999999.999999999999499999999999", "1", "999999999.999999999999"),
])
def test_openrouter_generation_actual_cost_is_the_customer_charge_basis(
        tmp_path, supplier, multiplier, expected_charge):
    fixture, upstream = build_demo(tmp_path / "monitor", seed=False)
    fetched = []

    def accounting(request):
        fetched.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/v1/generation"
        return httpx.Response(200, json={"data": {"id": request.url.params["id"],
            "model": "example/high", "total_cost": supplier,
            "native_tokens_prompt": 200, "native_tokens_completion": 20}})

    source = OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(accounting)),
                                  api_key=SYNTHETIC_CREDENTIAL)
    app = App(Gateway(fixture.store, fixture.gateway.service, fixture.gateway.registry),
              admin_token=ADMIN_TOKEN, billing_source=source)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        assert admin.post("/monitor/billing/settings", json={"revision": 0, "enabled": True,
            "multiplier": multiplier, "client_multipliers": {}, "open_source_prices": {}}).status_code == 200
        assert admin.post("/monitor/mode", json={"revision": app.store.control()["revision"],
            "mode": "fixed_high", "client": "studio"}).status_code == 200
        created = admin.post("/v1/tasks", json={"run_id": "actual-cost", "category": CATEGORY,
            "prompt": "Return only 2."}, headers={"Authorization": "Bearer " + TOKENS["studio"]})
        assert created.status_code == 200
        assert admin.get("/monitor/billing").status_code == 200
        assert fetched == []
        assert admin.post("/monitor/billing/reconcile", json={}).json()["charged"] == 1
        charge = admin.get("/monitor/billing").json()["entries"][0]
        assert charge["supplier_cost_usd"] == supplier
        assert charge["amount_usd"] == expected_charge
        assert charge["supplier_source"] == "openrouter_generation_api"
        assert charge["supplier_account_ref"] == hashlib.sha256(b"synthetic-accounting-credential").hexdigest()
        assert admin.get("/monitor/billing/evidence/" + charge["evidence_sha256"]).status_code == 200
        assert charge["input_tokens"] == 200
        assert app.gateway.ledger()["known_usd"] == "0.004"
        assert admin.post("/monitor/billing/reconcile", json={}).json()["charged"] == 0
        assert len(fetched) == len(upstream.calls) == 1


def test_missing_supplier_evidence_stays_pending_and_is_private_to_its_client(tmp_path):
    fixture, _ = build_demo(tmp_path / "monitor", seed=False)
    source = OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, json={"error": "not settled"}))),
        api_key=SYNTHETIC_CREDENTIAL)
    app = App(Gateway(fixture.store, fixture.gateway.service, fixture.gateway.registry),
              admin_token=ADMIN_TOKEN, billing_source=source)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        admin.post("/monitor/billing/settings", json={"revision": 0, "enabled": True,
            "multiplier": "1", "client_multipliers": {}, "open_source_prices": {}}).raise_for_status()
        admin.post("/monitor/mode", json={"revision": app.store.control()["revision"],
            "mode": "fixed_high", "client": "studio"}).raise_for_status()
        admin.post("/v1/tasks", json={"run_id": "pending-cost", "category": CATEGORY,
            "prompt": "Return only 2."}, headers={"Authorization": "Bearer " + TOKENS["studio"]}).raise_for_status()
        result = admin.post("/monitor/billing/reconcile", json={}).json()
        assert result["charged"] == 0 and result["waiting"] == 1
        operator = admin.get("/monitor/billing").json()
        assert operator["entries"] == []
        assert operator["pending"][0]["reason"] == "openrouter_accounting_not_found"
        other = admin.get("/v1/billing", headers={"Authorization": "Bearer " + TOKENS["agent"]}).json()
        assert other["pending"] == []
        assert other["entries"] == []


def test_account_read_is_explicit_and_does_not_become_a_customer_balance(tmp_path):
    fixture, _ = build_demo(tmp_path / "monitor", seed=False)
    requests = []

    def account(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"data": {"usage": "4", "total_credits": "100",
                                                   "total_usage": "25"}})

    source = OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(account)),
                                  api_key=SYNTHETIC_CREDENTIAL, management_key="synthetic-management-credential")
    app = App(fixture.gateway, admin_token=ADMIN_TOKEN, billing_source=source)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        assert admin.get("/monitor/billing").json()["source"]["account"] is None
        assert requests == []
        result = admin.post("/monitor/billing/source/account", json={})
        assert result.status_code == 200
        assert result.json()["account"]["scope"] == "account"
        assert result.json()["key"]["usage_usd"] == "4"
        view = admin.get("/monitor/billing").json()
        assert view["source"]["account"]["account"]["total_credits_usd"] == "100"
        assert all(c["balance_usd"] == "0" for c in view["clients"])
        assert requests == ["/api/v1/key", "/api/v1/credits"]
        assert "evidence_json" not in json.dumps(view)
        sha = view["source"]["account"]["key"]["evidence_sha256"]
        evidence = admin.get("/monitor/billing/evidence/" + sha)
        assert evidence.status_code == 200
        assert hashlib.sha256(evidence.json()["evidence_json"].encode()).hexdigest() == sha
        assert admin.get("/monitor/billing/evidence/" + sha,
            headers={"Authorization": "Bearer " + TOKENS["studio"]}).status_code == 401


@pytest.mark.parametrize("price,tokens,charge,reason", [
    (None, (20, 1), None, "billing_open_source_price_missing"),
    ({"input_per_million": "0.4", "output_per_million": "0.8"},
     (None, None), None, "billing_token_usage_pending"),
    ({"input_per_million": "0.4", "output_per_million": "0.8"}, (20, 1), "0.0000088", None),
])
def test_free_supplier_cost_does_not_replace_missing_sale_price_or_native_usage(
        tmp_path, price, tokens, charge, reason):
    fixture, upstream = build_demo(tmp_path / "monitor", seed=False)
    upstream.fail_tiers = set()
    source = OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"id": request.url.params["id"],
            "model": "example/open_source", "total_cost": "0", "native_tokens_prompt": tokens[0],
            "native_tokens_completion": tokens[1]}}))), api_key=SYNTHETIC_CREDENTIAL)
    app = App(Gateway(fixture.store, fixture.gateway.service, fixture.gateway.registry),
              admin_token=ADMIN_TOKEN, billing_source=source)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        admin.post("/monitor/billing/settings", json={"revision": 0, "enabled": True,
            "multiplier": "1", "client_multipliers": {},
            "open_source_prices": {} if price is None else {"example/open_source": price}}).raise_for_status()
        admin.post("/monitor/mode", json={"revision": app.store.control()["revision"],
            "mode": "open_source_first", "client": "studio"}).raise_for_status()
        admin.post("/v1/tasks", json={"run_id": "zero-cost", "category": CATEGORY,
            "prompt": "Return only 2."}, headers={"Authorization": "Bearer " + TOKENS["studio"]}).raise_for_status()
        result = admin.post("/monitor/billing/reconcile", json={}).json()
        view = admin.get("/monitor/billing").json()
        if reason:
            assert result["charged"] == 0 and result["waiting"] == 1
            assert view["pending"][0]["reason"] == reason
            assert view["entries"] == []
        else:
            assert result["charged"] == 1 and result["waiting"] == 0
            assert view["entries"][0]["amount_usd"] == charge
            assert view["entries"][0]["supplier_cost_usd"] == "0"
            own = admin.get("/v1/billing", headers={"Authorization": "Bearer " + TOKENS["studio"]}).json()
            assert own["entries"][0]["amount_usd"] == "0.0000088"
            assert "supplier_cost_usd" not in own["entries"][0]
            assert "multiplier" not in own["entries"][0]


def test_enabling_billing_never_backcharges_existing_tasks(tmp_path):
    app, upstream = build_demo(tmp_path / "monitor", seed=False)
    upstream.fail_tiers = set()
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        task = {"run_id": "before-billing", "category": CATEGORY, "prompt": "Return only 2."}
        headers = {"Authorization": "Bearer " + TOKENS["studio"]}
        admin.post("/v1/tasks", json=task, headers=headers).raise_for_status()
        admin.post("/monitor/billing/settings", json={"revision": 0, "enabled": True,
            "multiplier": "1", "client_multipliers": {}, "open_source_prices": {
                "example/open_source": {"input_per_million": "0.4", "output_per_million": "0.8"}}}).raise_for_status()
        admin.post("/v1/tasks", json=task, headers=headers).raise_for_status()
        result = admin.post("/monitor/billing/reconcile", json={}).json()
        assert result["charged"] == result["attempted"] == 0
        assert admin.get("/monitor/billing").json()["entries"] == []
        assert len(upstream.calls) == 1


@pytest.mark.parametrize("unsettled_first", [False, True])
def test_bounded_reconciliation_retains_unprocessed_requests_as_pending(tmp_path, unsettled_first):
    fixture, upstream = build_demo(tmp_path / "monitor", seed=False)
    fetched = []
    unsettled = set()

    def accounting(request):
        fetched.append(request.url.params["id"])
        if unsettled_first:
            if len(unsettled) < 20:
                unsettled.add(request.url.params["id"])
            if request.url.params["id"] in unsettled:
                return httpx.Response(404)
        return httpx.Response(200, json={"data": {"id": request.url.params["id"],
            "model": "example/high", "total_cost": "0.02"}})

    source = OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(accounting)),
                                  api_key=SYNTHETIC_CREDENTIAL)
    app = App(Gateway(fixture.store, fixture.gateway.service, fixture.gateway.registry),
              admin_token=ADMIN_TOKEN, billing_source=source)
    with httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
                      headers={"Authorization": "Bearer " + ADMIN_TOKEN}) as admin:
        admin.post("/monitor/billing/settings", json={"revision": 0, "enabled": True,
            "multiplier": "1", "client_multipliers": {}, "open_source_prices": {}}).raise_for_status()
        admin.post("/monitor/mode", json={"revision": app.store.control()["revision"],
            "mode": "fixed_high", "client": "studio"}).raise_for_status()
        for index in range(21):
            admin.post("/v1/tasks", json={"run_id": f"batch-{index}", "category": CATEGORY,
                "prompt": "Return only 2."}, headers={"Authorization": "Bearer " + TOKENS["studio"]}).raise_for_status()
        first = admin.post("/monitor/billing/reconcile", json={}).json()
        assert len(fetched) == 20
        assert first["charged"] == (0 if unsettled_first else 20)
        assert first["waiting"] == (21 if unsettled_first else 1)
        view = admin.get("/monitor/billing").json()
        assert any(p["reason"] == "billing_batch_deferred" for p in view["pending"])
        restarted = App(Gateway(fixture.store, fixture.gateway.service, fixture.gateway.registry),
                        admin_token=ADMIN_TOKEN, billing_source=source)
        with httpx.Client(transport=httpx.WSGITransport(app=restarted), base_url=app.origin,
                         headers=admin.headers) as other:
            second = other.post("/monitor/billing/reconcile", json={}).json()
        assert second["charged"] == 1
        assert second["waiting"] == (20 if unsettled_first else 0)
        assert len(set(fetched)) == 21
        assert len(admin.get("/monitor/billing").json()["pending"]) == (20 if unsettled_first else 0)


def test_concurrent_reconciliation_keeps_reads_available_and_debits_once(tmp_path):
    fixture, _ = build_demo(tmp_path / "monitor", seed=False)
    both_reading, release = threading.Barrier(3, timeout=5), threading.Event()

    def accounting(request):
        both_reading.wait()
        assert release.wait(5)
        return httpx.Response(200, json={"data": {"id": request.url.params["id"],
            "model": "example/high", "total_cost": "0.02"}})

    source = OpenRouterAccounting(httpx.Client(transport=httpx.MockTransport(accounting)),
                                  api_key=SYNTHETIC_CREDENTIAL)
    apps = [App(Gateway(fixture.store, fixture.gateway.service, fixture.gateway.registry),
                admin_token=ADMIN_TOKEN, billing_source=source) for _ in range(2)]
    clients = [httpx.Client(transport=httpx.WSGITransport(app=app), base_url=app.origin,
               headers={"Authorization": "Bearer " + ADMIN_TOKEN}) for app in apps]
    first, second = clients
    try:
        first.post("/monitor/billing/settings", json={"revision": 0, "enabled": True,
            "multiplier": "1.2", "client_multipliers": {}, "open_source_prices": {}}).raise_for_status()
        first.post("/monitor/mode", json={"revision": fixture.store.control()["revision"],
            "mode": "fixed_high", "client": "studio"}).raise_for_status()
        first.post("/v1/tasks", json={"run_id": "concurrent-debit", "category": CATEGORY,
            "prompt": "Return only 2."}, headers={"Authorization": "Bearer " + TOKENS["studio"]}).raise_for_status()
        with ThreadPoolExecutor(3) as workers:
            calls = [workers.submit(client.post, "/monitor/billing/reconcile", json={}) for client in clients]
            try:
                both_reading.wait()
                read = workers.submit(first.get, "/monitor/billing")
                assert read.result(timeout=2).status_code == 200
            finally:
                release.set()
            results = [call.result(timeout=5) for call in calls]
        assert all(result.status_code == 200 for result in results)
        assert sorted(result.json()["charged"] for result in results) == [0, 1]
        entries = second.get("/monitor/billing").json()["entries"]
        assert len(entries) == 1
        assert entries[0]["amount_usd"] == "0.024"
    finally:
        release.set()
        for client in clients:
            client.close()
