"""Synthetic real BackupCategoryCore/HealthStore host; no supplier qualification."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys

import cloudyfan_jev
import httpx
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.diagnostics import DiagnosticContract
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.http_api import RegisteredContract, TaskService
from cloudyfan_jev.identity import IdentityStore
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.health import HealthStore

from cloudyfan_monitor.integration import ACCESS_POLICY, RunBoundCore, assemble_monitor
from cloudyfan_monitor.pools import empty_view
from cloudyfan_monitor.store import Store

OPERATOR = "synthetic-candidate-health-operator-not-a-secret"
CONTRACT = "synthetic-health-public-v1"


def health_values():
    core_tests = Path(cloudyfan_jev.__file__).resolve().parents[2] / "tests"
    spec = importlib.util.spec_from_file_location("_monitor_candidate_health_fixture", core_tests / "test_backup_runtime.py")
    module = importlib.util.module_from_spec(spec)
    original = sys.path[:]
    try:
        sys.path.insert(0, str(core_tests))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original
    plan = module.plan_for(count=3)

    def locations(value):
        if isinstance(value, dict):
            if isinstance(value.get("profile"), dict):
                value["profile"]["execution_location"] = "cloud:fixture"
            for child in value.values():
                locations(child)
        elif isinstance(value, list):
            for child in value:
                locations(child)

    locations(plan)
    snapshot = plan["snapshot"]
    snapshot["snapshot_id"] = digest({key: value for key, value in snapshot.items() if key != "snapshot_id"})
    plan["snapshot_id"] = snapshot["snapshot_id"]
    privacy = ExecutionPolicy("unrestricted", "home:fixture", "cloud:typesafe")
    diagnostics = DiagnosticContract("synthetic-health-v1", "synthetic-health-validator")
    _, decision = example_config()
    profiles = {tier: rows[0]["entry"]["profile"] for tier, rows in plan["pools"].items()}
    plan["qcs_policy"]["execution_contract"] = Core(None, profiles, decision, limits=Limits(max_turns=1),
        execution_policy=privacy, diagnostic_contract=diagnostics).execution_contract_descriptor(CONTRACT)
    module.rebind(plan)
    return {"plan": plan, "module": module, "privacy": privacy, "diagnostics": diagnostics,
            "decision": decision, "now": module.NOW}


@dataclass
class HealthHost:
    app: object
    service: object
    budget: object
    health: HealthStore
    credentials: dict = field(repr=False)
    root: Path
    pools: tuple
    values: dict
    clock: object
    calls: list = field(default_factory=list)
    operator_token: str = field(default=OPERATOR, repr=False)

    @property
    def category(self):
        return self.values["plan"]["category_id"]

    def observation(self, tier="open_source", position=0):
        row = self.values["plan"]["pools"][tier][position]
        return self.values["module"].observation(now=datetime.fromtimestamp(self.clock(), timezone.utc),
            provider=row["entry"]["profile"]["provider"], configuration=row["configuration_sha256"])


def assemble_health_host(root, origin, *, clock, publish_initial=True):
    root, values = Path(root), health_values()
    plan = values["plan"]
    store = Store(root / "monitor.sqlite", clock=clock)
    identities = IdentityStore(root / "identity.sqlite", clock=clock,
        pepper=b"synthetic-candidate-health-pepper-00000")
    credentials = {}
    for principal in ("studio", "agent"):
        identities.create_principal(principal)
        credentials[principal] = identities.issue(principal, scopes={"tasks:read", "tasks:write"},
            expires_at=clock() + 365 * 86400)
    now = lambda: datetime.fromtimestamp(clock(), timezone.utc)
    health = HealthStore(root / "health", policy=values["module"].HEALTH_POLICY, clock=now)
    host = HealthHost(None, None, None, health, credentials, root, (), values, clock)
    # Independent batches allow one disposable bad snapshot without damaging others.
    if publish_initial:
        for tier, rows in plan["pools"].items():
            for position in range(len(rows)):
                health.publish([host.observation(tier, position)])

    def upstream(request):
        payload = json.loads(request.content) if request.content else {}
        host.calls.append({"method": request.method, "path": request.url.path, "payload": payload})
        assert request.method == "POST"
        return values["module"].response(payload, len(host.calls))

    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)), "synthetic-only", root / "receipts")

    def factory(run_id, controlled, local):
        return BackupCategoryCore(controlled, plan, values["decision"], run_id=run_id,
            health_store=health, clock=now, limits=Limits(max_turns=1), local_backend=local,
            execution_policy=values["privacy"], diagnostic_contract=values["diagnostics"])

    template = factory("health-template", client, None)
    budget = BudgetStore(root / "budget.sqlite", clock=now)
    budget.authorize(Authorization("synthetic-health-only", Decimal("10"), "2099-01-01T00:00:00Z"))
    bounds = {row["entry"]["profile"]["model_version"]: EnforcedBound(".5", "MockTransport only")
              for rows in plan["pools"].values() for row in rows}
    bounds[values["decision"]["version"]] = EnforcedBound(".5", "MockTransport only")
    runner = BudgetedCore(RunBoundCore(template, factory), budget, model_bounds=bounds,
        tool_bounds={}, verify_bound=lambda *args: True)
    service = TaskService(root / "tasks", contracts={"health-backup-v1": RegisteredContract(
        CONTRACT, runner, lambda workspace: ArithmeticEnvironment())},
        allowed_principals={"studio", "agent"}, principal_active=identities.is_active,
        retention_policy=ACCESS_POLICY, clock=clock)
    view = empty_view("synthetic_candidate_health_no_real_qualification", values["now"])
    for tier, rows in plan["pools"].items():
        for position, row in enumerate(rows):
            view["categories"][host.category]["pools"][tier][position] = {
                "position": position, "status": "mock_only", "profile": deepcopy(row["entry"]["profile"]),
                "config_id": row["entry"]["config_id"]}
    active = store.save_pool(view)
    proposal = deepcopy(view)
    proposal["source_kind"] = "synthetic_unregistered_candidate_health_proposal"
    inactive = store.save_pool(proposal)
    store.change(revision=store.control()["revision"], routing=active, catalog=active)
    app = assemble_monitor(store, service, {(active, host.category, "cloudyfan"): "health-backup-v1"},
        identities, operator_token=OPERATOR, origin=origin, demo=True)
    host.app, host.service, host.budget, host.pools = app, service, budget, (active, inactive)
    return host
