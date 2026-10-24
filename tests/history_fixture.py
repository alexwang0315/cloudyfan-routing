"""Real backup host with explicitly synthetic evidence and MockTransport only."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import cloudyfan_jev
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.diagnostics import DiagnosticContract
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.http_api import RegisteredContract, TaskService
from cloudyfan_jev.identity import IdentityStore
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore, backup_pool_contract_hash
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.health import HealthStore

from cloudyfan_monitor.integration import ACCESS_POLICY, RunBoundCore, assemble_monitor
from cloudyfan_monitor.pools import empty_view
from cloudyfan_monitor.store import Store

OPERATOR = "synthetic-history-host-operator-not-a-secret"
CONTRACT = "synthetic-history-public-v1"


def backup_fixture(classifiers=(), *, economic=True):
    core_tests = Path(cloudyfan_jev.__file__).resolve().parents[2] / "tests"
    spec = importlib.util.spec_from_file_location("_monitor_history_backup_fixture", core_tests / "test_backup_runtime.py")
    module = importlib.util.module_from_spec(spec)
    original = sys.path[:]
    try:
        sys.path.insert(0, str(core_tests))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original
    plan = module.plan_for(classifiers=classifiers, count=2)

    def execution_locations(value):
        if isinstance(value, dict):
            if isinstance(value.get("profile"), dict):
                value["profile"]["execution_location"] = "cloud:fixture"
            for child in value.values():
                execution_locations(child)
        elif isinstance(value, list):
            for child in value:
                execution_locations(child)

    execution_locations(plan)
    snapshot = plan["snapshot"]
    snapshot["snapshot_id"] = digest({k: v for k, v in snapshot.items() if k != "snapshot_id"})
    plan["snapshot_id"] = snapshot["snapshot_id"]
    privacy = ExecutionPolicy("unrestricted", "home:fixture", "cloud:typesafe")
    diagnostics = DiagnosticContract("synthetic-history-v1", "synthetic-history-validator")
    _, decision = example_config()
    profiles = {tier: rows[0]["entry"]["profile"] for tier, rows in plan["pools"].items()}
    plan["qcs_policy"]["execution_contract"] = Core(None, profiles, decision, limits=Limits(max_turns=1),
        execution_policy=privacy, diagnostic_contract=diagnostics).execution_contract_descriptor(CONTRACT)
    for stage in plan["qcs_policy"]["stages"].values():
        for variant in stage["variants"].values():
            variant["calibration"]["pool_contract_sha256"] = backup_pool_contract_hash(plan)
            if not economic:
                variant["classifier_cost_usd"] = {"lower": ".2", "upper": ".2"}
    module.rebind(plan)
    return {"plan": plan, "module": module, "privacy": privacy, "diagnostics": diagnostics,
            "decision": decision, "now": datetime.fromisoformat(module.NOW)}


@dataclass
class BackupHost:
    app: object
    service: object
    budget: object
    health: object
    credentials: dict = field(repr=False)
    root: Path
    values: dict
    calls: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    scenario: str = "direct"
    before_response: object = None
    operator_token: str = field(default=OPERATOR, repr=False)


def assemble_backup_host(root, origin, *, classifiers=(), economic=True):
    values = backup_fixture(classifiers, economic=economic)
    root, plan = Path(root), values["plan"]
    clock = lambda: values["now"].timestamp()
    store = Store(root / "monitor.sqlite", clock=clock)
    identities = IdentityStore(root / "identity.sqlite", clock=clock,
        pepper=b"synthetic-history-host-pepper-00000000")
    credentials = {}
    for principal in ("studio", "agent"):
        identities.create_principal(principal)
        credentials[principal] = identities.issue(principal, scopes={"tasks:read", "tasks:write"},
            expires_at=clock() + 365 * 86400)
    health = HealthStore(root / "health", policy=values["module"].HEALTH_POLICY, clock=lambda: values["now"])
    health.publish([values["module"].observation(now=values["now"],
        provider=row["entry"]["profile"]["provider"], configuration=row["configuration_sha256"])
        for rows in plan["pools"].values() for row in rows])
    host = BackupHost(None, None, None, health, credentials, root, values)

    def upstream(request):
        host.requests.append({"method": request.method, "path": request.url.path})
        assert request.method == "POST"
        payload = json.loads(request.content)
        host.calls.append(payload)
        if host.before_response is not None:
            host.before_response(request)
        n = len(host.calls)
        if "questions" in payload:
            stage = next(iter(payload["questions"]))
            chosen = "low" if stage == "low_mid" else "high"
            return httpx.Response(200, json={"id": f"gen-history-{n}", "model": payload["model"],
                "provider": "TypeSafe", "usage": {"cost": .001, "input_tokens": 1, "output_tokens": 1}, "answers": {stage: {
                    "type": "choice", "choice": chosen,
                    "probabilities": {key: .9 if key == chosen else .1 for key in payload["questions"][stage]["criteria"]}}}})
        initial = payload["model"].startswith("example/open_source")
        fails = (initial and host.scenario not in ("first_pass", "availability")) or (
            host.scenario == "quality_chain" and not payload["model"].startswith("example/high"))
        answer = "PRIVATE_RESILIENCE_ANSWER" if host.scenario == "private_answer" and initial else "bad" if fails else "2"
        return values["module"].response(payload, n, answer=answer,
            status=503 if host.scenario == "availability" and n == 1 else 200)

    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)), "synthetic-only", root / "receipts")

    def factory(run_id, controlled, local):
        return BackupCategoryCore(controlled, plan, values["decision"], run_id=run_id,
            health_store=health, clock=lambda: values["now"], limits=Limits(max_turns=1),
            local_backend=local, execution_policy=values["privacy"], diagnostic_contract=values["diagnostics"])

    template = factory("history-template", client, None)
    budget = BudgetStore(root / "budget.sqlite", clock=lambda: values["now"])
    budget.authorize(Authorization("synthetic-history-only", Decimal("10"), "2099-01-01T00:00:00Z"))
    bounds = {row["entry"]["profile"]["model_version"]: EnforcedBound(".5", "MockTransport only")
              for rows in plan["pools"].values() for row in rows}
    bounds[values["decision"]["version"]] = EnforcedBound(".5", "MockTransport only")
    runner = BudgetedCore(RunBoundCore(template, factory), budget, model_bounds=bounds,
        tool_bounds={}, verify_bound=lambda *args: True)
    service = TaskService(root / "tasks", contracts={"history-backup-v1": RegisteredContract(
        CONTRACT, runner, lambda workspace: ArithmeticEnvironment())},
        allowed_principals={"studio", "agent"}, principal_active=identities.is_active,
        retention_policy=ACCESS_POLICY, clock=clock)
    pool = store.save_pool(empty_view("synthetic_history_no_real_qualification", values["now"].isoformat()))
    store.change(revision=store.control()["revision"], routing=pool, catalog=pool)
    app = assemble_monitor(store, service, {(pool, plan["category_id"], "cloudyfan"): "history-backup-v1"},
        identities, operator_token=OPERATOR, origin=origin, demo=True)
    host.app, host.service, host.budget = app, service, budget
    return host
