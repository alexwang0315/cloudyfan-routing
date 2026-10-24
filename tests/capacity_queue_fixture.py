"""Synthetic host fixtures for the queue/window injection seam; never qualification."""
from pathlib import Path
import hashlib
import json

from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor


class WindowProtocolFixture:
    """Exercise the public backend SPI, not real response-window semantics."""
    protocol = "capacity-windows-v1"
    version = "synthetic-window-protocol-v1"

    def __init__(self, clock, *, status="open"):
        self.clock, self.status, self.responses = clock, status, []

    def check(self, db, scope, physical_groups, now):
        return {"status": self.status, "not_before": now + 30 if self.status == "wait" else None,
            "evidence_refs": ["synthetic-saved-window"], "revision": 1,
            "reason_code": "capacity_window_" + self.status}

    def record_response(self, receipt_ref, scope):
        path = Path(receipt_ref["root"]) / "calls" / receipt_ref["call_id"] / "response.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt_ref["response_sha256"]
        response = json.loads(path.read_text())
        assert scope["observed_at"] == response["window_headers"]["observed_at"]
        self.responses.append({"receipt": receipt_ref, "scope": scope, "response": response})

    def snapshot(self):
        return {"configured": True, "protocol": self.protocol, "version": self.version, "status": self.status}


def attach_window_protocol(bundle, previous, backend):
    old = bundle.app
    controller = CapacityController(previous.store, credential_active=previous.credential_active,
        window_backend=backend)
    controller.register("synthetic-window-enabled-v2", configuration_version="synthetic-admission-config-v1",
        routes=previous.routes["synthetic-admission-v1"], max_seconds=300, windows_version=backend.version)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(previous.store, sources={}), capacity_controller=controller)
    return controller


def attach_queue(bundle, clock, *, limits=None, rpm=10, tpm=None, concurrency=1,
                 token_adapter=None, seconds=300, config=None, route_bindings=None, ollama_models=None,
                 window_rules=None):
    from cloudyfan_jev.capacity_queue import QueuePreparation
    from cloudyfan_jev.scheduler import QueueScheduler, QueueLimits
    from capacity_admission_fixture import attach_admission
    previous = attach_admission(bundle, clock, rpm=rpm, tpm=tpm, concurrency=concurrency,
        token_adapter=token_adapter, seconds=seconds, config=config)
    old = bundle.app
    preparation = QueuePreparation("synthetic-pure-queue-v1", contracts=old.gateway.service.contracts,
        tools={name: [] for name in old.gateway.service.contracts})
    previous.queue_backend = preparation
    if window_rules is not None:
        from cloudyfan_jev.capacity_windows import CapacityWindows
        previous.window_backend = CapacityWindows(previous.store, version='synthetic-queue-windows-v1', rules=window_rules)
    routes = previous.routes["synthetic-admission-v1"]
    if route_bindings:
        from dataclasses import replace
        routes = [replace(route, binding=route_bindings.get(str(route.transport.root.resolve()), route.binding)) for route in routes]
    previous.register("synthetic-queued-admission-v1", configuration_version="synthetic-admission-config-v1",
        routes=routes, max_seconds=seconds, queue_version=preparation.version,
        **({'windows_version': previous.window_backend.version} if window_rules is not None else {}))
    queue = QueueScheduler(old.gateway.service, bundle.root / "queue.sqlite", clock=clock,
        limits=limits or QueueLimits(8, 2, 4, 120), measurement_kind="synthetic", capacity_protocol=preparation)
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=old.capacity_observer, capacity_controller=previous, queue=queue, ollama_models=ollama_models)
    return queue


