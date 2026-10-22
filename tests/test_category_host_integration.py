"""Real CategoryCore host plumbing; borrowed arithmetic fixtures grant no real qualification."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

import cloudyfan_jev
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.diagnostics import DiagnosticContract
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.http_api import RegisteredContract, TaskService
from cloudyfan_jev.identity import IdentityStore
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.scores.categories import compile_category_matrix
from cloudyfan_jev.scores.category_routing import CategoryCore
from cloudyfan_jev.scores.configuration import configuration_digest

from cloudyfan_monitor.integration import ACCESS_POLICY, RunBoundCore, assemble_monitor
from cloudyfan_monitor.pools import empty_view
from cloudyfan_monitor.store import Store


CONTRACT = "synthetic-category-host-arithmetic-v1"
OPERATOR = "synthetic-category-host-operator-not-a-real-secret"


def category_values():
    """Reuse existing offline evidence instead of inventing real model admission data."""
    core_tests = Path(cloudyfan_jev.__file__).resolve().parents[2] / "tests"
    spec = importlib.util.spec_from_file_location(
        "_cloudyfan_category_host_offline_fixture", core_tests / "test_category_routing.py")
    module = importlib.util.module_from_spec(spec)
    original_path = sys.path[:]
    try:
        sys.path.insert(0, str(core_tests))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    _, requirements, evidence, policy = module.settings()
    observations, registries, policies = module.fixture()
    for registry in registries.values():
        for entry in registry["candidates"] + registry["open_source_candidates"]:
            entry["profile"]["execution_location"] = "cloud:fixture"
    matrix = compile_category_matrix(observations, registries, policies, now=module.NOW)
    for tier, entries in matrix["categories"][module.CATEGORY]["pools"].items():
        configuration = configuration_digest(entries[0])
        evidence[entries[0]["config_id"]]["configuration_sha256"] = configuration
        policy["calibration"]["configurations"][tier] = configuration
    profiles, decision = example_config()
    limits = Limits(max_turns=1)
    privacy = ExecutionPolicy(mode="unrestricted", origin="home:fixture", classifier="cloud:typesafe")
    diagnostics = DiagnosticContract("category-host-arithmetic-v1", "synthetic-public-contract")
    policy["execution_contract"] = Core(None, profiles, decision, limits=limits,
        execution_policy=privacy, diagnostic_contract=diagnostics).execution_contract_descriptor(CONTRACT)
    module.bind(matrix, module.CATEGORY, requirements, evidence, policy)
    return {"matrix": matrix, "requirements": requirements, "evidence": evidence, "policy": policy,
            "decision": decision, "limits": limits, "privacy": privacy, "diagnostics": diagnostics,
            "category": module.CATEGORY, "now": module.NOW}


@pytest.fixture
def category_fixture():
    return category_values()


def assemble_category_host(root, values, requests, *, clock=None):
    instant = datetime.fromisoformat(values["now"])
    if clock is None:
        clock = lambda: instant.timestamp()
    store = Store(root / "monitor.sqlite", clock=clock)
    identities = IdentityStore(root / "identity.sqlite", clock=clock,
                                pepper=b"synthetic-category-host-pepper-only-000000")
    credentials = {}
    for principal in ("studio", "agent"):
        try:
            identities.principal(principal)
        except ValueError:
            identities.create_principal(principal)
        credentials[principal] = identities.issue(principal, scopes={"tasks:read", "tasks:write"},
                                                   expires_at=clock() + 365 * 86400)

    def upstream(request):
        assert request.method == "POST"
        assert not request.url.path.endswith("/decisions")
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={"id": f"gen-category-host-{len(requests)}",
            "model": payload["model"], "provider": "Example", "usage": {"cost": ".01"},
            "choices": [{"message": {"role": "assistant", "content": "2"}}]})

    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)), "synthetic-only", root / "receipts")

    def factory(run_id, controlled_client, local_backend):
        return CategoryCore(controlled_client, values["matrix"], values["category"], values["requirements"],
            values["evidence"], values["policy"], values["decision"], run_id=run_id,
            clock=lambda: values["now"], limits=values["limits"], local_backend=local_backend,
            execution_policy=values["privacy"], diagnostic_contract=values["diagnostics"])

    template = factory("synthetic-host-template", client, None)
    budget = BudgetStore(root / "budget.sqlite", clock=lambda: instant)
    if budget.report()["authorization"] is None:
        budget.authorize(Authorization("synthetic-category-host-only", Decimal("2"),
                                       "2099-01-01T00:00:00+00:00"))
    bounds = {profile["model_version"]: EnforcedBound(".1", "MockTransport arithmetic fixture only")
              for profile in template.profiles.values()}
    bounds[values["decision"]["version"]] = EnforcedBound(".1", "MockTransport arithmetic fixture only")
    runner = BudgetedCore(RunBoundCore(template, factory), budget, model_bounds=bounds,
                           tool_bounds={}, verify_bound=lambda *args: True)
    contract = RegisteredContract(CONTRACT, runner, lambda workspace: ArithmeticEnvironment())
    service = TaskService(root / "tasks", contracts={"category-host-v1": contract},
                          allowed_principals={"studio", "agent"}, principal_active=identities.is_active,
                          retention_policy=ACCESS_POLICY, clock=clock)
    view = empty_view("synthetic_category_host_fixture_no_real_qualification", values["now"])
    pool = store.save_pool(view)
    if store.control()["routing_pool"] is None:
        store.change(revision=store.control()["revision"], routing=pool, catalog=pool)
    registry = {(pool, values["category"], "cloudyfan"): "category-host-v1"}
    app = assemble_monitor(store, service, registry, identities, operator_token=OPERATOR,
                           origin="http://127.0.0.1:8766", demo=True)
    return app, service, budget, credentials


def test_real_category_core_binds_two_http_clients_and_reopens_original_results(tmp_path, category_fixture):
    requests = []
    values = deepcopy(category_fixture)
    app, service, budget, credentials = assemble_category_host(tmp_path, values, requests)
    descriptor = json.loads(json.dumps(service.contracts["category-host-v1"].descriptor()))
    manifests = {}
    jobs = {}
    principals = (("studio", "category-first"), ("agent", "category-second"))
    for principal, run in principals:
        with httpx.Client(transport=httpx.WSGITransport(app), base_url=app.origin,
                          headers={"Authorization": "Bearer " + credentials[principal].token}) as http:
            payload = {"run_id": run, "category": values["category"], "prompt": "Return only 1+1."}
            created = http.post("/v1/tasks", json=payload)
            assert created.status_code == 200, created.json()
            assert created.json()["status"] == "passed"
            dispatched = len(requests)
            untrusted = http.post("/v1/tasks", json={**payload, "run_id": "untrusted-" + run,
                                                     "workload_id": "caller-cannot-select-calibration"})
            assert untrusted.status_code == 422
            assert len(requests) == dispatched
            result = http.get(f"/v1/tasks/{run}/result")
            assert result.status_code == 200 and result.json()["answer"] == "2"
            assert result.json()["costs"]["complete_api_usd"] == "0.01"
            internal = service.internal_id(principal, run)
            manifest_path = tmp_path / "receipts" / "runs" / internal / "manifest.json"
            job_path = service.root / internal / "job.json"
            manifests[internal] = manifest_path.read_bytes()
            jobs[internal] = job_path.read_bytes()
            manifest = json.loads(manifests[internal])
            assert manifest["contract_version"] == CONTRACT
            assert manifest["mode"] == "cloudyfan"
            assert manifest["task"]["category"] == values["category"]
            assert manifest["task"]["workload_id"] == values["policy"]["workload_id"]
            assert manifest["task"]["category_binding"] == {
                "schema_version": "category-qcs-v1", "category_id": values["category"],
                "matrix_id": values["matrix"]["matrix_id"],
                "snapshot_id": values["matrix"]["categories"][values["category"]]["snapshot"]["snapshot_id"],
            }
            job = json.loads(jobs[internal])
            assert job["request"]["run_id"] == run
            assert job["registered"] == descriptor

    assert len(manifests) == 2
    assert len(requests) == 2
    report = budget.report()
    assert report["known_usd"] == "0.02"
    assert {event["run"] for event in report["events"]} == set(manifests)
    assert all(item["status"] == "complete" for item in report["reservations"])

    reopened, reopened_service, reopened_budget, _ = assemble_category_host(tmp_path, values, requests)
    assert json.loads(json.dumps(reopened_service.contracts["category-host-v1"].descriptor())) == descriptor
    for principal, run in principals:
        with httpx.Client(transport=httpx.WSGITransport(reopened), base_url=reopened.origin,
                          headers={"Authorization": "Bearer " + credentials[principal].token}) as http:
            result = http.get(f"/v1/tasks/{run}/result")
            assert result.status_code == 200 and result.json()["answer"] == "2"
            other_run = "category-second" if principal == "studio" else "category-first"
            assert http.get(f"/v1/tasks/{other_run}/result").status_code == 404
            duplicate = http.post("/v1/tasks", json={"run_id": run, "category": values["category"],
                                                    "prompt": "Return only 1+1."})
            assert duplicate.status_code == 200 and duplicate.json()["status"] == "passed"
            assert http.post(f"/v1/tasks/{run}/resume", json={}).json()["status"] == "passed"
    assert len(requests) == 2
    assert reopened_budget.report()["known_usd"] == "0.02"
    for internal in manifests:
        assert (tmp_path / "receipts" / "runs" / internal / "manifest.json").read_bytes() == manifests[internal]
        assert (reopened_service.root / internal / "job.json").read_bytes() == jobs[internal]
