from dataclasses import replace

import httpx
from cloudyfan_jev.budget import BudgetedCore
from cloudyfan_jev.engine import Core

from cloudyfan_monitor.demo import CATEGORY
from cloudyfan_monitor.integration import RunBoundCore, assemble_monitor, build_local_integration


class SingleRunCore(Core):
    def __init__(self, *args, run_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.bound_run_id = run_id

    def run(self, run_id, *args, **kwargs):
        if run_id != self.bound_run_id:
            raise ValueError("Core must be assembled for the actual internal task identity")
        return super().run(run_id, *args, **kwargs)


def test_run_bound_contract_supports_independent_internal_task_identities(tmp_path):
    bundle = build_local_integration(tmp_path)
    bundle.upstream.fail_tiers = set()
    service = bundle.app.gateway.service
    original = service.contracts["demo-cloudyfan"]
    template = original.runner.core

    def factory(run_id, client, local):
        return SingleRunCore(client, template.profiles, template.decision, run_id=run_id,
                              limits=template.limits, system=template.system, local_backend=local,
                              diagnostic_contract=template.diagnostic_contract,
                              execution_policy=template.execution_policy)

    runner = BudgetedCore(RunBoundCore(template, factory), original.runner.store,
                           model_bounds=original.runner.model_bounds, tool_bounds={},
                           verify_bound=original.runner.verify_bound)
    service.contracts["demo-cloudyfan"] = replace(original, runner=runner)
    client = httpx.Client(transport=httpx.WSGITransport(bundle.app), base_url=bundle.app.origin,
                          headers={"Authorization": "Bearer " + bundle.credentials["studio"].token})
    for run in ("factory-first", "factory-second"):
        response = client.post("/v1/tasks", json={"run_id": run, "category": CATEGORY,
                                                 "prompt": "Return only the integer sum of 1 and 1."})
        assert response.status_code == 200
        assert response.json()["status"] == "passed"
        result = client.get(f"/v1/tasks/{run}/result")
        assert result.status_code == 200
        assert result.json()["answer"] == "2"
    assert len(bundle.upstream.calls) == 2


def test_run_factory_cannot_bypass_the_budgeted_transport(tmp_path):
    bundle = build_local_integration(tmp_path)
    service = bundle.app.gateway.service
    original = service.contracts["demo-cloudyfan"]
    template = original.runner.core
    wrapper = RunBoundCore(template, lambda *_: template)
    runner = BudgetedCore(wrapper, original.runner.store, model_bounds=original.runner.model_bounds,
                           tool_bounds={}, verify_bound=original.runner.verify_bound)
    service.contracts["demo-cloudyfan"] = replace(original, runner=runner)
    client = httpx.Client(transport=httpx.WSGITransport(bundle.app), base_url=bundle.app.origin,
                          headers={"Authorization": "Bearer " + bundle.credentials["studio"].token})
    response = client.post("/v1/tasks", json={"run_id": "unsafe-factory", "category": CATEGORY,
                                             "prompt": "Return only the integer sum of 1 and 1."})
    assert response.status_code == 503
    assert not bundle.upstream.calls


def test_host_authorization_restriction_survives_monitor_assembly(tmp_path):
    bundle = build_local_integration(tmp_path)
    service = bundle.app.gateway.service
    active = {"studio": False, "agent": True}
    service.principal_active = lambda principal: active.get(principal, False)
    app = assemble_monitor(bundle.app.store, service, bundle.app.gateway.registry, bundle.identity,
                           operator_token=bundle.operator_token, demo=True)
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url=app.origin,
                          headers={"Authorization": "Bearer " + bundle.credentials["studio"].token})
    assert client.get("/v1/monitor").status_code == 401
    active["studio"] = True
    assert client.get("/v1/monitor").status_code == 200
    bundle.identity.set_active("studio", False)
    assert client.get("/v1/monitor").status_code == 401
    assert not bundle.upstream.calls
