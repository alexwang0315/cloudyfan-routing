"""Actual task acceptance must not depend on an older operator observation."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import sqlite3
import threading

import pytest

from capacity_resilience_fixture import build_resilience_health_host, copied_database_fault
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network


@pytest.fixture
def acceptance_host(local_service, tmp_path):
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    controlled = build_resilience_health_host(tmp_path / 'host', clock=local_service.clock,
        origin=local_service.origin)
    local_service.bundle = controlled.host
    local_service.server.set_app(controlled.app)
    operator = local_service.client('operator', timeout=10)
    client = local_service.client(timeout=10)
    assert operator.post('/monitor/capacity/activate', json={
        'revision': 0, 'version': controlled.policy_version}).status_code == 200
    try:
        yield controlled, operator, client
    finally:
        controlled.close()


def original_facts(controlled):
    root = controlled.host.root
    return {'budget': {lane: store.report() for lane, store in controlled.budgets.items()},
        'health_sha256': hashlib.sha256(controlled.host.health.path.read_bytes()).hexdigest(),
        'raw_sha256': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for pattern in ('private-request-*.json', 'private-wire-*.bin')
            for path in root.glob(pattern)},
        'model_requests': list(controlled.requests),
        'side_requests': {name: list(rows) for name, rows in controlled.side_requests.items()}}


def original_acceptance(controlled, run_id):
    root = controlled.host.root
    with sqlite3.connect((root / 'queue.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT id,state,reason,created,deadline FROM jobs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        return None
    with sqlite3.connect((root / 'tasks/.retention/lifecycle.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        lifecycle = db.execute('SELECT queued FROM tasks WHERE id=?', (row[0],)).fetchone()
    return {'state': row[1], 'reason': row[2], 'created': row[3], 'deadline': row[4],
        'retention_queued': lifecycle[0] if lifecycle else None}


def test_old_operator_snapshot_does_not_interrupt_new_task_acceptance(
        acceptance_host, local_service, monkeypatch, tmp_path):
    controlled, operator, client = acceptance_host
    service = controlled.host.service
    target = 'accept-during-old-snapshot'
    internal = service.internal_id('studio', target)
    before = original_facts(controlled)
    captured, created, budget_held, reader_finished = (threading.Event() for _ in range(4))
    reader_advanced = threading.Event()
    enumerate_new_binding, finish_acceptance, finish_observation = (threading.Event() for _ in range(3))
    local = threading.local()
    observed, submitted, events = {}, {}, []
    original_observation = controlled.app._observation
    original_create = service.create
    original_guard = service.retention.guard

    @contextmanager
    def after_original_capture():
        with original_observation():
            if getattr(local, 'request', None) == 'GET':
                assert not controlled.queue.contains('studio', target)
                events.append('real_old_capture_finished')
                captured.set()
                assert enumerate_new_binding.wait(10)
            yield

    def after_original_create(principal, payload):
        result = original_create(principal, payload)
        if getattr(local, 'request', None) == 'POST' and payload.get('run_id') == target:
            events.append('real_task_created')
            created.set()
            assert finish_acceptance.wait(10)
        return result

    @contextmanager
    def original_guard_schedule(task_id):
        with original_guard(task_id):
            previous = getattr(local, 'original_target_guard', False)
            if getattr(local, 'request', None) == 'GET' and task_id == internal:
                local.original_target_guard = True
            try:
                yield
            finally:
                local.original_target_guard = previous

    monkeypatch.setattr(controlled.app, '_observation', after_original_capture)
    monkeypatch.setattr(service, 'create', after_original_create)
    monkeypatch.setattr(service.retention, 'guard', original_guard_schedule)
    for store in controlled.budgets.values():
        original_report = store.report

        def after_original_budget(_original=original_report):
            result = _original()
            if (getattr(local, 'request', None) == 'GET'
                    and getattr(local, 'original_target_guard', False) and not budget_held.is_set()):
                events.append('real_budget_read_finished_inside_original_guard')
                budget_held.set()
                reader_advanced.set()
                assert finish_observation.wait(10)
            return result

        monkeypatch.setattr(store, 'report', after_original_budget)

    def unchanged_http_app(environ, start_response):
        local.request = environ['REQUEST_METHOD']
        try:
            return controlled.app(environ, start_response)
        finally:
            local.request = None

    local_service.server.set_app(unchanged_http_app)

    def read_once():
        try:
            response = operator.get('/monitor/snapshot')
            observed.update(status=response.status_code, body=response.json())
        except BaseException as error:
            observed['error_type'] = type(error).__name__
        finally:
            reader_finished.set()
            reader_advanced.set()

    def submit_once():
        try:
            response = client.post('/v1/tasks', json={'run_id': target, 'category': controlled.category,
                'prompt': 'Return only 1+1.'})
            submitted.update(status=response.status_code, body=response.json())
        except BaseException as error:
            submitted['error_type'] = type(error).__name__

    reader = threading.Thread(target=read_once, daemon=True)
    writer = threading.Thread(target=submit_once, daemon=True)
    reader.start()
    try:
        assert captured.wait(5)
        writer.start()
        assert created.wait(5)
        enumerate_new_binding.set()
        assert reader_advanced.wait(5)
        # A corrected read can finish without acquiring the old guard. Requiring
        # that guard would turn a removed interference path into a test timeout.
        assert budget_held.is_set() or reader_finished.is_set()
        finish_acceptance.set()
        writer.join(10)
        assert not writer.is_alive()
    finally:
        enumerate_new_binding.set()
        finish_acceptance.set()
        finish_observation.set()
        reader.join(10)
        if writer.ident is not None:
            writer.join(10)
        (tmp_path / 'acceptance-observation-receipt.json').write_text(json.dumps({
            'public_submit': submitted, 'public_snapshot': observed,
            'original_acceptance': original_acceptance(controlled, target),
            'original_facts_before': before, 'original_facts_after': original_facts(controlled),
            'scheduling_events': events, 'original_guard_budget_branch': budget_held.is_set(),
            'actual_reader_finished': reader_finished.is_set(),
            'reader_exited': not reader.is_alive(), 'writer_exited': not writer.is_alive(),
            'actual_http': local_service.http_records}, indent=2))
    assert not reader.is_alive() and observed.get('status') == 200
    assert submitted.get('status') == 202, submitted
    assert submitted['body']['status'] == 'queued'
    assert submitted['body']['queue']['deadline'] == 1792542600.0
    assert original_acceptance(controlled, target) == {
        'state': 'queued', 'reason': 'queue_waiting_worker', 'created': 1792540800.0,
        'deadline': 1792542600.0, 'retention_queued': 1}
    task = next(row for row in observed['body']['tasks'] if row['run'] == target)
    assert task['status'] in {'bound', 'ready', 'queued', 'accepting', 'engineering_interrupted'}
    assert all(task.get(key) is None for key in ('completed_status', 'first_pass', 'final_pass'))
    assert original_facts(controlled) == before
    assert controlled.requests == [] and controlled.side_requests == {'source': [], 'accounting': []}


def test_new_binding_before_original_lifecycle_is_unknown_without_creating_access_metadata(
        acceptance_host, monkeypatch, tmp_path):
    controlled, operator, client = acceptance_host
    target = 'binding-before-original-job'
    before = original_facts(controlled)
    bound, continue_acceptance = threading.Event(), threading.Event()
    original_bind = controlled.host.app.gateway.store.bind
    submitted = {}

    def after_original_binding(principal, payload, registry, **kwargs):
        result = original_bind(principal, payload, registry, **kwargs)
        if payload.get('run_id') == target:
            bound.set()
            assert continue_acceptance.wait(10)
        return result

    monkeypatch.setattr(controlled.host.app.gateway.store, 'bind', after_original_binding)

    def submit_once():
        try:
            response = client.post('/v1/tasks', json={'run_id': target, 'category': controlled.category,
                'prompt': 'Return only 1+1.'})
            submitted.update(status=response.status_code, body=response.json())
        except BaseException as error:
            submitted['error_type'] = type(error).__name__

    writer = threading.Thread(target=submit_once, daemon=True)
    writer.start()
    snapshot, retained_before, retained_after = None, None, None
    try:
        assert bound.wait(5)
        retention = controlled.host.service.retention.path
        retained_before = hashlib.sha256(retention.read_bytes()).hexdigest()
        response = operator.get('/monitor/snapshot')
        assert response.status_code == 200
        snapshot = response.json()
        retained_after = hashlib.sha256(retention.read_bytes()).hexdigest()
        assert original_acceptance(controlled, target) is None
        assert retained_after == retained_before, 'An observation initialized original access state'
        task = next(row for row in snapshot['tasks'] if row['run'] == target)
        assert task['status'] == 'engineering_interrupted'
        assert task['observation'] == 'evidence_unavailable'
        assert task['error_code'] == 'retention_metadata_missing'
        assert all(task.get(key) is None for key in ('completed_status', 'first_pass', 'final_pass'))
    finally:
        continue_acceptance.set()
        writer.join(10)
        (tmp_path / 'early-binding-receipt.json').write_text(json.dumps({
            'public_snapshot': snapshot, 'public_submit': submitted,
            'retention_hash_before_read': retained_before, 'retention_hash_after_read': retained_after,
            'original_acceptance': original_acceptance(controlled, target),
            'original_facts_before': before, 'original_facts_after': original_facts(controlled),
            'writer_exited': not writer.is_alive()}, indent=2))
    assert not writer.is_alive() and submitted.get('status') == 202
    assert submitted['body']['status'] == 'queued'
    assert submitted['body']['queue']['deadline'] == 1792542600.0
    assert original_acceptance(controlled, target)['retention_queued'] == 1
    assert original_facts(controlled) == before


@pytest.mark.parametrize('condition, expected_code', [
    ('active', None), ('expired', 'task_expired'), ('nonfinite', 'retention_clock_invalid'),
    ('rollback', 'retention_clock_rollback'), ('policy_changed', 'retention_policy_changed'),
    ('missing_lifecycle', 'retention_metadata_missing'), ('inactive', None),
])
def test_older_snapshot_still_checks_original_access_before_observing_new_binding(
        acceptance_host, monkeypatch, tmp_path, condition, expected_code):
    controlled, operator, client = acceptance_host
    service = controlled.host.service
    target = 'fresh-access-' + condition.replace('_', '-')
    captured, release = threading.Event(), threading.Event()
    original_admin = controlled.app._admin
    observed = {}

    def after_original_capture(method, path, environ):
        if method == 'GET' and path == '/monitor/snapshot':
            captured.set()
            assert release.wait(10)
        return original_admin(method, path, environ)

    monkeypatch.setattr(controlled.app, '_admin', after_original_capture)

    def read_once():
        try:
            response = operator.get('/monitor/snapshot')
            observed.update(status=response.status_code, body=response.json())
        except BaseException as error:
            observed['error_type'] = type(error).__name__

    reader = threading.Thread(target=read_once, daemon=True)
    reader.start()
    fault = None
    try:
        assert captured.wait(5)
        accepted = client.post('/v1/tasks', json={'run_id': target, 'category': controlled.category,
            'prompt': 'Return only 1+1.'})
        assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
        if condition == 'expired':
            monkeypatch.setattr(service.retention, 'clock', lambda: 1795132801.0)
        elif condition == 'nonfinite':
            monkeypatch.setattr(service.retention, 'clock', lambda: float('nan'))
        elif condition == 'rollback':
            monkeypatch.setattr(service.retention, 'clock', lambda: 1792540799.0)
        elif condition == 'policy_changed':
            from cloudyfan_jev.retention import RetentionPolicy
            monkeypatch.setattr(service, 'retention_policy', RetentionPolicy('changed-access-contract', 30 * 86400))
        elif condition == 'missing_lifecycle':
            # Retain the complete original DB; the new fault copy has the same
            # schema and clock, but no original lifecycle rows. Never repair it.
            with sqlite3.connect(service.retention.path.as_uri() + '?mode=ro', uri=True) as db:
                schema = db.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
            fault = copied_database_fault(service.retention.path, tmp_path / 'retained-original-lifecycle',
                omit=('tasks',))
            with sqlite3.connect(service.retention.path) as db:
                db.execute(schema)
        elif condition == 'inactive':
            controlled.app.identity_store.set_active('studio', False)
        before = original_facts(controlled)
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive() and observed.get('status') == 200, observed
    task = next(row for row in observed['body']['tasks'] if row['run'] == target)
    (tmp_path / 'original-access-receipt.json').write_text(json.dumps({
        'condition': condition, 'public_task': task, 'retained_fault': fault,
        'original_facts_before_read': before, 'original_facts_after_read': original_facts(controlled),
        'reader_exited': not reader.is_alive()}, indent=2))
    assert task['status'] not in {'running', 'passed', 'completed'}
    assert all(task.get(key) is None for key in ('completed_status', 'first_pass', 'final_pass'))
    if expected_code is not None:
        assert task['error_code'] == expected_code
        assert task['status'] == ('expired' if condition == 'expired' else 'engineering_interrupted')
    elif condition == 'inactive':
        assert task['observation'] == 'client_inactive'
    else:
        assert task['status'] == 'ready' and task['observation'] is None
    assert original_facts(controlled) == before
    assert controlled.requests == [] and controlled.side_requests == {'source': [], 'accounting': []}


def test_explicit_original_worker_can_finish_while_older_observation_is_reading_budget(
        acceptance_host, local_service, monkeypatch, tmp_path):
    controlled, operator, client = acceptance_host
    target = 'worker-after-honest-acceptance'
    captured, enumerate_binding, budget_read, release = (threading.Event() for _ in range(4))
    original_admin = controlled.app._admin
    original_report = controlled.budgets['primary'].report
    local, observed = threading.local(), {}

    def after_capture(method, path, environ):
        if method == 'GET' and path == '/monitor/snapshot':
            local.observation = True
            captured.set()
            assert enumerate_binding.wait(10)
        return original_admin(method, path, environ)

    def after_original_budget():
        result = original_report()
        if getattr(local, 'observation', False) and not budget_read.is_set():
            budget_read.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(controlled.app, '_admin', after_capture)
    monkeypatch.setattr(controlled.budgets['primary'], 'report', after_original_budget)

    def read_once():
        try:
            response = operator.get('/monitor/snapshot')
            observed.update(status=response.status_code, body=response.json())
        except BaseException as error:
            observed['error_type'] = type(error).__name__

    reader = threading.Thread(target=read_once, daemon=True)
    reader.start()
    completed = None
    try:
        assert captured.wait(5)
        accepted = client.post('/v1/tasks', json={'run_id': target, 'category': controlled.category,
            'prompt': 'Return only 1+1.'})
        assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
        assert controlled.requests == [], 'Submitting did not authorize an implicit worker'
        enumerate_binding.set()
        assert budget_read.wait(5)
        completed = controlled.queue.dispatch_next()  # The host explicitly runs the original worker.
        assert completed['status'] == 'passed' and completed['reason'] is None
        assert len(controlled.requests) == 1
    finally:
        enumerate_binding.set()
        release.set()
        reader.join(10)
        (tmp_path / 'original-worker-receipt.json').write_text(json.dumps({
            'public_snapshot': observed, 'original_worker_result': completed,
            'original_acceptance': original_acceptance(controlled, target),
            'physical_mock_posts': controlled.requests,
            'original_facts_after_worker': original_facts(controlled),
            'reader_exited': not reader.is_alive(), 'actual_http': local_service.http_records}, indent=2))
    assert not reader.is_alive() and observed.get('status') == 200
    current = client.get('/v1/tasks/' + target)
    assert current.status_code == 200 and current.json()['status'] == 'passed'
    assert current.json()['queue']['deadline'] == 1792542600.0
    assert controlled.queue.dispatch_next() is None and len(controlled.requests) == 1
    assert controlled.side_requests == {'source': [], 'accounting': []}


@pytest.mark.parametrize('legacy_queue', [False, True])
def test_default_and_noncontrolled_queue_observation_keeps_original_busy_guard(
        local_service, tmp_path, legacy_queue):
    from cloudyfan_jev.scheduler import QueueLimits, QueueScheduler
    from test_local_http_integration import TASK
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    service = bundle.app.gateway.service
    if legacy_queue:
        service.queue_scheduler = QueueScheduler(service, tmp_path / 'legacy-queue.sqlite',
            limits=QueueLimits(8, 2, 4, 120))
    client, operator = local_service.client(timeout=10), local_service.client('operator', timeout=10)
    assert client.post('/v1/tasks', json=TASK).json()['status'] == 'passed'
    before = list(bundle.upstream.calls)
    with service.retention.guard(service.internal_id('studio', TASK['run_id'])):
        snapshot = operator.get('/monitor/snapshot')
    assert snapshot.status_code == 200
    task = next(row for row in snapshot.json()['tasks'] if row['run'] == TASK['run_id'])
    assert task['status'] == 'passed' and task['observation'] == 'busy'
    assert bundle.upstream.calls == before
    (tmp_path / 'legacy-observation-guard-receipt.json').write_text(json.dumps({
        'legacy_queue_without_controlled_protocol': legacy_queue, 'public_task': task,
        'model_call_count_before_and_after': len(before), 'original_guard_preserved': True}, indent=2))
