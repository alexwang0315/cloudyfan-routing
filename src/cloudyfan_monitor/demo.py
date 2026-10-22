"""Real Core + BudgetedCore + TaskService against MockTransport; no credentials read."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
import uuid

import httpx
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.diagnostics import DiagnosticContract
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.http_api import RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy

from .app import App
from .gateway import Gateway
from .pools import empty_view, from_coverage
from .store import Store, MODES

CATEGORY = "artificial-analysis:coding_index"
ADMIN_TOKEN = "cloudyfan-local-demo-operator-not-a-real-secret"
TOKENS = {client: "local-demo-" + client + "-client-token-not-real" for client in ("studio", "agent", "batch")}


class Upstream:
    def __init__(self):
        self.calls, self.fail_tiers, self.unknown_cost = [], {"open_source", "mid"}, False
        self.lock = threading.Lock()
        self.prefix = uuid.uuid4().hex[:12]
        self.before_response = None

    def __call__(self, request):
        if request.method != "POST":
            raise AssertionError("No accounting/source network is implicit")
        payload = json.loads(request.content)
        with self.lock:
            self.calls.append(payload)
            n = len(self.calls)
        if self.before_response is not None:
            self.before_response(request)
        if request.url.path.endswith("/decisions"):
            stage = next(iter(payload["questions"]))
            choice = "mid" if stage == "low_mid" else "high"
            return httpx.Response(200, json={"id": f"gen-monitor-{self.prefix}-{n}", "model": payload["model"], "provider": "TypeSafe",
                "usage": {"cost": .0001, "input_tokens": 10, "output_tokens": 1},
                "answers": {stage: {"type": "choice", "choice": choice,
                    "probabilities": {k: .8 if k == choice else .2 for k in payload["questions"][stage]["criteria"]}}}})
        tier = payload["model"].split("/")[-1]
        answer = "3" if tier in self.fail_tiers else "2"
        usage = {"prompt_tokens": 20, "completion_tokens": 1}
        if not self.unknown_cost:
            usage["cost"] = {"open_source": .0001, "low": .0003, "mid": .001, "high": .004}[tier]
        return httpx.Response(200, json={"id": f"gen-monitor-{self.prefix}-{n}", "model": payload["model"], "provider": "Example",
            "usage": usage, "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]})


class DemoSource:
    def __init__(self, view):
        self.view = view

    def refresh(self):
        view = json.loads(json.dumps(self.view))
        view["source_kind"] = "offline_demo_refresh_no_external_get"
        view["demo_refreshed_at"] = datetime.now(timezone.utc).isoformat()
        view["model_posts"] = 0
        view["requests"] = []
        view["completeness"] = {"status": "complete", "attempted": 0, "succeeded": 0,
                                 "failed": 0, "deferred": 0, "failures": [], "deferred_models": []}
        return view


def build_demo(root, *, core_root=None, seed=True, origin="http://127.0.0.1:8766", clock=time.time,
               retention_policy=None):
    root = Path(root)
    store = Store(root / "monitor.sqlite", clock=clock)
    upstream = Upstream()
    profiles, decision = example_config()
    for profile in profiles.values():
        profile["execution_location"] = "cloud:fixture"
    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)), "mock-only", root / "receipts")
    core = Core(client, profiles, decision, limits=Limits(max_turns=1, max_tokens=128),
                execution_policy=ExecutionPolicy(mode="unrestricted", origin="home:fixture", classifier="cloud:typesafe"),
                diagnostic_contract=DiagnosticContract("monitor-arithmetic-v1", "demo-public-contract"))
    budget = BudgetStore(root / "budget.sqlite")
    if budget.report()["authorization"] is None:
        budget.authorize(Authorization("mock-only-not-real-budget", Decimal("100"),
                         (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()))
    bounds = {p["model_version"]: EnforcedBound(".01", "MockTransport only") for p in profiles.values()}
    bounds[decision["version"]] = EnforcedBound(".001", "MockTransport only")
    runner = BudgetedCore(core, budget, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *args: True)
    contract = RegisteredContract("monitor-arithmetic-v1", runner, lambda workspace: ArithmeticEnvironment())
    contracts = {"demo-" + mode: replace(contract, mode=mode) for mode in MODES}
    service = TaskService(root / "tasks", contracts=contracts, allowed_principals=set(TOKENS),
                          retention_policy=retention_policy, clock=clock)
    view = empty_view("offline_mock_runtime", "2026-10-22T00:00:00+00:00")
    for tier in profiles:
        view["categories"][CATEGORY]["pools"][tier][0] = {"position": 0, "profile": profiles[tier],
            "status": "mock_only", "quality": profiles[tier]["external_score"], "runtime_qualified": False}
    routing_id = store.save_pool(view)
    registry = {(routing_id, CATEGORY, mode): "demo-" + mode for mode in MODES}
    for client_id, token in TOKENS.items():
        store.register_client(client_id, {"studio": "Studio 工作台", "agent": "Agent / SDK", "batch": "Batch 批次任務"}[client_id], token)
    if store.control()["routing_pool"] is None:
        store.change(revision=store.control()["revision"], routing=routing_id)
    gateway = Gateway(store, service, registry, demo=True)
    coverage = Path(core_root) / "docs/evidence/category-backup-coverage-20261021.json" if core_root else None
    catalog = from_coverage(json.loads(coverage.read_text())) if coverage and coverage.exists() else view
    if store.control()["catalog_pool"] is None:
        store.change(revision=store.control()["revision"], catalog=store.save_pool(catalog))
    if seed and not store.dashboard()["tasks"]:
        for i in range(15):
            principal = list(TOKENS)[i % 3]
            mode = list(MODES)[i % 4]
            gateway.switch(store.control()["revision"], mode, principal)
            upstream.fail_tiers = set() if i % 3 == 0 else {"open_source", "low"} if i % 3 == 1 else {"open_source", "mid"}
            store.authenticate(TOKENS[principal])
            gateway.create(principal, {"run_id": f"demo-{i + 1:03d}", "category": CATEGORY,
                                       "prompt": "Return only the integer sum of 1 and 1."})
        for principal in TOKENS:
            gateway.switch(store.control()["revision"], None, principal)
    return App(gateway, admin_token=ADMIN_TOKEN, source=DemoSource(catalog), origin=origin), upstream
