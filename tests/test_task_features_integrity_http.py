"""Public HTTP integrity when only auxiliary creation metadata cannot be saved."""
import json
import hashlib
import fcntl
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
from pathlib import Path
import sqlite3
from urllib.parse import unquote

import pytest

from task_features_fixture import task_features_http as task_features_http, POLICY_BUNDLE, StoreFeatureBindingPort
from test_local_http_integration import loopback_network as loopback_network
from test_local_http_integration import local_service as local_service


def _sqlite_full(tracer, monkeypatch):
    """Limit real writable connections to this new synthetic Monitor DB only."""
    target = tracer.controlled.app.store.path.resolve()
    original = sqlite3.connect
    evidence = {'kind': 'connection_level_synthetic_sqlite_full_not_physical_disk_full',
                'target': str(target), 'connections': [], 'engine_errors': []}
    (tracer.artifacts / 'monitor-before-synthetic-full.sqlite').write_bytes(target.read_bytes())
    with original(target) as db:
        for number in range(3):
            db.execute('INSERT INTO creation_feature_adoptions(client,run,adoption,adoption_sha) VALUES(?,?,?,?)',
                       ('unrelated-synthetic-filler', str(number), 'F' * 1200, '0' * 64))
        evidence['page_count'] = db.execute('PRAGMA page_count').fetchone()[0]
        evidence['page_size'] = db.execute('PRAGMA page_size').fetchone()[0]
    (tracer.artifacts / 'monitor-filled-before-http.sqlite').write_bytes(target.read_bytes())

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            try:
                return super().execute(sql, parameters)
            except sqlite3.Error as error:
                evidence['engine_errors'].append({'name': error.sqlite_errorname,
                    'code': error.sqlite_errorcode, 'original_transaction_active': self.in_transaction,
                    'statement_kind': sql.split()[0]})
                raise

    def limited(database, *args, **kwargs):
        name = str(database)
        candidate = unquote(name.split('?', 1)[0].removeprefix('file:'))
        selected = Path(candidate).absolute() == target and 'mode=ro' not in name
        if selected:
            kwargs['factory'] = ObservedConnection
        db = original(database, *args, **kwargs)
        if selected:
            limit = db.execute('PRAGMA max_page_count=' + str(evidence['page_count'])).fetchone()[0]
            evidence['connections'].append({'limit': limit, 'actual_connection': isinstance(db, sqlite3.Connection)})
        return db

    monkeypatch.setattr(sqlite3, 'connect', limited)
    return evidence, original


