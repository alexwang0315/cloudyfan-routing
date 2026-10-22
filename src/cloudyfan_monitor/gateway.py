"""A controlled Core TaskService adapter, not a second routing algorithm."""
from decimal import Decimal
from types import MappingProxyType
import hashlib
import json
import threading
from urllib.parse import urlsplit

from cloudyfan_jev.budget import _fingerprint
from cloudyfan_jev.client import usage
from cloudyfan_jev.http_api import ServiceError

from .admission import Admission
from .retention_policy import DAY, EvidenceRetentionPolicy
from .store import MODES, MonitorError, digest


def _bound_task_id(binding):
    # TaskService's stable identity scheme, only for lifecycle/public ledger metadata.
    # Raw results and dispatch still require the service's current authorization.
    identity = json.dumps([binding["client"], binding["run"]], sort_keys=True,
                          ensure_ascii=False, allow_nan=False).encode()
    return "http-" + hashlib.sha256(identity).hexdigest()[:48]


class Gateway:
    def __init__(self, store, service, registry, *, demo=False):
        self.store, self.service, self.demo = store, service, demo
        self.registry = MappingProxyType(dict(registry))
        self.refresh_lock = threading.Lock()
        self.admission = Admission(store.path.with_suffix(".execution.lock"))
        self.retention_policy = EvidenceRetentionPolicy()
        # Each key binds a previously registered, exact immutable contract to a mode.
        for (pool, category, mode), contract_id in self.registry.items():
            if mode not in MODES or service.contracts[contract_id].mode != mode:
                raise ValueError("Pool mode must match the registered Core contract")
            view = store.pool(pool)
            if category not in view["categories"]:
                raise ValueError("Unknown pool category")
            service.contracts[contract_id].descriptor()

    def mode_available(self, mode):
        pool = self.store.control()["routing_pool"]
        return any(p == pool and m == mode for p, _, m in self.registry)

    def switch(self, revision, mode, client=None):
        if mode is not None and not self.mode_available(mode):
            raise MonitorError("mode_not_registered_for_active_pool", 409)
        if client is None and mode is None:
            raise MonitorError("global_mode_required")
        return self.store.change(revision=revision, mode=mode, client=client,
                                 inherit=client is not None and mode is None)

    def activate(self, pool, revision):
        self.store.pool(pool)
        if not any(key[0] == pool for key in self.registry):
            raise MonitorError("candidate_data_has_no_registered_runtime_contracts", 409)
        config = self.store.control()
        modes = {c["effective_mode"] for c in self.store.dashboard()["clients"]} | {config["mode"]}
        if any(not any(p == pool and m == mode for p, _, m in self.registry) for mode in modes):
            raise MonitorError("new_pool_missing_configured_modes", 409)
        return self.store.change(revision=revision, routing=pool)

    def create(self, principal, payload):
        self.service.authorize(principal)
        prior = (self.store.existing_binding(principal, payload.get("run_id"))
                 if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) else None)
        if prior is not None:
            binding = self.store.bind(principal, payload, self.registry)
            return self.status(principal, binding["run"])
        try:
            with self.admission.acquire():
                prior = (self.store.existing_binding(principal, payload.get("run_id"))
                         if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) else None)
                binding = self.store.bind(principal, payload, self.registry)
                if prior is not None:
                    return self.status(principal, binding["run"])
                self.store.record(binding, {"status": "bound"})
                try:
                    self.service.create(principal, {"run_id": binding["run"], "contract_id": binding["contract"],
                                                    "prompt": payload["prompt"]})
                except Exception:
                    self.capture(binding, unavailable=True)
                    raise
                self.capture(binding)
                return self._execute(binding)
        except MonitorError as error:
            if error.code != "service_busy":
                raise
            prior = (self.store.existing_binding(principal, payload.get("run_id"))
                     if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) else None)
            if prior is None:
                raise
            binding = self.store.bind(principal, payload, self.registry)
            return self.status(principal, binding["run"])

    def status(self, principal, run):
        self.service.authorize(principal)
        binding = self.store.binding(principal, run)
        expired = self._expiry(binding)
        if expired:
            self.capture(binding)
            raise ServiceError(expired, 410)
        if self.registry.get((binding["pool"], binding["category"], binding["mode"])) != binding["contract"]:
            raise MonitorError("frozen_runtime_contract_unavailable", 409)
        try:
            status = self.service.status(principal, run)
            if status["status"] in ("bound", "ready", "running", "queued"):
                status["costs"] = {**status.get("costs", {}), "complete_api_usd": None}
            return status
        except ServiceError as error:
            if error.code == "task_busy":
                previous = self.store.observation(principal, run) or {"status": "running"}
                return {"run_id": run, "status": previous["status"], "observation": "busy",
                        "costs": {key: previous.get(key) for key in
                                  ("known_api_usd", "complete_api_usd", "unknown_events")}}
            if error.status == 410:
                self.capture(binding)
            raise

    def _expiry(self, binding):
        previous = self.store.observation(binding["client"], binding["run"]) or {}
        if previous.get("access_expired"):
            return previous.get("error_code") or "task_expired"
        if self.service.retention is not None:
            lifecycle = self.service.retention.lookup(_bound_task_id(binding))
            if lifecycle:
                if lifecycle["state"] == "retired":
                    return "task_retired"
                if lifecycle["state"] == "expired" or self.store.clock() >= lifecycle["expires_at"]:
                    return "task_expired"
        return None

    def execute(self, principal, run):
        self.service.authorize(principal)
        binding = self.store.binding(principal, run)
        status = self.status(principal, run)
        if status["status"] in ("passed", "failed", "unverifiable"):
            return status
        with self.admission.acquire():
            return self._execute(binding)

    def _execute(self, binding):
        principal, run = binding["client"], binding["run"]
        if self.registry.get((binding["pool"], binding["category"], binding["mode"])) != binding["contract"]:
            raise MonitorError("frozen_runtime_contract_unavailable", 409)
        self.store.record(binding, {"status": "running", "complete_api_usd": None})
        self.store.record_version_use(binding)
        try:
            return self.service.execute(principal, run)
        finally:
            self.store.record_version_use(binding)
            self.capture(binding)

    def _completed_summary(self, binding, *, expired=False):
        if not expired:
            self.service.result(binding["client"], binding["run"], refresh=False)
        contract = self.service.contracts[binding["contract"]]
        result = contract.runner.core.read_completed(self.service.internal_id(binding["client"], binding["run"]))
        return {"completed_status": result["status"], "first_pass": result["first_public_pass"],
                "final_pass": result["final_public_pass"], "wall_seconds": result.get("wall_seconds"),
                "generation_attempts": len(result["attempts"]),
                "classifier_calls": (result.get("selection") or {}).get("classifier_calls", 0),
                "path": [{"tier": a["tier"], "passed": a["public"].get("passed"), "stop": a.get("stop"),
                          "models": sorted({c.get("model") for c in a["calls"] if c.get("model")})}
                         for a in result["attempts"]]}

    def _task_costs(self, binding):
        contract = self.service.contracts[binding["contract"]]
        internal = self.service.internal_id(binding["client"], binding["run"])
        events = [event for event in contract.runner.store.report()["events"] if event["run"] == internal]
        known = sum((Decimal(event["cost"]) for event in events if event["cost"] is not None), Decimal(0))
        unknown = sum(event["cost"] is None for event in events)
        return {"known_api_usd": str(known), "complete_api_usd": None if unknown else str(known),
                "unknown_events": unknown}

    def capture(self, binding, unavailable=False):
        principal, run = binding["client"], binding["run"]
        previous = self.store.observation(principal, run) or {}
        summary = {"status": "engineering_interrupted", "error_code": "monitor_evidence_unavailable"}
        try:
            if unavailable:
                raise ValueError("creation interrupted")
            expired = self._expiry(binding)
            if expired:
                raise ServiceError(expired, 410)
            status = self.service.status(principal, run)
            costs = status.get("costs", {})
            summary = {"status": status["status"], "known_api_usd": costs.get("known_api_usd"),
                       "complete_api_usd": costs.get("complete_api_usd"),
                       "unknown_events": costs.get("unknown_events"),
                       "error_code": (status.get("error") or {}).get("code"), "observation": None}
            if status["status"] in ("bound", "ready", "running", "queued"):
                summary["complete_api_usd"] = None
            if status["status"] in ("passed", "failed", "unverifiable"):
                summary.update(self._completed_summary(binding))
        except ServiceError as error:
            if error.code == "task_busy":
                summary = {"status": previous.get("status") or "running", "observation": "busy"}
            elif error.status == 401:
                # Identity suspension does not invalidate a previously verified public record.
                # Do not bypass authorization to reread its original result.
                summary = {"status": previous.get("status") or "engineering_interrupted",
                           "observation": "client_inactive"}
            elif error.status == 410:
                summary = {"status": "expired", "access_expired": True, "error_code": error.code,
                           "observation": None}
                try:
                    self.service.authorize(principal)
                    summary.update(self._task_costs(binding))
                    summary.update(self._completed_summary(binding, expired=True))
                except ServiceError as denied:
                    if denied.status == 401:
                        summary["observation"] = "client_inactive"
                except Exception:
                    pass  # Retain previous public evidence; never clear it on expiry.
            else:
                summary.update({"status": "engineering_interrupted", "error_code": error.code,
                                "first_pass": None, "final_pass": None,
                                "completed_status": None, "observation": "evidence_unavailable"})
        except Exception:
            # Do not turn incomplete evidence into zero cost, success, or an invented path.
            summary.update({"status": "engineering_interrupted", "error_code": "monitor_evidence_unavailable",
                            "first_pass": None, "final_pass": None, "completed_status": None,
                            "observation": "evidence_unavailable"})
        self.store.record(binding, summary)

    def refresh_observations(self, principal=None):
        # This is a read projection only: no execute, reconcile GET, source refresh, or model POST.
        with self.store.db() as db:
            bindings = [dict(r) for r in db.execute("SELECT * FROM bindings")]
        for binding in bindings:
            if principal is None or binding["client"] == principal:
                self.capture(binding)

    def ledger(self):
        seen, accounts, rows = set(), [], []
        known, unknown = Decimal(0), 0
        clients = {}
        with self.store.db() as db:
            bindings = [dict(row) for row in db.execute("SELECT * FROM bindings")]
            client_ids = [row["id"] for row in db.execute("SELECT id FROM clients")]
        owners = {_bound_task_id(binding): binding for binding in bindings}
        by_client = {client: Decimal(0) for client in client_ids}
        unattributed = Decimal(0)
        for contract in self.service.contracts.values():
            runner = contract.runner
            for client in [runner.client, *([runner.local_backend] if runner.local_backend else [])]:
                clients[_fingerprint(str(client.root.resolve()))] = client
        for contract in self.service.contracts.values():
            budget = contract.runner.store
            identity = str(budget.path.resolve())
            if identity in seen:
                continue
            seen.add(identity)
            report = budget.report()
            account = digest(identity)[:12]
            known += Decimal(report["known_usd"])
            unknown += report["unknown_events"]
            fields = ("authorized_usd", "reserved_usd", "known_usd", "verified_usd", "total_usd",
                      "available_usd", "unknown_events", "dispatch_blocked", "provisional_hold_usd")
            accounts.append({"id": account, **{k: report.get(k) for k in fields}})
            for event in report["events"]:
                row = {"id": event["id"], "account": account,
                       **{k: event.get(k) for k in ("run", "kind", "cost", "verified", "receipt", "bound")}}
                row["request_kind"] = event["kind"]
                owner = owners.get(event.get("run"))
                row["client"] = owner["client"] if owner else None
                row["task_run"] = owner["run"] if owner else None
                if event.get("cost") is not None:
                    if owner:
                        by_client[owner["client"]] += Decimal(event["cost"])
                    else:
                        unattributed += Decimal(event["cost"])
                try:
                    kind, root_id, call_id = event["id"].split(":", 2)
                    client = clients[root_id]
                    directory = client._directory(call_id)
                    info = usage(client._load(directory))
                    row.update({k: info.get(k) for k in ("model", "provider", "cost_source", "elapsed_ms")})
                    intent = json.loads((directory / "intent.json").read_text())
                    row["request_kind"] = "classification" if urlsplit(intent.get("url", "")).path.endswith("/decisions") else "generation"
                    row["tokens"] = {k: v for k, v in info.get("usage", {}).items()
                        if k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens")
                        and type(v) is int and v >= 0}
                except Exception:
                    row["metadata_unavailable"] = True
                rows.append(row)
        reserved = sum((Decimal(account["reserved_usd"]) for account in accounts), Decimal(0))
        return {"known_usd": str(known), "total_usd": None if unknown or reserved else str(known),
                "unknown_events": unknown, "accounts": accounts, "rows": rows,
                "reserved_usd": str(reserved),
                "by_kind_known_usd": {kind: str(sum((Decimal(r["cost"]) for r in rows
                    if r["request_kind"] == kind and r["cost"] is not None), Decimal(0)))
                    for kind in sorted({r["request_kind"] for r in rows})},
                "basis": "simulated_core_budget" if self.demo else "core_budget_events",
                "by_client_known_usd": {client: str(value) for client, value in by_client.items()},
                "unattributed_known_usd": str(unattributed),
                "host_electricity_human_usd": None}

    def capabilities(self):
        from cloudyfan_jev.scores.categories import CATEGORY_SPECS
        pool = self.store.control()["routing_pool"]
        categories = {}
        for category in sorted(CATEGORY_SPECS):
            categories[category] = {}
            for mode in MODES:
                contract_id = self.registry.get((pool, category, mode))
                registered = contract_id in self.service.contracts
                categories[category][mode] = {
                    "contract_registered": registered,
                    "contract_id": contract_id if registered else None,
                    "reason": None if registered else "routing_configuration_unavailable"}
        return {"pool": pool, "categories": categories,
                "notice": "registration_not_execution_authorization"}

    def retention_preview(self, principal, run):
        """Operator-only projection; caller must enforce operator authentication."""
        binding = self.store.binding(principal, run)
        latest, references = self.store.version_retention_context(
            binding, self.retention_policy.task_access_days * DAY)
        try:
            core_preview = self.service.retention_preview(principal, run)
        except Exception as error:
            core_preview = {"eligible": False, "targets": [],
                            "blockers": [getattr(error, "code", "core_retention_preview_unavailable")]}
        contract = self.service.contracts.get(binding["contract"])
        if contract is None:
            accounting = {"unknown_events": None}
            core_preview = {**core_preview, "eligible": False,
                            "blockers": [*core_preview["blockers"], "frozen_runtime_contract_unavailable"]}
        else:
            accounting = contract.runner.store.report()
        return self.retention_policy.review(binding, now=self.store.clock(), version_last_used=latest,
            references=references, accounting=accounting, core_preview=core_preview)

    def snapshot(self, principal=None):
        self.refresh_observations(principal)
        view = self.store.dashboard(principal)
        view.update({"demo": self.demo, "modes": MODES, "now": self.store.clock(),
                     "available_modes": [m for m in MODES if self.mode_available(m)],
                     "correctness_basis": "public_validator_not_universal_or_independent_truth"})
        view["capabilities"] = self.capabilities()
        view["capability_matrix"] = [{"category": category, "modes": modes}
                                     for category, modes in view["capabilities"]["categories"].items()]
        view["retention_policy"] = self.retention_policy.descriptor()
        if principal is None:
            view["ledger"] = self.ledger()
        return view
