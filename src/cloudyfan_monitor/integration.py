"""Explicit host assembly and a separate synthetic local integration environment.

No service is started and no real credentials or model APIs are accessed here.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import time

from cloudyfan_jev.engine import Core
from cloudyfan_jev.http_api import TaskService
from cloudyfan_jev.identity import IdentityStore
from cloudyfan_jev.retention import RetentionPolicy

from .app import App
from .demo import ADMIN_TOKEN, CATEGORY, TOKENS, build_demo
from .gateway import Gateway
from .store import MODES, Store

ACCESS_SECONDS = 30 * 24 * 60 * 60
ACCESS_POLICY = RetentionPolicy("monitor-task-access-30d-v1", ACCESS_SECONDS)


class RunBoundCore(Core):
    """Bind a trusted, descriptor-pinned Core factory to each internal run ID.

    A host can use this with CategoryCore/BackupCategoryCore, whose constructors
    require a run identity. The factory receives the budget-wrapped client, so it
    cannot accidentally bypass the existing BudgetedCore instrumentation.
    """
    def __init__(self, template, factory):
        if not isinstance(template, Core) or not callable(factory):
            raise ValueError("Controlled Core template and factory required")
        self.__dict__.update(template.__dict__)
        self._template, self._factory = template, factory

    def __getattr__(self, name):
        template = self.__dict__.get("_template")
        if template is None:
            raise AttributeError(name)
        return getattr(template, name)

    def configuration_descriptor(self):
        return deepcopy(self._template.configuration_descriptor())

    def _bound(self, run_id):
        core = self._factory(run_id, self.client, self.local_backend)
        if (not isinstance(core, Core) or core.client is not self.client
                or core.local_backend is not self.local_backend
                or core.configuration_descriptor() != self.configuration_descriptor()):
            raise ValueError("Run factory changed the registered contract or budgeted transports")
        return core

    def queue_template(self):
        return self._template

    def queue_binding(self, run_id, task):
        core = self._bound(run_id)
        descriptor = core.configuration_descriptor()
        policies = [descriptor.get("selection_policy", {}).get("policy", {}),
                    descriptor.get("backup_plan", {}).get("qcs_policy", {})]
        workloads = {policy["workload_id"] for policy in policies if policy}
        if workloads:
            if len(workloads) != 1 or not all(isinstance(value, str) and value.strip() for value in workloads):
                raise ValueError("One frozen host workload required")
            workload = next(iter(workloads))
            if "workload_id" in task and task["workload_id"] != workload:
                raise ValueError("Task workload differs from the registered host contract")
            task = {**deepcopy(task), "workload_id": workload}
        return core, task

    def run(self, run_id, task, environment_factory, *, contract_version, mode="cloudyfan"):
        core, task = self.queue_binding(run_id, task)
        return core.run(run_id, task, environment_factory, contract_version=contract_version, mode=mode)

    def read_completed(self, run_id):
        return self._bound(run_id).read_completed(run_id)


def assemble_monitor(store, service, registry, identities, *, operator_token, source=None,
                     origin="http://127.0.0.1:8766", demo=False, labels=None, billing_source=None,
                     ollama_models=None, simulation=None, candidate_observer=None, task_observer=None,
                     capacity_observer=None, capacity_controller=None, queue=None):
    """Consume an explicitly controlled host service; never create qualifications."""
    if (not isinstance(store, Store) or not isinstance(service, TaskService)
            or not isinstance(identities, IdentityStore)):
        raise ValueError("Controlled monitor store, TaskService and IdentityStore required")
    if service.retention_policy is None or service.retention_policy.ttl_seconds != ACCESS_SECONDS:
        raise ValueError("Explicit 30-day TaskService retention required; no implicit legacy migration")
    host_active = service.principal_active
    registry_active = identities.is_active
    if (host_active is None or (getattr(host_active, "__self__", None) is identities
            and getattr(host_active, "__func__", None) is getattr(registry_active, "__func__", None))):
        service.principal_active = registry_active
    else:
        service.principal_active = lambda principal: (registry_active(principal) is True
            and host_active(principal) is True)
    for principal in sorted(service.allowed_principals):
        identities.principal(principal)
        store.register_principal(principal, (labels or {}).get(principal, principal))
    service.retention_protected_paths = tuple(set(service.retention_protected_paths)
                                               | {identities.path.resolve(), store.path.resolve()})
    if capacity_observer is not None:
        from .capacity import CapacityObserver
        if type(capacity_observer) is not CapacityObserver:
            raise ValueError("Controlled capacity source observer required")
        service.retention_protected_paths = tuple(set(service.retention_protected_paths)
            | {capacity_observer.store.path.resolve(), capacity_observer.store.path.with_suffix(".refresh.lock").resolve()})
    if capacity_controller is not None:
        from cloudyfan_jev.capacity_dispatch import CapacityController
        if (type(capacity_controller) is not CapacityController or capacity_observer is None
                or capacity_controller.store is not capacity_observer.store):
            raise ValueError("Controlled dispatch and source evidence must share a store")
        service.capacity_controller = capacity_controller
        for contract in service.contracts.values():
            contract.runner.capacity_controller = capacity_controller
    if task_observer is None:
        from .history_observation import TaskDecisionObserver
        task_observer = TaskDecisionObserver(service, store, registry)
    if queue is not None:
        from cloudyfan_jev.scheduler import QueueScheduler
        if type(queue) is not QueueScheduler or queue.service is not service or service.queue_scheduler is not queue:
            raise ValueError('Controlled queue must share the exact service')
    return App(Gateway(store, service, registry, demo=demo, queue=queue), admin_token=operator_token,
               source=source, origin=origin, identity_store=identities, billing_source=billing_source,
               ollama_models=ollama_models, simulation=simulation, candidate_observer=candidate_observer,
               task_observer=task_observer, capacity_observer=capacity_observer, capacity_controller=capacity_controller)


@dataclass
class LocalIntegration:
    app: App
    upstream: object
    identity: IdentityStore
    credentials: dict = field(repr=False)
    root: Path
    operator_token: str = field(default=ADMIN_TOKEN, repr=False)


def build_local_integration(root, *, origin="http://127.0.0.1:8766", clock=time.time,
                            core_root=None, seed=False):
    """Synthetic fixture only: fixed mock pepper, mock models and synthetic fees.

    Use a dedicated root. Existing legacy task directories without lifecycle
    records are refused, not silently adopted or granted a fresh 30-day period.
    """
    root = Path(root)
    tasks = root / "tasks"
    if tasks.exists() and not (tasks / ".retention").exists() and any(tasks.iterdir()):
        raise ValueError("Use a separate integration root; legacy tasks cannot be auto-migrated")
    original, upstream = build_demo(root, core_root=core_root, seed=False, origin=origin, clock=clock,
                                     retention_policy=ACCESS_POLICY)
    identities = IdentityStore(root / "identities.sqlite",
                                pepper=b"synthetic-local-integration-pepper-000000", clock=clock)
    credentials = {}
    for principal in TOKENS:
        try:
            identities.principal(principal)
        except ValueError:
            identities.create_principal(principal)
        credentials[principal] = identities.issue(principal, scopes={"tasks:read", "tasks:write"},
                                                   expires_at=clock() + 365 * 86400)
    service = TaskService(tasks, contracts=original.gateway.service.contracts,
                          allowed_principals=set(TOKENS), principal_active=identities.is_active,
                          retention_policy=ACCESS_POLICY, clock=clock,
                          retention_protected_paths=(identities.path, original.store.path))
    app = assemble_monitor(original.store, service, original.gateway.registry, identities,
                            operator_token=ADMIN_TOKEN, source=original.source, origin=origin, demo=True)
    if seed and not app.store.dashboard()["tasks"]:
        for index in range(12):
            principal = list(TOKENS)[index % len(TOKENS)]
            app.gateway.switch(app.store.control()["revision"], list(MODES)[index % 4], principal)
            upstream.fail_tiers = set() if index % 3 == 0 else {"open_source", "mid"}
            app.store.touch_client(principal)
            app.gateway.create(principal, {"run_id": f"integrated-{index + 1:03d}", "category": CATEGORY,
                                            "prompt": "Return only the integer sum of 1 and 1."})
        for principal in TOKENS:
            app.gateway.switch(app.store.control()["revision"], None, principal)
    return LocalIntegration(app, upstream, identities, credentials, root)
