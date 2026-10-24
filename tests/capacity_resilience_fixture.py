"""Owned synthetic fault copies; retain every untouched original at its old binding."""
import hashlib
import json
from pathlib import Path
import sqlite3

from cloudyfan_jev.capacity import CapacityStore
from cloudyfan_jev.capacity_dispatch import CapacityController
from cloudyfan_jev.capacity_windows import CapacityWindows
from cloudyfan_monitor.capacity import CapacityObserver
from cloudyfan_monitor.integration import assemble_monitor


def build_resilience_host(root, *, clock, origin, options=None, before_response=None):
    """Real reopenable service/queue; only upstream responses are simulated.

    The existing queue fixture persists credentials, configuration timestamps,
    receipt roots and actual BudgetStores. Fault copies keep these exact paths.
    No HTTP route exposes fixture controls or the private preservation witness.
    """
    from capacity_queue_fixture import build_queue_host
    host = build_queue_host(root, clock=clock, origin=origin, options=options,
        before_response=before_response)
    host.controller = host.bundle.app.capacity_controller
    host.windows = host.controller.window_backend
    host.protected_paths += (host.controller.store.integrity.root,)
    if host.queue.integrity is not None:
        host.protected_paths += (host.queue.integrity.root,)
    return host


def build_resilience_health_host(root, *, clock, origin, before_response=None):
    """Same persistent Backup/Health/Queue host for real desktop and restart QA.

    This fixture owns controlled signals and simulated upstreams only. All
    admission, identity, original fee/receipt, health and continuation decisions
    use the actual product stores and registered protocols.
    """
    from copy import deepcopy
    from dataclasses import asdict
    from datetime import datetime, timezone
    from decimal import Decimal
    from types import SimpleNamespace
    import threading
    from uuid import uuid4
    import httpx
    from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
    from cloudyfan_jev.capacity_dispatch import CapacityRoute
    from cloudyfan_jev.capacity_queue import QueuePreparation
    from cloudyfan_jev.demo import ArithmeticEnvironment
    from cloudyfan_jev.engine import Limits
    from cloudyfan_jev.http_api import RegisteredContract, TaskService
    from cloudyfan_jev.identity import IdentityStore, IssuedCredential
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.scheduler import QueueScheduler, QueueLimits
    from cloudyfan_jev.scores.backup_runtime import BackupCategoryCore
    from cloudyfan_jev.scores.health import HealthStore
    from cloudyfan_monitor.integration import ACCESS_POLICY, RunBoundCore
    from cloudyfan_monitor.pools import empty_view
    from cloudyfan_monitor.store import Store
    from candidate_health_fixture import CONTRACT, OPERATOR, HealthHost, health_values
    from capacity_sources_fixture import limit

    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    values, control_path = health_values(), root / 'synthetic-resilience-host.json'
    plan = values['plan']
    store = Store(root / 'monitor.sqlite', clock=clock)
    identities = IdentityStore(root / 'identity.sqlite', clock=clock,
        pepper=b'synthetic-resilience-private-pepper-00000')
    if control_path.exists():
        control = json.loads(control_path.read_text())
        credentials = {name: IssuedCredential(**{**value, 'scopes': tuple(value['scopes'])})
            for name, value in control['credentials'].items()}
    else:
        credentials = {}
        for name in ('studio', 'agent'):
            identities.create_principal(name)
            credentials[name] = identities.issue(name, scopes={'tasks:read', 'tasks:write', 'models:read'},
                expires_at=clock() + 400 * 86400)
        control = {'credentials': {name: asdict(value) for name, value in credentials.items()},
            'configured_at': clock()}
    now = lambda: datetime.fromtimestamp(clock(), timezone.utc)
    health = HealthStore(root / 'health', policy=values['module'].HEALTH_POLICY, clock=now)
    host = HealthHost(None, None, None, health, credentials, root, (), values, clock)
    if not control_path.exists():
        for tier, rows in plan['pools'].items():
            for position in range(len(rows)):
                health.publish([host.observation(tier, position)])
    flags = {'hold': False, 'scenario': 'success'}
    held, released = threading.Event(), threading.Event()
    requests, side_requests, transports, factory_calls = [], {'source': [], 'accounting': []}, [], []
    boot = uuid4().hex
    def upstream(request, lane):
        payload = json.loads(request.content) if request.content else {}
        identifier = boot + '-' + str(len(requests) + 1)
        record = {'method': request.method, 'path': request.url.path, 'model': payload.get('model'),
            'lane': lane, 'wire_sha256': hashlib.sha256(request.content).hexdigest(), 'id': identifier}
        requests.append(record)
        (root / ('private-wire-' + identifier + '.bin')).write_bytes(request.content)
        record_path = root / ('private-request-' + identifier + '.json')
        record_path.write_text(json.dumps(record, sort_keys=True))
        record_path.chmod(0o600)
        host.calls.append({**record, 'payload': payload})
        if before_response is not None:
            before_response(request)
        if request.method != 'POST':
            side_requests['accounting'].append(record)
            return httpx.Response(503, json={'error': 'PRIVATE_RESILIENCE_ACCOUNTING_UNAVAILABLE'})
        scenario = flags['scenario'] if lane == 'primary' else 'success'
        if lane == 'primary':
            flags['scenario'] = 'success'
        if lane == 'primary' and flags['hold']:
            held.set()
            if not released.wait(600):
                raise httpx.ReadTimeout('Synthetic resilience completion gate timed out')
        if scenario in {'window', 'unknown', 'unknown_fee', 'availability'}:
            status = 503 if scenario == 'availability' else 429
            response = values['module'].response(payload, identifier, status=status,
                cost=None if scenario == 'unknown_fee' else '.01')
            body = response.json()
            body['error']['message'] = 'PRIVATE_RESILIENCE_PROVIDER_CANARY'
            return httpx.Response(status, json=body, headers={
                'Retry-After': 'invalid-time' if scenario == 'unknown' else '3600',
                'X-Private-Canary': 'PRIVATE_RESILIENCE_HEADER_CANARY'})
        return values['module'].response(payload, identifier, answer='bad' if scenario == 'quality' else '2')

    contracts, budgets, clients = {}, {}, {}
    for lane, name in (('primary', 'health-backup-v1'), ('disjoint', 'health-disjoint-v1')):
        http = httpx.Client(transport=httpx.MockTransport(lambda request, current=lane: upstream(request, current)),
            trust_env=False)
        client = Client(http, 'synthetic-resilience-key-' + lane, root / ('receipts-' + lane))
        transports.append(http)
        clients[lane] = client
        def factory(run_id, controlled, local):
            factory_calls.append({'run_id': run_id, 'receipt_root': str(controlled.root)})
            return BackupCategoryCore(controlled, plan, values['decision'], run_id=run_id, health_store=health,
                clock=now, limits=Limits(max_turns=1), local_backend=local, execution_policy=values['privacy'],
                diagnostic_contract=values['diagnostics'])
        template = factory('resilience-template', client, None)
        budget = BudgetStore(root / ('budget-' + lane + '.sqlite'), clock=now)
        if budget.report()['authorization'] is None:
            budget.authorize(Authorization('synthetic-resilience-' + lane, Decimal('10000'), '2099-01-01T00:00:00Z'))
        bounds = {row['entry']['profile']['model_version']: EnforcedBound('.5', 'MockTransport only')
            for rows in plan['pools'].values() for row in rows}
        bounds[values['decision']['version']] = EnforcedBound('.5', 'MockTransport only')
        runner = BudgetedCore(RunBoundCore(template, factory), budget, model_bounds=bounds,
            tool_bounds={}, verify_bound=lambda *args: True)
        contracts[name] = RegisteredContract(CONTRACT, runner, lambda workspace: ArithmeticEnvironment(),
            mode='cloudyfan' if lane == 'primary' else 'open_source_first')
        budgets[lane] = budget
    service = TaskService(root / 'tasks', contracts=contracts, allowed_principals={'studio', 'agent'},
        principal_active=identities.is_active, retention_policy=ACCESS_POLICY, clock=clock)
    view = empty_view('synthetic_resilience_no_real_qualification', values['now'])
    for tier, rows in plan['pools'].items():
        for position, row in enumerate(rows):
            view['categories'][host.category]['pools'][tier][position] = {
                'position': position, 'status': 'mock_only', 'profile': deepcopy(row['entry']['profile']),
                'config_id': row['entry']['config_id']}
    active = store.save_pool(view)
    proposal = deepcopy(view)
    proposal['source_kind'] = 'synthetic_resilience_unregistered_proposal'
    inactive = store.save_pool(proposal)
    if not control_path.exists():
        store.change(revision=store.control()['revision'], routing=active, catalog=active)
        control_path.write_text(json.dumps(control, sort_keys=True))
        control_path.chmod(0o600)
    registry = {(active, host.category, mode): name for mode, name in (
        ('cloudyfan', 'health-backup-v1'), ('open_source_first', 'health-disjoint-v1'))}
    capacity = CapacityStore(root / 'capacity.sqlite', clock=clock)
    bindings, groups, rules = [], [], []
    for index in range(5):
        name = 'provider-' + str(index)
        scope = {'kind': 'account', 'ref': 'synthetic-resilience-account-' + str(index)}
        bindings.append({'id': name, 'provider': 'openrouter', 'account_ref': scope['ref'],
            'credential_ref': 'synthetic-resilience-provider-key-' + str(index),
            'endpoint_ref': 'synthetic-resilience-endpoint-' + str(index), 'groups': [name]})
        groups.append({'id': name, 'scope': scope, 'source': {'kind': 'controlled_import',
            'version': 'synthetic-resilience-source-v1'}, 'supplier_limits': [], 'local_limits': [
                limit('rpm', 1000, control['configured_at']),
                limit('tpm', None, control['configured_at'], status='not_applicable'),
                limit('concurrency', 2, control['configured_at'])]})
        rules.append({'binding': name, 'group': name, 'header': 'retry-after',
            'format': 'retry_after', 'constraint': 'requests'})
    configuration = {'schema_version': 1, 'version': 'synthetic-resilience-config-v1',
        'bindings': bindings, 'groups': groups}
    capacity.configure(configuration)
    def credential_active(principal, credential, scope, observed):
        return identities.is_active(principal) and any(row['id'] == credential and row['revoked_at'] is None
            and row['created_at'] <= observed < row['expires_at'] and scope in row['scopes']
            for row in identities.credentials(principal))
    windows = CapacityWindows(capacity, version='synthetic-resilience-windows-v1', rules=rules)
    controller = CapacityController(capacity, credential_active=credential_active, window_backend=windows)
    preparation = QueuePreparation('synthetic-resilience-preparation-v1', contracts=contracts,
        tools={name: [] for name in contracts})
    controller.queue_backend = preparation
    routes = []
    profiles = [row['entry']['profile'] for rows in plan['pools'].values() for row in rows] + [values['decision']]
    for lane, client in clients.items():
        for profile in profiles:
            classifier = profile is values['decision']
            binding = 'provider-4' if lane == 'disjoint' else 'provider-3' if classifier else profile['provider']
            routes.append(CapacityRoute(client, 'https://openrouter.ai/api/alpha/decisions' if classifier
                else 'https://openrouter.ai/api/v1/chat/completions', profile.get('model_version', profile.get('version')),
                binding, provider_name=profile['provider_name'],
                accepted_models=tuple(profile.get('accepted_models', ()))))
    for version, queued in (('synthetic-resilience-queued-v1', True), ('synthetic-resilience-queued-v2', True),
                            ('synthetic-resilience-immediate-v1', False)):
        controller.register(version, configuration_version=configuration['version'], routes=routes, max_seconds=3600,
            windows_version=windows.version, **({'queue_version': preparation.version} if queued else {}))
    observer = CapacityObserver(capacity, sources={})
    host.app = assemble_monitor(store, service, registry, identities, operator_token=OPERATOR, origin=origin,
        demo=True, capacity_observer=observer, capacity_controller=controller)
    queue = QueueScheduler(service, root / 'queue.sqlite', clock=clock, limits=QueueLimits(64, 4, 32, 1800),
        measurement_kind='synthetic', capacity_protocol=preparation)
    host.app = assemble_monitor(store, service, registry, identities, operator_token=OPERATOR, origin=origin,
        demo=True, capacity_observer=observer, capacity_controller=controller, queue=queue,
        ollama_models={'resilience-mrc': {'category': host.category, 'contract_id': 'health-backup-v1'}})
    from cloudyfan_monitor.openrouter_accounting import OpenRouterAccounting
    from cloudyfan_monitor.pools import OpenRouterPoolSource
    from test_sources import fixtures
    for kind in side_requests:
        def unexpected(request, source=kind):
            side_requests[source].append({'method': request.method, 'path': request.url.path})
            record_path = root / ('private-' + source + '-request-' + uuid4().hex + '.json')
            record_path.write_text(json.dumps(side_requests[source][-1], sort_keys=True))
            record_path.chmod(0o600)
            if before_response is not None:
                before_response(request)
            return httpx.Response(503, json={'error': 'PRIVATE_RESILIENCE_OBSERVATION_CANARY'})
        transports.append(httpx.Client(transport=httpx.MockTransport(unexpected), trust_env=False))
    host.app.source = OpenRouterPoolSource(transports[-2], fixtures()[2])
    host.app.gateway.billing.source = OpenRouterAccounting(transports[-1], api_key=uuid4().hex)
    host.service, host.budget, host.pools = service, budgets['primary'], (active, inactive)
    def close():
        released.set()
        for transport in transports:
            transport.close()
    return SimpleNamespace(host=host, app=host.app, controller=controller, windows=windows, queue=queue,
        flags=flags, held=held, released=released, requests=requests, side_requests=side_requests,
        factory_calls=factory_calls,
        clock=clock, budgets=budgets, credentials=credentials, operator_token=OPERATOR, category=host.category,
        policy_version='synthetic-resilience-queued-v1', close=close)


