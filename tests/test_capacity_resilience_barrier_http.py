"""A real operator history read must not own unrelated dispatch authority."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import traceback

from capacity_resilience_fixture import build_resilience_health_host
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network


def test_original_budget_read_does_not_hold_unrelated_queue_dispatch(local_service, monkeypatch, tmp_path):
    """The Event is after the unmodified original Budget report has returned.

    The HTTP response, original financial values, real queue and paid guards are
    never substituted. A and B have distinct runs and original Client/Budget
    roots; B first completes a real control before its second accepted task.
    """
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    fixture = build_resilience_health_host(tmp_path / 'barrier-host',
        clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = fixture.host
    local_service.server.set_app(fixture.app)
    entered, release, worker_done = threading.Event(), threading.Event(), threading.Event()
    audit = {'scope': 'actual loopback HTTP; actual Backup/Health/Queue/Capacity/Budget; MockTransport upstream',
        'events': [], 'http': [], 'worker': {}, 'operator': {}, 'pause_seconds_bound': 10,
        'progress_seconds_bound': 5, 'armed': False}
    receipt = tmp_path / 'barrier-evidence.json'
    workers = []

    def event(name, **facts):
        audit['events'].append({'event': name, 'monotonic_ns': time.monotonic_ns(),
            'utc': datetime.now(timezone.utc).isoformat(), **facts})

    def save():
        receipt.write_text(json.dumps(audit, indent=2, sort_keys=True))

    def http(client, method, path, **kwargs):
        started = time.monotonic_ns()
        response = client.request(method, path, **kwargs)
        audit['http'].append({'method': method, 'path': path, 'status': response.status_code,
            'started_ns': started, 'finished_ns': time.monotonic_ns(), 'body': response.json()})
        save()
        return response

    def exclusive_probe(path):
        with Path(path).open('r+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
                return True

    def budget_transaction_probe(store):
        with sqlite3.connect(store.path, timeout=0, isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('ROLLBACK')
        return True

    def fingerprints(directory):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob('*') if path.is_file()}

    def dispatch_one():
        event('b_worker_started')
        try:
            audit['worker']['result'] = fixture.queue.dispatch_next()
        except BaseException as error:
            audit['worker']['error_type'] = type(error).__name__
        finally:
            event('b_worker_returned', released=release.is_set(), physical_posts=len(fixture.requests))
            worker_done.set()

    def read_operator():
        event('operator_http_started')
        try:
            response = http(operator, 'GET', '/monitor/snapshot')
            audit['operator'].update(status=response.status_code)
        except BaseException as error:
            audit['operator']['error_type'] = type(error).__name__
        finally:
            event('operator_http_returned', released=release.is_set())

    operator = local_service.client('operator', timeout=10)
    primary = local_service.client(timeout=10)
    disjoint = local_service.client('agent', timeout=10)
    original_report = fixture.budgets['primary'].report
    try:
        activated = http(operator, 'POST', '/monitor/capacity/activate', json={
            'revision': 0, 'version': fixture.policy_version})
        assert activated.status_code == 200, activated.text
        payload = {'category': fixture.category, 'prompt': 'PRIVATE_BARRIER_PROMPT Return 1+1.'}
        first = http(primary, 'POST', '/v1/tasks', json={**payload, 'run_id': 'barrier-a-complete'})
        assert first.status_code == 202, first.text
        assert fixture.queue.dispatch_next()['status'] == 'passed'
        view = http(operator, 'GET', '/monitor/snapshot')
        assert view.status_code == 200, view.text
        changed = http(operator, 'POST', '/monitor/mode', json={
            'revision': view.json()['control']['revision'], 'client': 'agent', 'mode': 'open_source_first'})
        assert changed.status_code == 200, changed.text
        control = http(disjoint, 'POST', '/v1/tasks', json={**payload, 'run_id': 'barrier-b-control'})
        assert control.status_code == 202, control.text
        assert fixture.queue.dispatch_next()['status'] == 'passed'
        control_result = http(disjoint, 'GET', '/v1/tasks/barrier-b-control')
        assert control_result.status_code == 200 and control_result.json()['status'] == 'passed'
        assert [row['lane'] for row in fixture.requests] == ['primary', 'disjoint']
        assert fixture.budgets['primary'].path != fixture.budgets['disjoint'].path
        audit['control'] = {'physical_posts': list(fixture.requests),
            'fees': {lane: budget.report() for lane, budget in fixture.budgets.items()},
            'health': fingerprints(fixture.host.root / 'health'),
            'original_a': fingerprints(fixture.host.root / 'receipts-primary')}
        assert audit['control']['fees']['disjoint']['known_usd'] == '0.01'
        accepted = http(disjoint, 'POST', '/v1/tasks', json={**payload, 'run_id': 'barrier-b-during-read'})
        assert accepted.status_code == 202 and accepted.json()['status'] == 'queued', accepted.text
        event('b_already_accepted', physical_posts=len(fixture.requests))

        def gated_report():
            report = original_report()
            # The real ledger calls this after refreshing saved task metadata.
            # No SQL context, Core run lock, Health lock, or fixture mutex is
            # retained by this transparent after-return wrapper.
            frame = sys._getframe(1)
            in_ledger = False
            while frame is not None:
                in_ledger |= frame.f_code.co_name == 'ledger' and Path(frame.f_code.co_filename).name == 'gateway.py'
                frame = frame.f_back
            if audit['armed'] and in_ledger:
                audit['armed'] = False
                audit['pause_controls'] = {
                    'primary_budget_transaction_available': budget_transaction_probe(fixture.budgets['primary']),
                    'disjoint_budget_transaction_available': budget_transaction_probe(fixture.budgets['disjoint']),
                    'health_exclusive_lock_available': exclusive_probe(fixture.host.health.root / '.health.lock'),
                    'original_financial_report': report,
                    'original_report_returned': True,
                    'barrier_location': 'original primary Budget.report returned to Gateway.ledger'}
                event('original_budget_report_returned_and_paused')
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Synthetic after-report barrier timed out')
                event('original_budget_report_resumed')
            return report

        monkeypatch.setattr(fixture.budgets['primary'], 'report', gated_report)
        audit['armed'] = True
        reader = threading.Thread(target=read_operator, name='barrier-operator', daemon=True)
        workers.append(reader)
        reader.start()
        assert entered.wait(5), 'Actual operator ledger never reached the original after-report barrier'
        assert all(audit['pause_controls'][key] for key in (
            'primary_budget_transaction_available', 'disjoint_budget_transaction_available',
            'health_exclusive_lock_available'))
        audit['authority_lock_probes_before_worker'] = {
            'queue_exclusive_available': exclusive_probe(fixture.queue.integrity.lock_path),
            'capacity_exclusive_available': exclusive_probe(fixture.controller.store.integrity.lock_path)}
        worker = threading.Thread(target=dispatch_one, name='barrier-real-b-worker', daemon=True)
        workers.append(worker)
        worker.start()
        progressed = worker_done.wait(5)
        audit['while_paused'] = {'worker_returned': progressed, 'barrier_released': release.is_set(),
            'physical_posts': list(fixture.requests), 'operator_thread_alive': reader.is_alive()}
        frame = sys._current_frames().get(worker.ident)
        if frame is not None:
            audit['while_paused']['worker_stack'] = [dict(filename=row.filename, lineno=row.lineno,
                name=row.name, line=row.line) for row in traceback.extract_stack(frame)]
            lock_frames = []
            while frame is not None:
                if frame.f_code.co_name == 'lock' and Path(frame.f_code.co_filename).name == 'capacity_integrity.py':
                    lock_frames.append({'line': frame.f_lineno,
                        'actual_queue_integrity': frame.f_locals.get('self') is fixture.queue.integrity,
                        'actual_capacity_integrity': frame.f_locals.get('self') is fixture.controller.store.integrity,
                        'requested_write': frame.f_locals.get('write') is True})
                frame = frame.f_back
            audit['while_paused']['actual_metadata_lock_frames'] = lock_frames
        event('progress_boundary_observed', worker_returned=progressed,
            physical_posts=len(fixture.requests), released=release.is_set())
        save()
    finally:
        event('barrier_finally_released')
        release.set()
        for thread in workers:
            thread.join(10)
        audit['cleanup'] = {'threads': {thread.name: thread.is_alive() for thread in workers},
            'side_requests': fixture.side_requests, 'physical_posts': list(fixture.requests)}
        audit['after_release'] = {'fees': {lane: budget.report() for lane, budget in fixture.budgets.items()},
            'health': fingerprints(fixture.host.root / 'health'),
            'original_a': fingerprints(fixture.host.root / 'receipts-primary')}
        save()
        fixture.close()

    assert not any(audit['cleanup']['threads'].values()), audit['cleanup']
    assert audit['operator'].get('status') == 200, audit['operator']
    assert audit['worker'].get('result', {}).get('status') == 'passed', audit['worker']
    assert len(fixture.requests) == 3 and [row['lane'] for row in fixture.requests] == ['primary', 'disjoint', 'disjoint']
    assert audit['after_release']['fees']['disjoint']['known_usd'] == '0.02'
    assert audit['after_release']['fees']['primary'] == audit['control']['fees']['primary']
    assert audit['after_release']['original_a'] == audit['control']['original_a']
    assert fixture.side_requests == {'source': [], 'accounting': []}
    assert audit['while_paused']['worker_returned'] and len(audit['while_paused']['physical_posts']) == 3, (
        'The real disjoint accepted task was blocked by an unrelated operator read after its original Budget '
        'transaction had exited. See barrier-evidence.json; late completion after release is not progress while paused.')