def build_queue_host(root, *, clock, origin, options=None, before_response=None):
    """Reopen the same real service/identity/queue in independent HTTP workers.

    Private synthetic credentials/configuration are persisted once, never rotated
    by a restart. All registered contracts/routes exist identically in each host.
    The caller owns actual loopback servers and the non-loopback socket guard.
    """
    from dataclasses import replace, asdict
    from datetime import datetime, timezone, timedelta
    from decimal import Decimal
    from types import SimpleNamespace
    import httpx
    from cloudyfan_jev.budget import BudgetStore, BudgetedCore, Authorization
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.engine import Core
    from cloudyfan_jev.identity import IdentityStore, IssuedCredential
    from cloudyfan_jev.scheduler import QueueLimits
    from cloudyfan_monitor.demo import build_demo, TOKENS, CATEGORY
    from cloudyfan_monitor.integration import ACCESS_POLICY, LocalIntegration
    from capacity_sources_fixture import limit
    root, options = Path(root), options or {}
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    original, upstream = build_demo(root, seed=False, origin=origin, clock=clock, retention_policy=ACCESS_POLICY)
    upstream.before_response = before_response
    upstream.fail_tiers = set(options.get('fail_tiers', []))
    upstream.unknown_cost = options.get('unknown_cost', False)
    if options.get('retry_after_seconds') is not None:
        def response_window(request):
            response = upstream(request)
            if len(upstream.calls) != 1:
                return response
            body = response.json()
            body.update(choices=[], error={'code': 429, 'message': 'Synthetic controlled queue rate window'})
            return httpx.Response(429, json=body, headers={'Retry-After': str(options['retry_after_seconds'])})
        for contract in original.gateway.service.contracts.values():
            contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(response_window), trust_env=False)
    identities = IdentityStore(root / 'identities.sqlite',
        pepper=b'synthetic-local-integration-pepper-000000', clock=clock)
    credentials_path = root / 'synthetic-host-credentials.json'
    if credentials_path.exists():
        credentials = {name: IssuedCredential(**{**value, 'scopes': tuple(value['scopes'])})
                       for name, value in json.loads(credentials_path.read_text()).items()}
    else:
        credentials = {}
        for principal in TOKENS:
            try:
                identities.principal(principal)
            except ValueError:
                identities.create_principal(principal)
            credentials[principal] = identities.issue(principal,
                scopes={'tasks:read', 'tasks:write', 'models:read'}, expires_at=clock() + 365 * 86400)
        credentials_path.write_text(json.dumps({name: asdict(value) for name, value in credentials.items()}))
        credentials_path.chmod(0o600)
    service = original.gateway.service
    if options.get('separate_budgets') or options.get('shared_budget'):
        replacements = {}
        for name, contract in service.contracts.items():
            template, old = contract.runner.core, contract.runner
            receipt_root = root / ('receipts-' + name)
            client = Client(httpx.Client(transport=httpx.MockTransport(response_window if options.get('retry_after_seconds') is not None else upstream), trust_env=False),
                            'synthetic-key-' + name, receipt_root)
            core = Core(client, template.profiles, template.decision, limits=template.limits,
                execution_policy=template.execution_policy, diagnostic_contract=template.diagnostic_contract)
            account = 'shared-queue' if options.get('shared_budget') else name
            budget = BudgetStore(root / ('budget-' + account + '.sqlite'))
            if budget.report()['authorization'] is None:
                budget.authorize(Authorization('synthetic-queue-' + account, Decimal('100'),
                    (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()))
            runner = BudgetedCore(core, budget, model_bounds=old.model_bounds,
                tool_bounds=old.tool_bounds, verify_bound=old.verify_bound)
            replacements[name] = replace(contract, runner=runner)
        service.contracts = replacements
    app = assemble_monitor(original.store, service, original.gateway.registry, identities,
        operator_token=original.admin_token, source=original.source, origin=origin, demo=True)
    bundle = LocalIntegration(app, upstream, identities, credentials, root, original.admin_token)
    configuration_path = root / 'synthetic-queue-configuration.json'
    if configuration_path.exists():
        configuration = json.loads(configuration_path.read_text())
    else:
        def group(name, ref):
            return {'id': name, 'scope': {'kind': 'account', 'ref': ref},
                'source': {'kind': 'controlled_import', 'version': 'synthetic-reviewed-v1'},
                'supplier_limits': [], 'local_limits': [limit('rpm', options.get('rpm', 10), clock()),
                    limit('tpm', None, clock(), status='not_applicable'),
                    limit('concurrency', options.get('supplier_concurrency', 1), clock())]}
        groups = [group('shared', 'synthetic-shared-account')]
        bindings = [{'id': 'synthetic-binding', 'provider': 'openrouter', 'account_ref': 'synthetic-shared-account',
            'credential_ref': 'synthetic-upstream-key', 'endpoint_ref': 'openrouter-chat', 'groups': ['shared']}]
        if options.get('disjoint'):
            groups.append(group('disjoint', 'synthetic-other-account'))
            bindings.append({**bindings[0], 'id': 'synthetic-disjoint-binding',
                'account_ref': 'synthetic-other-account', 'credential_ref': 'synthetic-other-key', 'groups': ['disjoint']})
        configuration = {'schema_version': 1, 'version': 'synthetic-admission-config-v1', 'bindings': bindings, 'groups': groups}
        configuration_path.write_text(json.dumps(configuration))
        configuration_path.chmod(0o600)
    route_bindings = {}
    if options.get('disjoint'):
        target = service.contracts['demo-open_source_first'].runner.client.root
        route_bindings[str(target.resolve())] = 'synthetic-disjoint-binding'
    queue = attach_queue(bundle, clock, config=configuration,
        route_bindings=route_bindings, seconds=options.get('seconds', 3600),
        limits=QueueLimits(**options.get('limits', {'capacity': 8, 'concurrency': 2, 'per_principal': 4, 'max_wait_seconds': 1800})),
        ollama_models={'queue-mrc': {'category': CATEGORY, 'contract_id': 'demo-cloudyfan'}},
        window_rules=([{'binding': 'synthetic-binding', 'group': 'shared', 'header': 'retry-after',
            'format': 'retry_after', 'constraint': 'requests'}] if options.get('retry_after_seconds') is not None else None))
    def close():
        clients = {id(contract.runner.client): contract.runner.client for contract in service.contracts.values()}
        for client in clients.values():
            client.http.close()
    protected = [queue.path, bundle.root / 'capacity.sqlite', service.root]
    for contract in service.contracts.values():
        protected.extend([contract.runner.store.path, contract.runner.client.root])
    return SimpleNamespace(app=bundle.app, queue=queue, credential=credentials['studio'].token, credentials=credentials,
        operator_token=bundle.operator_token, protected_paths=tuple(set(protected)), close=close,
        upstream=upstream, bundle=bundle)