def copied_database_fault(path, archive, *, omit=(), updates=()):
    """Move the closed original aside, then build a NEW DB at the same host path.

    No original or copy is deleted. Missing tables are omitted while copying;
    SQL UPDATE faults apply only to newly copied rows, never the retained source.
    """
    path, archive = Path(path), Path(archive)
    archive.mkdir(parents=True, mode=0o700)
    retained = archive / path.name
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    path.rename(retained)
    with sqlite3.connect(retained.resolve().as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(path) as candidate:
            objects = original.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='table' DESC,name").fetchall()
            for kind, name, table, sql in objects:
                if table in omit:
                    continue
                candidate.execute(sql)
                if kind == 'table':
                    columns = original.execute('PRAGMA table_info("' + name + '")').fetchall()
                    candidate.executemany('INSERT INTO "' + name + '" VALUES(' + ','.join('?' for _ in columns) + ')',
                        original.execute('SELECT * FROM "' + name + '"'))
            for statement, arguments in updates:
                assert statement.startswith('UPDATE '), 'Only explicit new-copy UPDATE faults allowed'
                candidate.execute(statement, arguments)
    path.chmod(0o600)
    receipt = {'original': str(retained), 'original_sha256': fingerprint,
        'candidate': str(path), 'omitted_tables': list(omit), 'update_count': len(updates),
        'original_unchanged': hashlib.sha256(retained.read_bytes()).hexdigest() == fingerprint}
    (archive / 'copy-receipt.json').write_text(json.dumps(receipt, indent=2))
    assert receipt['original_unchanged']
    return receipt


def reopen_windows(bundle, controller, clock):
    store = CapacityStore(controller.store.path, clock=clock)
    windows = CapacityWindows(store, version='synthetic-windows-v1', rules=[
        {'binding': 'synthetic-binding', 'group': 'shared', 'header': 'retry-after',
         'format': 'retry_after', 'constraint': 'requests'}])
    current = CapacityController(store, credential_active=controller.credential_active, window_backend=windows)
    current.register('synthetic-window-admission-v1', configuration_version='synthetic-admission-config-v1',
        routes=controller.routes['synthetic-window-admission-v1'], max_seconds=300, windows_version=windows.version)
    old = bundle.app
    bundle.app = assemble_monitor(old.store, old.gateway.service, old.gateway.registry, old.identity_store,
        operator_token=bundle.operator_token, source=old.source, origin=old.origin, demo=True,
        capacity_observer=CapacityObserver(store, sources={}), capacity_controller=current)
    return current, windows
