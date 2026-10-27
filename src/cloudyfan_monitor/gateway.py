"""A controlled Core TaskService adapter, not a second routing algorithm."""
from contextlib import closing, nullcontext
from decimal import Decimal
from functools import wraps
from types import MappingProxyType
import hashlib
import json
import sqlite3
import threading
from urllib.parse import urlsplit

from cloudyfan_jev.budget import _fingerprint
from cloudyfan_jev.client import usage
from cloudyfan_jev.http_api import ServiceError

from .admission import Admission
from .billing import Billing
from .retention_policy import DAY, EvidenceRetentionPolicy
from .store import MODES, MonitorError, digest, stats
from .observation_fields import identifier
from .tracing import routing_trace

RETAINED_SQL_VM_STEPS = 32000


def _bound_task_id(binding):
    # TaskService's stable identity scheme, only for lifecycle/public ledger metadata.
    # Raw results and dispatch still require the service's current authorization.
    identity = json.dumps([binding["client"], binding["run"]], sort_keys=True,
                          ensure_ascii=False, allow_nan=False).encode()
    return "http-" + hashlib.sha256(identity).hexdigest()[:48]


def _creation_computation(method):
    @wraps(method)
    def create(self, *args, **kwargs):
        recorder = self.service.creation_features
        with recorder.operation() if recorder is not None else nullcontext():
            return method(self, *args, **kwargs)
    return create