@pytest.mark.parametrize('enabled', [False, True], ids=['feature-off-control', 'feature-on-full'])
def test_lost_original_binding_transaction_cannot_accept_a_task(task_features_http, monkeypatch, enabled):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert capacity.status_code == 200
    active = operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version})
    assert active.status_code == 200
    if not enabled:
        tracer.controlled.host.service.creation_features = None
    fault, original_connect = _sqlite_full(tracer, monkeypatch)
    accepted = client.post('/v1/tasks', json={'run_id': 'full-tracer',
        'category': tracer.controlled.category, 'prompt': '中文1+1'})
    observed = client.get('/v1/tasks/full-tracer')
    effects = tracer.snapshot('after-auxiliary-full-http')
    with original_connect(tracer.controlled.app.store.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        bindings = db.execute('SELECT COUNT(*) FROM bindings WHERE client=? AND run=?', ('agent', 'full-tracer')).fetchone()[0]
    with original_connect(tracer.controlled.queue.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        jobs = db.execute('SELECT COUNT(*) FROM jobs WHERE principal=? AND run_id=?', ('agent', 'full-tracer')).fetchone()[0]
    evidence = {'enabled': enabled, 'post_status': accepted.status_code, 'get_status': observed.status_code,
        'post_task_status': accepted.json().get('status'), 'error': accepted.json().get('error'),
        'original_binding_count': bindings, 'original_queue_count': jobs, 'fault': fault}
    tracer.evidence['auxiliary_full'] = evidence
    (tracer.artifacts / 'auxiliary-full-public-behavior.json').write_text(json.dumps(evidence, indent=2) + '\n')
    assert effects['model_requests'] == effects['source_requests'] == effects['accounting_requests'] == []
    assert effects['health_sha256'] == tracer.evidence['before']['health_sha256']
    assert effects['budget_sqlite_sha256'] == tracer.evidence['before']['budget_sqlite_sha256']
    assert fault['connections'] and all(row['actual_connection'] for row in fault['connections'])
    if not enabled:
        assert accepted.status_code == observed.status_code == 202
        assert accepted.json()['status'] == observed.json()['status'] == 'queued'
        assert bindings == jobs == 1
        assert not fault['engine_errors']
    else:
        assert any(row['name'] == 'SQLITE_FULL' and not row['original_transaction_active']
                   for row in fault['engine_errors']), 'Intended real SQLite outer-transaction loss was not reached'
        assert accepted.status_code == 503, 'Lost original binding was incorrectly accepted'
        assert observed.status_code == 404
        assert bindings == jobs == 0


@pytest.mark.parametrize('phase', ['old-binding', 'old-accepting'])
def test_existing_acceptance_cannot_gain_a_new_creation_producer(task_features_http, phase):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    payload = {'run_id': 'acceptance-gap', 'category': tracer.controlled.category, 'prompt': '中文1+1'}
    service, gateway = tracer.controlled.host.service, tracer.controlled.app.gateway
    internal = service.internal_id('agent', payload['run_id'])
    if phase == 'old-binding':
        binding, created = tracer.controlled.app.store.bind('agent', payload, gateway.registry,
            feature_policy=POLICY_BUNDLE, feature_contracts=service.creation_features.registered, with_creation=True)
        assert created is True and binding['run'] == payload['run_id']
    else:
        directory = service.root / internal
        directory.mkdir(mode=0o700)
        with (directory / '.lock').open('a+b') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            pending = client.post('/v1/tasks', json=payload)
            assert pending.status_code == 202 and pending.json()['status'] == 'accepting'
            assert pending.json()['task_features']['status'] == 'unavailable'
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
    assert accepted.json()['task_features']['status'] == 'unavailable'
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    for unused in range(2):
        assert client.get('/v1/tasks/acceptance-gap').json()['task_features']['status'] == 'unavailable'
        assert client.post('/v1/tasks', json=payload).json()['task_features']['status'] == 'unavailable'
    assert not (service.root / '.task-features' / (internal + '.json')).exists()
    assert not (service.root / internal / 'creation-feature-anchor.json').exists()
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/acceptance-gap')
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    assert completed.json()['task_features']['status'] == 'unavailable'
    effects = tracer.snapshot('after-first-owner-gap-completion')
    assert len(effects['model_requests']) == 1
    assert effects['source_requests'] == effects['accounting_requests'] == []


def test_a_worker_cannot_dispatch_before_the_original_feature_seal_attempt(task_features_http, monkeypatch):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    internal = tracer.controlled.host.service.internal_id('agent', 'seal-before-worker')
    target = tracer.controlled.host.service.root / internal / 'creation-feature-anchor.json'
    entered, released = threading.Event(), threading.Event()
    original = os.fsync
    def durable_anchor(fd):
        original(fd)
        if target.exists() and os.fstat(fd).st_ino == target.stat().st_ino and not entered.is_set():
            entered.set()
            assert released.wait(3), 'Synthetic anchor durability barrier was not released'
    monkeypatch.setattr(os, 'fsync', durable_anchor)
    with ThreadPoolExecutor(1) as workers:
        submitted = workers.submit(client.post, '/v1/tasks', json={'run_id': 'seal-before-worker',
            'category': tracer.controlled.category, 'prompt': '中文1+1'})
        try:
            assert entered.wait(2), 'Actual anchor fsync did not reach the controlled filesystem boundary'
            assert tracer.controlled.queue.dispatch_next() is None
            assert not tracer.controlled.requests
            with sqlite3.connect(tracer.controlled.app.store.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                assert db.execute('SELECT record_sha FROM creation_feature_adoptions WHERE client=? AND run=?',
                                  ('agent', 'seal-before-worker')).fetchone()[0] is None
        finally:
            released.set()
        accepted = submitted.result(timeout=5)
    assert accepted.status_code == 202
    assert accepted.json()['status'] == 'queued' and accepted.json()['task_features']['status'] == 'available'
    with sqlite3.connect(tracer.controlled.app.store.path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        seal = db.execute('SELECT record_sha FROM creation_feature_adoptions WHERE client=? AND run=?',
                          ('agent', 'seal-before-worker')).fetchone()[0]
    assert isinstance(seal, str) and len(seal) == 64
    assert tracer.dispatch() is not None
    assert client.get('/v1/tasks/seal-before-worker').json()['status'] == 'passed'


@pytest.mark.parametrize('fault', ['missing-anchor', 'missing-record', 'torn-record', 'oversized-record',
    'symlink-record', 'hardlinked-record', 'self-resigned-record', 'duplicate-key', 'missing-seal', 'wrong-adoption'])
def test_torn_or_forged_creation_metadata_is_unavailable_on_both_public_views(task_features_http, fault):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    accepted = client.post('/v1/tasks', json={'run_id': 'integrity-tracer', 'category': tracer.controlled.category,
        'prompt': 'PRIVATE_FEATURE_INTEGRITY_CANARY 1+1'})
    assert accepted.status_code == 202
    features = accepted.json()['task_features']
    assert features['status'] == 'available'
    root = tracer.controlled.host.root
    record = root / 'tasks/.task-features' / (features['binding']['internal_id'] + '.json')
    anchor = root / 'tasks' / features['binding']['internal_id'] / 'creation-feature-anchor.json'
    (tracer.artifacts / 'original-record.json').write_bytes(record.read_bytes())
    (tracer.artifacts / 'original-anchor.json').write_bytes(anchor.read_bytes())
    (tracer.artifacts / 'original-monitor.sqlite').write_bytes(tracer.controlled.app.store.path.read_bytes())
    if fault == 'missing-anchor':
        anchor.rename(tracer.artifacts / 'preserved-missing-anchor.json')
    elif fault == 'missing-record':
        record.rename(tracer.artifacts / 'preserved-missing-record.json')
    elif fault == 'torn-record':
        record.write_bytes(b'{"schema_version":')
    elif fault == 'oversized-record':
        record.write_bytes(b'X' * 16385)
    elif fault == 'symlink-record':
        record.rename(tracer.artifacts / 'preserved-symlink-record.json')
        record.symlink_to(tracer.artifacts / 'preserved-symlink-record.json')
    elif fault == 'hardlinked-record':
        preserved = tracer.artifacts / 'preserved-hardlinked-record.json'
        record.rename(preserved)
        os.link(preserved, record)
    elif fault == 'self-resigned-record':
        changed = json.loads(record.read_bytes())
        changed['features'][0]['value'] = 1
        record.write_text(json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        changed_anchor = json.loads(anchor.read_bytes())
        changed_anchor['record_sha256'] = hashlib.sha256(record.read_bytes()).hexdigest()
        anchor.write_text(json.dumps(changed_anchor))
    elif fault == 'duplicate-key':
        body = record.read_bytes()
        record.write_bytes(b'{"status":"available",' + body[1:])
    else:
        with sqlite3.connect(tracer.controlled.app.store.path) as db:
            if fault == 'missing-seal':
                db.execute('UPDATE creation_feature_adoptions SET record_sha=NULL,request_sha=NULL,registered_sha=NULL '
                           'WHERE client=? AND run=?', ('agent', 'integrity-tracer'))
            else:
                db.execute('UPDATE creation_feature_adoptions SET adoption_sha=? WHERE client=? AND run=?',
                           ('0' * 64, 'agent', 'integrity-tracer'))
    observed = client.get('/v1/tasks/integrity-tracer')
    snapshot = operator.get('/monitor/snapshot')
    effects = tracer.snapshot('after-integrity-fault-public-http')
    assert observed.status_code == 202
    assert observed.json()['task_features']['status'] == 'unavailable'
    assert snapshot.status_code == 200
    task = next(task for task in snapshot.json()['tasks'] if task['client'] == 'agent' and task['run'] == 'integrity-tracer')
    assert task['operator_observation']['task_features']['status'] == 'unavailable'
    assert 'PRIVATE_FEATURE_INTEGRITY_CANARY' not in observed.text + snapshot.text
    assert effects['model_requests'] == effects['source_requests'] == effects['accounting_requests'] == []
    assert effects['health_sha256'] == tracer.evidence['before']['health_sha256']
    assert effects['budget_sqlite_sha256'] == tracer.evidence['before']['budget_sqlite_sha256']


def test_original_four_modes_and_canonical_inputs_keep_their_public_contract(local_service):
    """Original synchronous four-mode fixture; Health/Queue coverage is separate."""
    from cloudyfan_jev.scores.categories import CATEGORY_SPECS
    from cloudyfan_monitor.demo import CATEGORY
    host = local_service.bundle
    host.upstream.fail_tiers = set()
    service = host.app.gateway.service
    service.contracts = {name: replace(contract, tool_names=('allowed-tool-is-not-a-requirement',))
                         for name, contract in service.contracts.items()}
    service.creation_features = {'policy_bundle': {**POLICY_BUNDLE, 'feature_version': 'local-deterministic-v2'},
                                 'binding_port': StoreFeatureBindingPort(host.app.store)}
    operator, client = local_service.client('operator', timeout=5), local_service.client(timeout=5)
    observed = {'scope': 'Original registered synchronous four-mode Core; no new model adapter; Health/Queue tested in other real host cases',
                'modes': [], 'categories': [], 'input_boundaries': []}
    for mode, tier in (('cloudyfan', 'open_source'), ('fixed_high', 'high'), ('low_first', 'low'), ('open_source_first', 'open_source')):
        current = operator.get('/monitor/snapshot')
        assert current.status_code == 200
        assert operator.post('/monitor/mode', json={'revision': current.json()['control']['revision'], 'mode': mode}).status_code == 200
        response = client.post('/v1/tasks', json={'run_id': 'original-' + mode, 'category': CATEGORY,
                                                'prompt': 'Return JSON and call allowed-tool-is-not-a-requirement. 1+1'})
        assert response.status_code == 200 and response.json()['status'] == 'passed'
        features = response.json()['task_features']
        assert features['status'] == 'available'
        items = {item['id']: item for item in features['features']}
        assert items['original_mode']['value'] == mode
        assert items['tools_required']['kind'] == items['output_schema']['kind'] == 'unknown'
        assert items['tools_required']['value'] is items['output_schema']['value'] is None
        assert items['context_requirements']['kind'] == 'unknown'
        assert items['runtime_limits']['value']['max_tokens'] == 128
        assert 'allowed-tool-is-not-a-requirement' not in json.dumps(features)
        snapshot = operator.get('/monitor/snapshot')
        task = next(t for t in snapshot.json()['tasks'] if t['run'] == 'original-' + mode)
        assert task['mode'] == mode and task['path'][0]['tier'] == tier and task['classifier_calls'] == 0
        observed['modes'].append({'mode': mode, 'status': response.status_code, 'first_tier': tier})
    current = operator.get('/monitor/snapshot').json()
    assert operator.post('/monitor/mode', json={'revision': current['control']['revision'], 'mode': 'cloudyfan'}).status_code == 200
    for index, category in enumerate(CATEGORY_SPECS):
        response = client.post('/v1/tasks', json={'run_id': 'canonical-' + str(index), 'category': category, 'prompt': '1+1'})
        expected = 200 if category == CATEGORY else 409
        assert response.status_code == expected
        if expected == 200:
            assert response.json()['task_features']['binding']['category'] == category
        else:
            assert response.json()['error']['code'] == 'routing_configuration_unavailable'
            assert 'task_features' not in response.json()
        observed['categories'].append({'canonical': category, 'status': response.status_code,
                                       'registered': category == CATEGORY})
    assert len(observed['categories']) == 23
    boundary = client.post('/v1/tasks', json={'run_id': 'exact-32k', 'category': CATEGORY,
                                             'prompt': '中' * 10922 + 'ab'})
    assert boundary.status_code == 200
    items = {item['id']: item for item in boundary.json()['task_features']['features']}
    assert items['input_utf8_bytes']['value'] == 32768 and items['input_codepoints']['value'] == 10924
    for index, patch in enumerate(({'prompt': ''}, {'prompt': '中' * 10923}, {'messages': []}, {'documents': []},
                                   {'modality': 'image'}, {'tokenizer': 'unregistered'})):
        response = client.post('/v1/tasks', json={'run_id': 'unsupported-' + str(index), 'category': CATEGORY,
                                                'prompt': '1+1', **patch})
        assert response.status_code == 422 and 'task_features' not in response.json()
        observed['input_boundaries'].append({'case': index, 'status': response.status_code,
                                             'error': response.json()['error']['code']})
    assert len(host.upstream.calls) == 6
    destination = Path(os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR', str(host.root / 'feature-compatibility-evidence')))
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    (destination / 'mode-category-input-compatibility.json').write_text(json.dumps(observed, indent=2) + '\n')


@pytest.mark.parametrize('fault', ['auxiliary-sql-abort', 'metadata-directory-is-file', 'record-already-exists'])
def test_auxiliary_only_failures_keep_original_dispatch_and_do_not_repair_on_read(task_features_http, fault):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    service = tracer.controlled.host.service
    internal = service.internal_id('agent', 'auxiliary-only')
    root = service.root / '.task-features'
    if fault == 'auxiliary-sql-abort':
        with sqlite3.connect(tracer.controlled.app.store.path) as db:
            db.executescript("CREATE TRIGGER synthetic_auxiliary_abort BEFORE INSERT ON creation_feature_adoptions "
                "BEGIN SELECT RAISE(ABORT,'PRIVATE_AUXILIARY_SQL_CANARY'); END;")
    elif fault == 'metadata-directory-is-file':
        root.write_bytes(b'PRIVATE_AUXILIARY_DIRECTORY_CANARY')
    else:
        root.mkdir(mode=0o700)
        (root / (internal + '.json')).write_bytes(b'PRIVATE_AUXILIARY_EXISTING_RECORD_CANARY')
    payload = {'run_id': 'auxiliary-only', 'category': tracer.controlled.category, 'prompt': '中文1+1'}
    accepted = client.post('/v1/tasks', json=payload)
    assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
    assert accepted.json()['task_features']['status'] == 'unavailable'
    if fault == 'metadata-directory-is-file':
        root.rename(tracer.artifacts / 'preserved-auxiliary-directory-blocker')
    elif fault == 'record-already-exists':
        (root / (internal + '.json')).rename(tracer.artifacts / 'preserved-existing-record.bin')
    service.creation_features = {'policy_bundle': POLICY_BUNDLE, 'binding_port': StoreFeatureBindingPort(tracer.controlled.app.store)}
    for unused in range(2):
        response = client.get('/v1/tasks/auxiliary-only')
        assert response.status_code == 202 and response.json()['task_features']['status'] == 'unavailable'
        assert client.post('/v1/tasks', json=payload).json()['task_features']['status'] == 'unavailable'
        assert 'PRIVATE_AUXILIARY_' not in response.text
    assert tracer.dispatch() is not None
    completed = client.get('/v1/tasks/auxiliary-only')
    assert completed.status_code == 200 and completed.json()['status'] == 'passed'
    assert completed.json()['task_features']['status'] == 'unavailable'
    effects = tracer.snapshot('after-auxiliary-only-completion')
    assert len(effects['model_requests']) == 1 and effects['source_requests'] == effects['accounting_requests'] == []


def test_expired_observation_keeps_finances_and_explicit_operator_reconcile_works(local_service, monkeypatch):
    """Original billing-capable Mock fixture; financial POST keeps old authority."""
    from cloudyfan_monitor.demo import CATEGORY
    import io
    host = local_service.bundle
    host.upstream.fail_tiers = set()
    service = host.app.gateway.service
    service.creation_features = {'policy_bundle': POLICY_BUNDLE, 'binding_port': StoreFeatureBindingPort(host.app.store)}
    operator, client = local_service.client('operator', timeout=5), local_service.client(timeout=5)
    settings = {'revision': 0, 'enabled': True, 'multiplier': '1.2', 'client_multipliers': {'studio': '0.9'},
                'open_source_prices': {'example/open_source': {'input_per_million': '0.4', 'output_per_million': '0.8'}}}
    assert operator.post('/monitor/billing/settings', json=settings).status_code == 200
    assert operator.post('/monitor/billing/credits', json={'client': 'studio', 'amount_usd': '1',
        'idempotency_key': 'feature-original-credit', 'note': 'Synthetic original credit'}).status_code == 200
    current = operator.get('/monitor/snapshot')
    assert operator.post('/monitor/mode', json={'revision': current.json()['control']['revision'],
                                               'mode': 'open_source_first', 'client': 'studio'}).status_code == 200
    task = client.post('/v1/tasks', json={'run_id': 'retained-financial-post', 'category': CATEGORY,
                                        'prompt': 'PRIVATE_FINANCIAL_RAW_CANARY Return only 2.'})
    assert task.status_code == 200 and task.json()['status'] == 'passed'
    features = task.json()['task_features']
    before = operator.get('/monitor/snapshot').json()
    before_billing = operator.get('/monitor/billing').json()
    assert not [entry for entry in before_billing['entries'] if entry['kind'] == 'debit']
    target_paths = {str(path) for path in host.root.rglob('*.json') if path.is_file()
                    and b'PRIVATE_FINANCIAL_RAW_CANARY' in path.read_bytes()}
    assert target_paths
    opened, original_open = [], io.open
    def observed_open(file, mode='r', *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and os.fspath(file) in target_paths and 'r' in mode:
            opened.append(str(file))
        return original_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(io, 'open', observed_open)
    assert operator.get('/monitor/snapshot').status_code == 200 and opened
    opened.clear()
    local_service.clock.now = features['captured_at'] + 30 * 86400
    expired = client.get('/v1/tasks/retained-financial-post')
    observed = operator.get('/monitor/snapshot')
    after_billing = operator.get('/monitor/billing')
    snapshot_reads = list(opened)
    assert expired.status_code == 410 and observed.status_code == after_billing.status_code == 200
    assert after_billing.json()['entries'] == before_billing['entries']
    for key in ('known_usd', 'total_usd', 'unknown_events', 'accounts', 'reserved_usd', 'by_client_known_usd', 'unattributed_known_usd'):
        assert observed.json()['ledger'][key] == before['ledger'][key]
    fields = ('id', 'run', 'kind', 'cost', 'verified', 'receipt', 'bound')
    assert [{key: row.get(key) for key in fields} for row in observed.json()['ledger']['rows']] == [
        {key: row.get(key) for key in fields} for row in before['ledger']['rows']]
    old_task = next(row for row in before['tasks'] if row['run'] == 'retained-financial-post')
    new_task = next(row for row in observed.json()['tasks'] if row['run'] == 'retained-financial-post')
    for key in ('trace', 'path', 'first_pass', 'final_pass', 'classifier_calls', 'known_api_usd', 'complete_api_usd'):
        assert new_task[key] == old_task[key]
    assert new_task['operator_observation']['task_features'] == features
    assert client.post('/monitor/billing/reconcile', json={}).status_code == 401
    assert operator.post('/monitor/billing/reconcile', json={'include_retained_financial_metadata': True}).status_code == 422
    opened.clear()
    reconciled = operator.post('/monitor/billing/reconcile', json={})
    financial_reads = list(opened)
    assert reconciled.status_code == 200 and reconciled.json()['charged'] == 1
    repeated = operator.post('/monitor/billing/reconcile', json={})
    assert repeated.status_code == 200 and repeated.json()['charged'] == 0
    billing = operator.get('/monitor/billing').json()
    debits = [entry for entry in billing['entries'] if entry['kind'] == 'debit']
    assert len(debits) == 1 and debits[0]['amount_usd'] == '0.00000792'
    assert len(host.upstream.calls) == 1 and financial_reads
    output = Path(os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR', str(host.root / 'feature-financial-evidence')))
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    (output / 'retained-financial-public-behavior.json').write_text(json.dumps({
        'snapshot_raw_read_count': len(snapshot_reads), 'explicit_financial_post_raw_read_count': len(financial_reads),
        'known_budget_preserved': True, 'existing_entries_unchanged_by_get': True,
        'charged_once': reconciled.json()['charged'], 'repeated_charged': repeated.json()['charged'],
        'debit_amount_usd': debits[0]['amount_usd'], 'mock_model_calls': len(host.upstream.calls)}, indent=2) + '\n')
    assert snapshot_reads == [], 'General expired snapshot reopened retained original request text'
    assert all(row.get('metadata_unavailable') is True and row.get('metadata_reason') == 'original_raw_access_expired'
               for row in observed.json()['ledger']['rows'])


def test_auxiliary_seal_busy_is_one_nonwaiting_attempt_and_never_repaired(task_features_http, monkeypatch):
    tracer = task_features_http
    operator, client = tracer.client('operator'), tracer.client('agent')
    capacity = operator.get('/monitor/capacity')
    assert operator.post('/monitor/capacity/activate', json={
        'revision': capacity.json()['dispatch']['revision'], 'version': tracer.controlled.policy_version}).status_code == 200
    internal = tracer.controlled.host.service.internal_id('agent', 'seal-busy')
    anchor = tracer.controlled.host.service.root / internal / 'creation-feature-anchor.json'
    target = tracer.controlled.app.store.path.resolve()
    entered, released, busy = threading.Event(), threading.Event(), threading.Event()
    original_fsync, original_connect = os.fsync, sqlite3.connect
    attempts = []
    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            try:
                return super().execute(sql, parameters)
            except sqlite3.Error as error:
                if error.sqlite_errorname == 'SQLITE_BUSY':
                    attempts.append({'error': error.sqlite_errorname, 'timeout': 0})
                    busy.set()
                raise
    def connect(database, *args, **kwargs):
        name = str(database)
        candidate = unquote(name.split('?', 1)[0].removeprefix('file:'))
        if Path(candidate).absolute() == target and 'mode=rw' in name and kwargs.get('timeout') == 0:
            kwargs['factory'] = ObservedConnection
        return original_connect(database, *args, **kwargs)
    def hold_anchor(fd):
        original_fsync(fd)
        if anchor.exists() and os.fstat(fd).st_ino == anchor.stat().st_ino and not entered.is_set():
            entered.set()
            assert released.wait(3)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(os, 'fsync', hold_anchor)
    with ThreadPoolExecutor(1) as workers:
        submitted = workers.submit(client.post, '/v1/tasks', json={'run_id': 'seal-busy',
            'category': tracer.controlled.category, 'prompt': '中文1+1'})
        blocker = None
        try:
            assert entered.wait(2)
            blocker = original_connect(target, timeout=0)
            blocker.execute('BEGIN IMMEDIATE')
            released.set()
            assert busy.wait(2), 'Actual auxiliary seal did not use its nonwaiting SQLite connection'
        finally:
            released.set()
            if blocker is not None:
                blocker.rollback()
                blocker.close()
        accepted = submitted.result(timeout=5)
    assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
    assert accepted.json()['task_features']['status'] == 'unavailable'
    assert anchor.is_file()
    assert (tracer.controlled.host.service.root / '.task-features' / (internal + '.json')).is_file()
    for unused in range(2):
        assert client.get('/v1/tasks/seal-busy').json()['task_features']['status'] == 'unavailable'
        assert client.post('/v1/tasks', json={'run_id': 'seal-busy', 'category': tracer.controlled.category,
                                             'prompt': '中文1+1'}).json()['task_features']['status'] == 'unavailable'
    assert tracer.dispatch() is not None
    assert client.get('/v1/tasks/seal-busy').json()['status'] == 'passed'
    assert attempts == [{'error': 'SQLITE_BUSY', 'timeout': 0}]
    tracer.evidence['actual_auxiliary_seal_busy'] = {'attempts': attempts, 'read_repair': False,
        'scope': 'Actual synthetic Monitor writer lock after original binding commit; original dispatch remains legal'}
