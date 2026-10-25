"""Detached observations preserve old facts without becoming paid authority."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from cloudyfan_jev.capacity_integrity import read_views
from capacity_resilience_fixture import build_resilience_health_host, copied_database_fault
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network


@pytest.fixture
def resilience_host(local_service, tmp_path):
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    host = build_resilience_health_host(tmp_path / 'host', clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = host.host
    local_service.server.set_app(host.app)
    operator = local_service.client('operator', timeout=10)
    assert operator.post('/monitor/capacity/activate', json={
        'revision': 0, 'version': host.policy_version}).status_code == 200
    payload = {'category': host.category, 'prompt': 'PRIVATE_READ_VIEW_PROMPT Return 1+1.'}
    primary, disjoint = local_service.client(timeout=10), local_service.client('agent', timeout=10)
    assert primary.post('/v1/tasks', json={**payload, 'run_id': 'view-a'}).status_code == 202
    assert host.queue.dispatch_next()['status'] == 'passed'
    revision = operator.get('/monitor/snapshot').json()['control']['revision']
    assert operator.post('/monitor/mode', json={'revision': revision, 'client': 'agent',
        'mode': 'open_source_first'}).status_code == 200
    assert disjoint.post('/v1/tasks', json={**payload, 'run_id': 'view-b-control'}).status_code == 202
    assert host.queue.dispatch_next()['status'] == 'passed'
    assert [row['lane'] for row in host.requests] == ['primary', 'disjoint']
    try:
        yield host, operator, disjoint, payload
    finally:
        host.close()


def hashes(*roots):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots for path in (root.rglob('*') if root.is_dir() else (root,)) if path.is_file()}


def test_captured_http_capacity_survives_later_loss_but_next_original_guard_refuses(resilience_host, monkeypatch, tmp_path):
    host, operator, disjoint, payload = resilience_host
    baseline = operator.get('/monitor/capacity')
    assert baseline.status_code == 200 and baseline.json()['dispatch']['integrity']['status'] == 'verified'
    accepted = disjoint.post('/v1/tasks', json={**payload, 'run_id': 'view-b-after-capture'})
    assert accepted.status_code == 202 and accepted.json()['status'] == 'queued'
    baseline = operator.get('/monitor/capacity').json()['dispatch']
    fees = {lane: budget.report() for lane, budget in host.budgets.items()}
    originals = hashes(host.host.root / 'receipts-primary', host.host.root / 'receipts-disjoint',
        host.host.root / 'health', host.controller.store.integrity.root)
    entered, release = threading.Event(), threading.Event()
    outcome, armed = {}, True
    original_admin = host.app._admin

    def gated_admin(method, path, environ):
        nonlocal armed
        if method == 'GET' and path == '/monitor/capacity' and armed:
            armed = False
            entered.set()  # App has completed both real immutable captures.
            assert release.wait(10)
        return original_admin(method, path, environ)

    def observe():
        try:
            response = operator.get('/monitor/capacity')
            outcome.update(status=response.status_code, body=response.json())
        except BaseException as error:
            outcome['error_type'] = type(error).__name__

    monkeypatch.setattr(host.app, '_admin', gated_admin)
    reader = threading.Thread(target=observe, daemon=True)
    reader.start()
    try:
        assert entered.wait(5)
        fault = copied_database_fault(host.controller.store.path, tmp_path / 'retained-capacity',
            omit=('capacity_allocations',))
        assert host.queue.dispatch_next() is None
        refused = disjoint.post('/v1/tasks', json={**payload, 'run_id': 'view-b-fresh-refused'})
        assert refused.status_code in (429, 503), refused.text
        assert len(host.requests) == 2
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive() and outcome.get('status') == 200, outcome
    assert outcome['body']['dispatch'] == baseline, 'A later original fault changed the captured response'
    current = operator.get('/monitor/capacity')
    assert current.status_code == 200
    assert current.json()['dispatch']['integrity']['status'] == 'unavailable'
    assert {lane: budget.report() for lane, budget in host.budgets.items()} == fees
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in originals.items())
    assert host.side_requests == {'source': [], 'accounting': []}
    (tmp_path / 'captured-versus-fresh.json').write_text(json.dumps({'captured': outcome,
        'fresh': current.json(), 'fresh_refused': {'status': refused.status_code, 'body': refused.json()},
        'fault': fault, 'physical_posts': host.requests, 'fees': fees,
        'originals_unchanged': True, 'reader_exited': not reader.is_alive()}, indent=2))


def test_read_copy_rejects_sql_write_bind_claim_and_execution_authority(resilience_host, tmp_path):
    host, operator, disjoint, payload = resilience_host
    assert disjoint.post('/v1/tasks', json={**payload, 'run_id': 'view-b-after-read-scope'}).status_code == 202
    identity = host.host.service.internal_id('agent', 'view-b-after-read-scope')
    _, job = host.host.service._job('agent', 'view-b-after-read-scope')
    controller = host.controller
    adoption = controller.context(identity, 'agent', job['registered'])
    originals = hashes(controller.store.path, controller.store.integrity.root,
        host.queue.path, host.queue.integrity.root, host.host.root / 'health',
        *(budget.path for budget in host.budgets.values()))
    refused = []
    with read_views([host.queue.integrity, controller.store.integrity]):
        assert controller.read_context(identity, 'agent', job['registered']) == adoption
        with controller.store.connection() as db:
            assert db.execute('PRAGMA query_only').fetchone()[0] == 1
            for sql in ('UPDATE capacity_adoptions SET terminal=1', 'PRAGMA query_only=OFF'):
                with pytest.raises(sqlite3.DatabaseError):
                    db.execute(sql)
        for name, operation in (
            ('require', lambda: controller.store.integrity.require()),
            ('execution_context', lambda: controller.context(identity, 'agent', job['registered'])),
            ('verify', lambda: controller.verify(adoption)),
            ('bind', lambda: controller.store.integrity.bind(tmp_path, 'forbidden-read-bind')),
            ('claim', host.queue.dispatch_next),
        ):
            with pytest.raises(RuntimeError) as error:
                operation()
            refused.append({'operation': name, 'code': error.value.code})
        with pytest.raises(RuntimeError) as error:
            with controller.store.connection(write=True):
                raise AssertionError('Read scope entered a metadata write')
        refused.append({'operation': 'write_transaction', 'code': error.value.code})
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in originals.items())
    assert len(host.requests) == 2
    controller.verify(adoption)  # Leaving the scope restores fresh original verification.
    result = host.queue.dispatch_next()
    assert result['status'] == 'passed' and len(host.requests) == 3
    assert disjoint.get('/v1/tasks/view-b-after-read-scope').json()['status'] == 'passed'
    assert host.side_requests == {'source': [], 'accounting': []}
    (tmp_path / 'read-copy-refusals.json').write_text(json.dumps({'refused': refused,
        'originals_unchanged_before_actual_dispatch': True, 'late_original_dispatch': result,
        'physical_posts': host.requests, 'fees': {lane: budget.report() for lane, budget in host.budgets.items()}}, indent=2))


def test_old_http_snapshot_cannot_overwrite_a_later_completed_current_task(resilience_host, monkeypatch, tmp_path):
    host, operator, disjoint, payload = resilience_host
    assert disjoint.post('/v1/tasks', json={**payload, 'run_id': 'view-b-finish-after-capture'}).status_code == 202
    entered, release = threading.Event(), threading.Event()
    original_capture = host.app.gateway.capture
    outcome, armed = {}, True

    def after_original_projection(binding, *args, **kwargs):
        nonlocal armed
        projected = original_capture(binding, *args, **kwargs)
        if binding['run'] == 'view-b-finish-after-capture' and armed:
            armed = False
            assert kwargs.get('persist') is False
            assert projected['status'] == 'queued'
            entered.set()  # Original costs/history SQL reads have returned.
            assert release.wait(10)
        return projected

    def observe():
        try:
            response = operator.get('/monitor/snapshot')
            outcome.update(status=response.status_code, body=response.json())
        except BaseException as error:
            outcome['error_type'] = type(error).__name__

    monkeypatch.setattr(host.app.gateway, 'capture', after_original_projection)
    reader = threading.Thread(target=observe, daemon=True)
    reader.start()
    try:
        assert entered.wait(5)
        completed = host.queue.dispatch_next()
        assert completed['status'] == 'passed'
        current = host.app.gateway.store.observation('agent', 'view-b-finish-after-capture')
        assert current['status'] == 'passed'
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive() and outcome.get('status') == 200, outcome
    old_task = next(row for row in outcome['body']['tasks'] if row['run'] == 'view-b-finish-after-capture')
    assert old_task['status'] == 'queued' and old_task['complete_api_usd'] is None
    assert host.app.gateway.store.observation('agent', 'view-b-finish-after-capture') == current
    next_view = operator.get('/monitor/snapshot')
    assert next_view.status_code == 200
    fresh_task = next(row for row in next_view.json()['tasks'] if row['run'] == 'view-b-finish-after-capture')
    assert fresh_task['status'] == 'passed'
    assert host.app.gateway.store.observation('agent', 'view-b-finish-after-capture') == current
    assert len(host.requests) == 3 and host.budgets['disjoint'].report()['known_usd'] == '0.02'
    assert host.side_requests == {'source': [], 'accounting': []}
    (tmp_path / 'old-view-versus-current.json').write_text(json.dumps({'old_response_task': old_task,
        'current_preserved': current, 'next_response_task': fresh_task, 'reader_exited': not reader.is_alive(),
        'physical_posts': host.requests, 'fees': {lane: budget.report() for lane, budget in host.budgets.items()}}, indent=2))


def test_old_active_copy_does_not_inherit_newer_completed_summary(resilience_host, monkeypatch, tmp_path):
    host, operator, disjoint, payload = resilience_host
    assert disjoint.post('/v1/tasks', json={**payload, 'run_id': 'view-b-complete-before-history'}).status_code == 202
    entered, release = threading.Event(), threading.Event()
    original_admin = host.app._admin
    outcome, armed = {}, True

    def gated_admin(method, path, environ):
        nonlocal armed
        if method == 'GET' and path == '/monitor/snapshot' and armed:
            armed = False
            entered.set()  # Metadata copy precedes the later original task/Store read.
            assert release.wait(10)
        return original_admin(method, path, environ)

    def observe():
        try:
            response = operator.get('/monitor/snapshot')
            outcome.update(status=response.status_code, body=response.json())
        except BaseException as error:
            outcome['error_type'] = type(error).__name__

    monkeypatch.setattr(host.app, '_admin', gated_admin)
    reader = threading.Thread(target=observe, daemon=True)
    reader.start()
    try:
        assert entered.wait(5)
        assert host.queue.dispatch_next()['status'] == 'passed'
        current = host.app.gateway.store.observation('agent', 'view-b-complete-before-history')
        assert current['completed_status'] == 'passed' and current['final_pass'] is True
    finally:
        release.set()
        reader.join(10)
    assert not reader.is_alive() and outcome.get('status') == 200, outcome
    old = next(row for row in outcome['body']['tasks'] if row['run'] == 'view-b-complete-before-history')
    retained = host.app.gateway.store.observation('agent', 'view-b-complete-before-history')
    (tmp_path / 'different-capture-times.json').write_text(json.dumps({'old_response_task': old,
        'current_completed': current, 'current_after_old_response': retained, 'physical_posts': host.requests,
        'fees': {lane: budget.report() for lane, budget in host.budgets.items()}}, indent=2))
    assert old['status'] == 'queued' and old['complete_api_usd'] is None
    assert retained == current and len(host.requests) == 3
    assert old['completed_status'] is None and old['first_pass'] is None and old['final_pass'] is None, (
        'A captured queued row inherited a newer completed task summary from another observation time')