class Gateway:
    def __init__(self, store, service, registry, *, demo=False, queue=None):
        self.store, self.service, self.demo = store, service, demo
        self.queue = queue
        self.billing = Billing(store, demo=demo)
        self.registry = MappingProxyType(dict(registry))
        self.refresh_lock = threading.Lock()
        self.admission = Admission(store.path.with_suffix(".execution.lock"))
        self.retention_policy = EvidenceRetentionPolicy()
        if queue is not None:
            queue.transition_observer = self._queue_transition
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

    def _bind(self, principal, payload, expected_contract):
        recorder = self.service.creation_features
        return self.store.bind(principal, payload, self.registry, expected_contract=expected_contract,
            feature_policy=recorder.policy if recorder is not None else None,
            feature_contracts=recorder.registered if recorder is not None else None, with_creation=True)

    def _creation_owner(self, binding, created):
        recorder = self.service.creation_features
        return recorder.submitting(binding, created) if recorder is not None else nullcontext()

    @_creation_computation
    def create(self, principal, payload, *, expected_contract=None):
        self.service.authorize(principal)
        if self.queue is not None and (self.service.capacity_controller.queue_policy() is not None
                or isinstance(payload, dict) and isinstance(payload.get('run_id'), str)
                and self.queue.contains(principal, payload['run_id'])):
            prior = (self.store.existing_binding(principal, payload.get('run_id'))
                if isinstance(payload, dict) and isinstance(payload.get('run_id'), str) else None)
            if prior is not None and (expired := self._expiry(prior)):
                raise ServiceError(expired, 410)
            binding, created = self._bind(principal, payload, expected_contract)
            with self._creation_owner(binding, created):
                state = self.queue.submit(principal, {'run_id': binding['run'], 'contract_id': binding['contract'],
                                                    'prompt': payload['prompt']})
            self.capture(binding, retain_metadata=created)
            return self.queue_view(binding, state)
        prior = (self.store.existing_binding(principal, payload.get("run_id"))
                 if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) else None)
        if prior is not None:
            binding, _ = self._bind(principal, payload, expected_contract)
            return self.status(principal, binding["run"])
        try:
            with self.admission.acquire():
                prior = (self.store.existing_binding(principal, payload.get("run_id"))
                         if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) else None)
                binding, created = self._bind(principal, payload, expected_contract)
                if prior is not None:
                    return self.status(principal, binding["run"])
                self.store.record(binding, {"status": "bound"})
                try:
                    with self._creation_owner(binding, created):
                        self.service.create(principal, {"run_id": binding["run"], "contract_id": binding["contract"],
                                                        "prompt": payload["prompt"]})
                except Exception:
                    self.capture(binding, unavailable=True)
                    raise
                self.capture(binding, retain_metadata=True)
                return self._execute(binding)
        except MonitorError as error:
            if error.code != "service_busy":
                raise
            prior = (self.store.existing_binding(principal, payload.get("run_id"))
                     if isinstance(payload, dict) and isinstance(payload.get("run_id"), str) else None)
            if prior is None:
                raise
            binding, _ = self._bind(principal, payload, expected_contract)
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
            if self.queue is not None and self.queue.contains(principal, run):
                return self.queue_view(binding, self.queue.status(principal, run))
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
        return self._expiry_status(binding)[0]

    def _expiry_status(self, binding):
        previous = self.store.observation(binding["client"], binding["run"]) or {}
        if previous.get("access_expired"):
            return previous.get("error_code") or "task_expired", None
        if self.service.retention is not None:
            lifecycle = self.service.retention.lookup(_bound_task_id(binding))
            if lifecycle:
                if lifecycle["state"] == "retired":
                    return "task_retired", lifecycle['expires_at']
                if lifecycle["state"] == "expired" or self.store.clock() >= lifecycle["expires_at"]:
                    return "task_expired", lifecycle['expires_at']
                return None, lifecycle['expires_at']
        return None, None

    def execute(self, principal, run):
        self.service.authorize(principal)
        binding = self.store.binding(principal, run)
        if expired := self._expiry(binding):
            raise ServiceError(expired, 410)
        if self.queue is not None and self.queue.contains(principal, run):
            return self.queue_view(binding, self.queue.status(principal, run))
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
            self.capture(binding, retain_metadata=True)

    def queue_view(self, binding, status):
        if 'queue' not in status:
            return status
        with self.store.db() as db:
            row = db.execute('SELECT data FROM billing_task_policies WHERE client=? AND run_id=?',
                (binding['client'], binding['run'])).fetchone()
        tariff = json.loads(row['data']) if row is not None else None
        frozen = {**status['queue'].get('frozen', {}),
            'monitor': {'mode': binding['mode'], 'pool_version': binding['pool'],
                'category': binding['category'], 'contract_id': binding['contract'],
                'tariff': tariff, 'tariff_sha256': digest(tariff) if tariff is not None else None}}
        return {**status, 'queue': {**status['queue'], 'frozen': frozen}}

    def queue_snapshot(self):
        view = self.queue.snapshot()
        jobs = []
        for job in view['jobs']:
            try:
                binding = self.store.binding(job['client'], job['run_id'])
                job = self.queue_view(binding, job)
            except MonitorError:
                job = {**job, 'monitor_binding': 'unavailable'}
            jobs.append(job)
        return {**view, 'jobs': jobs}

    def _queue_transition(self, principal, run):
        binding = self.store.binding(principal, run)
        self.store.record_version_use(binding)
        self.capture(binding, retain_metadata=True)

    def _completed_summary(self, binding, *, expired=False, result=None):
        if result is None:
            if not expired:
                self.service.result(binding["client"], binding["run"], refresh=False)
            contract = self.service.contracts[binding["contract"]]
            result = contract.runner.core.read_completed(self.service.internal_id(binding["client"], binding["run"]))
        return {"completed_status": result["status"], "first_pass": result["first_public_pass"],
                "final_pass": result["final_public_pass"], "wall_seconds": result.get("wall_seconds"),
                "generation_attempts": len(result["attempts"]),
                "classifier_calls": (result.get("selection") or {}).get("classifier_calls", 0),
                "trace": routing_trace(result),
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

    def capture(self, binding, unavailable=False, *, persist=True, retain_metadata=False):
        principal, run = binding["client"], binding["run"]
        previous = self.store.observation(principal, run) or {}
        summary = {"status": "engineering_interrupted", "error_code": "monitor_evidence_unavailable"}
        original_status = None
        retained = None
        try:
            if unavailable:
                raise ValueError("creation interrupted")
            expired, expires_at = self._expiry_status(binding)
            if expired:
                raise ServiceError(expired, 410)
            observed = self.service.observe(principal, run)
            status = observed["state"]
            original_status = status['status']
            if self.queue is not None and self.queue.contains(principal, run):
                status = self.queue_view(binding, self.queue.status(principal, run))
            costs = status.get("costs", {})
            summary = {"status": status["status"], "known_api_usd": costs.get("known_api_usd"),
                       "complete_api_usd": costs.get("complete_api_usd"),
                       "unknown_events": costs.get("unknown_events"),
                       "error_code": (status.get("error") or {}).get("code"), "observation": None}
            if 'task_features' in status:
                summary['task_features'] = status['task_features']
            if 'queue' in status:
                summary.update({key: status.get(key) for key in ('queue', 'queue_state', 'reason',
                    'queue_seconds', 'end_to_end_seconds', 'cancellation_requested', 'provider_cancellation_confirmed')})
            if status["status"] in ("bound", "ready", "running", "queued"):
                summary["complete_api_usd"] = None
            if status["status"] in ("passed", "failed", "unverifiable"):
                if observed["completed_result"] is None:
                    raise ServiceError("result_evidence_unavailable", 503)
                summary.update(self._completed_summary(binding, result=observed["completed_result"]))
            if persist and retain_metadata:
                try:
                    retained = self._retained_metadata(binding, expires_at)
                except Exception:
                    pass  # Incomplete auxiliary capture never replaces prior safe evidence.
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
                    # Keep the previously verified public completion/trace.
                    # Expiry must not reopen raw results or prompts to fill it.
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
        if (not persist and original_status in {'passed', 'failed', 'unverifiable'}
                and summary['status'] in {'bound', 'ready', 'queued', 'accepting', 'claiming', 'running', 'interrupted_pending'}):
            # Original task history can be newer than the captured queue row.
            # Keep the queued/running fact as captured, with completion unknown
            # for this view, rather than inherit a newer cached success.
            summary.update(first_pass=None, final_pass=None, completed_status=None,
                trace=None, path=None, wall_seconds=None, observation='metadata_changed_during_read')
        if persist:
            self.store.record(binding, summary)
            if retained is not None:
                self.store.retain_metadata(binding, retained)
        # A detached queue view is older than work that may finish during the
        # remaining history/budget read. Project it into this response only;
        # never overwrite a newer current task record with that older summary.
        projected = {**previous, **summary,
            **{key: binding[key] for key in ('client', 'run', 'pool', 'mode', 'category', 'created')},
            'updated': self.store.clock()}
        if projected['status'] in {'passed', 'failed', 'unverifiable'}:
            projected['completed_status'] = projected['status']
        for key in ('generation_attempts', 'classifier_calls'):
            projected[key] = projected.get(key) or 0
        return projected

    def refresh_observations(self, principal=None, *, persist=True):
        # This is a read projection only: no execute, reconcile GET, source refresh, or model POST.
        with self.store.db() as db:
            bindings = [dict(r) for r in db.execute("SELECT * FROM bindings")]
        observed = []
        for binding in bindings:
            if principal is None or binding["client"] == principal:
                observed.append(self.capture(binding, persist=persist))
        return observed

    def _event_metadata(self, event, client, owner, *, pool=None):
        _, _, call_id = event['id'].split(':', 2)
        directory = client._directory(call_id)
        info = usage(client._load(directory))
        row = {key: info.get(key) for key in ('model', 'provider', 'cost_source', 'elapsed_ms', 'generation_id')}
        intent = json.loads((directory / 'intent.json').read_text())
        url = urlsplit(intent.get('url', ''))
        row['billing_origin'] = ('local' if info.get('local_call_id') else
                                 'openrouter' if url.hostname == 'openrouter.ai' else None)
        if owner:
            pool = self.store.pool(owner['pool']) if pool is None else pool
            candidates = pool['categories'][owner['category']]['pools']['open_source']
            row['open_source'] = any(candidate.get('profile', {}).get('model_version') == info.get('model')
                                     for candidate in candidates if candidate.get('profile'))
        row['request_kind'] = 'classification' if url.path.endswith('/decisions') else 'generation'
        row['tokens'] = {key: value for key, value in info.get('usage', {}).items()
            if key in ('input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens')
            and type(value) is int and value >= 0}
        return row

    def _retained_metadata(self, binding, expires_at):
        """Only called after a legal observe, by the existing persist=True writer."""
        if expires_at is not None and self.store.clock() >= expires_at:
            return None
        contract = self.service.contracts[binding['contract']]
        frozen = contract.descriptor()  # observe just verified the original registered descriptor.
        runner = contract.runner
        clients = {_fingerprint(str(client.root.resolve())): client for client in
                   [runner.client, *([runner.local_backend] if runner.local_backend else [])]}
        internal = _bound_task_id(binding)
        account = digest(str(runner.store.path.resolve()))[:12]
        events = []
        path = runner.store.path.absolute()
        if path != path.resolve() or path.is_symlink():
            raise ValueError('Unsafe original budget path')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            progress = 0
            def bounded_scan():
                nonlocal progress
                progress += 1000
                return progress >= RETAINED_SQL_VM_STEPS
            db.set_progress_handler(bounded_scan, 1000)
            original_events = db.execute('SELECT id,run FROM events WHERE run=? ORDER BY id LIMIT 65',
                                         (internal,)).fetchall()
        if len(original_events) > 64:
            raise ValueError('Retained event limit exceeded')
        pool = self.store.retained_pool(binding) if original_events else None
        for event in original_events:
            _, root_id, _ = event['id'].split(':', 2)
            events.append({'account': account, 'id': event['id'], 'run': internal,
                           'metadata': self._event_metadata(event, clients[root_id], binding, pool=pool)})
        if expires_at is not None and self.store.clock() >= expires_at:
            return None
        return {'schema_version': 'monitor-retained-public-metadata-v1', 'binding_sha256': digest(binding),
            'observed_at': self.store.clock(), 'history_binding': {
                'pool_version': identifier(binding['pool']), 'category': identifier(binding['category']),
                'mode': identifier(binding['mode']), 'contract_id': identifier(binding['contract']),
                'contract_version': identifier(frozen['version']),
                'routing_version': identifier(frozen.get('routing_version')), 'bound_at': binding['created']},
            'events': events}

    def ledger(self, *, include_retained_financial_metadata=False):
        seen, accounts, rows = set(), [], []
        expired = {}
        retained = {}
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
                if owner is not None and include_retained_financial_metadata is not True:
                    key = owner['client'], owner['run']
                    if key not in expired:
                        expired[key] = self._expiry(owner)
                    if expired[key]:
                        if key not in retained:
                            retained[key] = self.store.retained_metadata(owner)
                        saved = next((item['metadata'] for item in (retained[key] or {}).get('events', [])
                            if (item['account'], item['id'], item['run']) == (account, event['id'], event['run'])), None)
                        if saved is not None:
                            row.update(saved)
                        else:
                            row.update(metadata_unavailable=True, metadata_reason='original_raw_access_expired',
                                       request_kind='unknown')
                        rows.append(row)
                        continue
                try:
                    _, root_id, _ = event["id"].split(":", 2)
                    row.update(self._event_metadata(event, clients[root_id], owner))
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
        observed = self.refresh_observations(principal, persist=False)
        view = self.store.dashboard(principal)
        tasks = {(task['client'], task['run']): task for task in view['tasks']}
        tasks.update({(task['client'], task['run']): task for task in observed})
        tasks = sorted(tasks.values(), key=lambda task: task['created'], reverse=True)
        known = sum((Decimal(task['known_api_usd']) for task in tasks
            if task.get('known_api_usd') is not None), Decimal(0))
        view.update(tasks=tasks, summary={**stats(tasks), 'known_task_api_usd': str(known),
            'complete_task_api_usd': str(known) if all(task.get('complete_api_usd') is not None
                for task in tasks) else None},
            by_mode={mode: stats([task for task in tasks if task['mode'] == mode]) for mode in MODES})
        for client in view['clients']:
            client['stats'] = stats([task for task in tasks if task['client'] == client['id']])
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
