"""Resilience at real authenticated loopback HTTP; upstream remains MockTransport."""
import hashlib
from pathlib import Path

import httpx

from capacity_resilience_fixture import build_resilience_host, build_resilience_health_host, copied_database_fault, reopen_windows
from capacity_windows_fixture import attach_windows
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network, TASK


def test_missing_saved_response_window_is_not_a_fresh_free_store(local_service):
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    def upstream(request):
        response = bundle.upstream(request)
        if len(bundle.upstream.calls) != 1:
            return response
        body = response.json()
        body.update(choices=[], error={'code': 429, 'message': 'SYNTHETIC_PRIVATE_RESILIENCE_ERROR'})
        return httpx.Response(429, json=body, headers={'Retry-After': '3600'})
    for contract in bundle.app.gateway.service.contracts.values():
        contract.runner.client.http = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
    controller, _ = attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    assert operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-window-admission-v1'}).status_code == 200
    first = client.post('/v1/tasks', json={**TASK, 'run_id': 'original-long-window'})
    assert len(bundle.upstream.calls) == 1 and first.json()['costs']['unknown_events'] == 0
    local_service.clock.now += 61
    before = operator.get('/monitor/capacity').json()['dispatch']
    assert before['groups'][0]['rpm_used'] == 0
    assert before['windows']['groups'][0]['status'] == 'wait'
    controller, _ = reopen_windows(bundle, controller, local_service.clock)
    local_service.server.set_app(bundle.app)
    assert operator.get('/monitor/capacity').json()['dispatch']['windows'] == before['windows']
    receipts = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (bundle.root / 'receipts').rglob('*.json')}
    copied_database_fault(controller.store.path, bundle.root / 'preserved-original', omit=('capacity_window_observations',))
    controller, _ = reopen_windows(bundle, controller, local_service.clock)
    local_service.server.set_app(bundle.app)
    denied = client.post('/v1/tasks', json={**TASK, 'run_id': 'after-window-table-loss'})
    assert denied.status_code in (429, 503), denied.text
    assert len(bundle.upstream.calls) == 1
    assert receipts == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (bundle.root / 'receipts').rglob('*.json') if str(p) in receipts}


def test_metadata_observation_is_read_only_and_later_http_authorization_inspects_again(local_service):
    import sqlite3
    import pytest
    from cloudyfan_jev.capacity_integrity import CapacityIntegrityError
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    controller, _ = attach_windows(bundle, local_service.clock)
    local_service.server.set_app(bundle.app)
    operator, client = local_service.client('operator'), local_service.client()
    assert operator.post('/monitor/capacity/activate', json={
        'revision': 0, 'version': 'synthetic-window-admission-v1'}).status_code == 200
    first = client.post('/v1/tasks', json={**TASK, 'run_id': 'read-scope-original'})
    assert first.status_code == 200 and first.json()['status'] == 'passed', first.text
    before = operator.get('/monitor/capacity').json()['dispatch']
    assert before['integrity']['status'] == 'verified'
    copied_database_fault(controller.store.path, bundle.root / 'read-scope-retained-original', updates=[(
        "UPDATE capacity_allocations SET state='sent',horizon=9999999999", ())])
    after = operator.get('/monitor/capacity').json()['dispatch']
    assert after['integrity']['status'] == 'unavailable'
    assert after['groups'][0]['observation_status'] == 'unavailable'
    original_bytes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (controller.store.path, controller.store.integrity.head)}
    with sqlite3.connect(controller.store.path.as_uri() + '?mode=ro', uri=True) as db:
        original_allocations = db.execute('SELECT * FROM capacity_allocations ORDER BY id').fetchall()
    with controller.store.integrity.observation():
        with controller.store.connection(projection=True) as db:
            assert db.execute('PRAGMA query_only').fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                db.execute('UPDATE capacity_allocations SET horizon=0')
        with pytest.raises(CapacityIntegrityError) as error:
            with controller.store.connection(write=True):
                raise AssertionError('Read observation authorized a metadata write')
        assert error.value.code == 'capacity_store_lock_upgrade_denied'
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        for path, digest in original_bytes.items())
    refused = client.post('/v1/tasks', json={**TASK, 'run_id': 'read-scope-after-fault'})
    assert refused.status_code in (429, 503), refused.text
    assert len(bundle.upstream.calls) == 1
    with sqlite3.connect(controller.store.path.as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('SELECT * FROM capacity_allocations ORDER BY id').fetchall() == original_allocations
    assert operator.get('/monitor/capacity').json()['dispatch']['integrity']['status'] == 'unavailable'


def test_accepting_cancel_before_original_job_exists_is_a_durable_readable_tombstone(local_service):
    import fcntl
    import sqlite3
    host = build_resilience_host(local_service.bundle.root / 'acceptance', clock=local_service.clock,
        origin=local_service.origin)
    local_service.bundle = host.bundle
    local_service.server.set_app(host.app)
    operator, client = local_service.client('operator'), local_service.client()
    assert operator.post('/monitor/capacity/activate', json={
        'revision': 0, 'version': 'synthetic-queued-admission-v1'}).status_code == 200
    payload = {**TASK, 'run_id': 'accepting-cancel', 'prompt': 'PRIVATE_RESILIENCE_TEXT_CANARY 1+1'}
    identity = host.queue.service.internal_id('studio', payload['run_id'])
    directory = host.queue.service.root / identity
    directory.mkdir(mode=0o700)
    with (directory / '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        accepted = client.post('/v1/tasks', json=payload)
        assert accepted.status_code == 202 and accepted.json()['status'] == 'accepting'
        assert not (directory / 'job.json').exists()
        cancelled = client.post('/v1/tasks/accepting-cancel/cancel', json={})
        assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled', cancelled.text
    assert client.post('/v1/tasks', json=payload).json() == cancelled.json()
    assert host.queue.dispatch_next() is None
    with sqlite3.connect(host.controller.store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM capacity_adoptions WHERE terminal=0').fetchone()[0] == 0
        assert db.execute('SELECT run FROM capacity_terminal_fences').fetchall() == [(identity,)]
    for root in (host.queue.integrity.root, host.controller.store.integrity.root):
        assert all(b'PRIVATE_RESILIENCE_TEXT_CANARY' not in p.read_bytes() for p in root.rglob('*.json'))
    reopened = build_resilience_host(host.bundle.root, clock=local_service.clock, origin=local_service.origin)
    try:
        local_service.bundle = reopened.bundle
        local_service.server.set_app(reopened.app)
        current = client.get('/v1/tasks/accepting-cancel').json()
        assert current['status'] == 'cancelled'
        assert current['queue']['accepted_at'] == accepted.json()['queue']['accepted_at']
        assert current['queue']['deadline'] == accepted.json()['queue']['deadline']
        assert reopened.queue.dispatch_next() is None
        assert host.upstream.calls == reopened.upstream.calls == []
    finally:
        reopened.close()
        host.close()


def test_actual_backup_health_queue_factory_preserves_fallback_and_same_identity_on_reopen(local_service):
    from datetime import datetime
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    fixture = build_resilience_health_host(local_service.bundle.root / 'health-host',
        clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = fixture.host
    local_service.server.set_app(fixture.app)
    operator, client = local_service.client('operator'), local_service.client()
    try:
        activated = operator.post('/monitor/capacity/activate', json={
            'revision': 0, 'version': fixture.policy_version})
        assert activated.status_code == 200, activated.text
        fixture.flags['scenario'] = 'availability'
        accepted = client.post('/v1/tasks', json={'run_id': 'health-real-fallback',
            'category': fixture.category, 'prompt': 'Return 1+1.'})
        assert accepted.status_code == 202, accepted.text
        for _ in range(8):
            state = fixture.queue.dispatch_next()
            if state is not None and state['status'] == 'passed':
                break
        assert state['status'] == 'passed', state
        assert len(fixture.requests) == 2
        assert fixture.requests[0]['model'] != fixture.requests[1]['model']
        assert fixture.budgets['primary'].report()['known_usd'] == '0.02'
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for directory in (
            fixture.host.root / 'receipts-primary', fixture.host.root / 'health')
            for p in directory.rglob('*') if p.is_file()}
        reopened = build_resilience_health_host(fixture.host.root, clock=local_service.clock,
            origin=local_service.origin)
        try:
            local_service.bundle = reopened.host
            local_service.server.set_app(reopened.app)
            assert reopened.credentials == fixture.credentials
            observed = client.get('/v1/tasks/health-real-fallback')
            assert observed.status_code == 200 and observed.json()['status'] == 'passed', observed.text
            assert operator.get('/monitor/queue').status_code == 200
            assert reopened.requests == []
            assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                for path, digest in before.items())
        finally:
            reopened.close()
    finally:
        fixture.close()


def test_saved_backup_observation_does_not_rebuild_execution_core_while_another_post_is_held(local_service):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    import json
    import time
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    fixture = build_resilience_health_host(local_service.bundle.root / 'observation-read',
        clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = fixture.host
    local_service.server.set_app(fixture.app)
    operator, client = local_service.client('operator'), local_service.client()
    running, audit = None, []
    workers = ThreadPoolExecutor(1)
    try:
        assert operator.post('/monitor/capacity/activate', json={
            'revision': 0, 'version': fixture.policy_version}).status_code == 200
        payload = {'category': fixture.category, 'prompt': 'Return 1+1.'}
        assert client.post('/v1/tasks', json={**payload, 'run_id': 'observation-complete'}).status_code == 202
        assert fixture.queue.dispatch_next()['status'] == 'passed'
        before = operator.get('/monitor/snapshot')
        assert before.status_code == 200
        original_history = before.json()['tasks'][0]['operator_observation']['task_decision']
        assert original_history['candidate_evidence'] == 'saved_selection'
        assert original_history['attempts'] and original_history['stages']
        fixture.flags['hold'] = True
        assert client.post('/v1/tasks', json={**payload, 'run_id': 'observation-held'}).status_code == 202
        running = workers.submit(fixture.queue.dispatch_next)
        assert fixture.held.wait(5)
        original_factories = list(fixture.factory_calls)
        originals = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in (fixture.host.root / 'receipts-primary', fixture.host.root / 'health')
            for p in directory.rglob('*') if p.is_file()}
        for _ in range(3):
            started = time.monotonic()
            response = operator.get('/monitor/snapshot')
            audit.append({'status': response.status_code, 'elapsed_seconds': time.monotonic() - started,
                'factory_calls_before': len(original_factories), 'factory_calls_after': len(fixture.factory_calls),
                'physical_posts': len(fixture.requests), 'completion_signal_released': fixture.released.is_set()})
            (fixture.host.root / 'snapshot-read-audit.json').write_text(json.dumps(audit, indent=2))
            assert response.status_code == 200, response.text
            completed = next(row for row in response.json()['tasks'] if row['run'] == 'observation-complete')
            assert completed['status'] == 'passed'
            assert completed['operator_observation']['task_decision'] == original_history
            assert fixture.factory_calls == original_factories, 'Pure saved-evidence reads rebuilt an execution Core'
            assert not fixture.released.is_set() and len(fixture.requests) == 2
            assert fixture.side_requests == {'source': [], 'accounting': []}
            assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                for path, digest in originals.items())
        internal = fixture.host.service.internal_id('studio', 'observation-complete')
        manifest = fixture.host.root / 'receipts-primary' / 'runs' / internal / 'manifest.json'
        retained = manifest.with_name('original-manifest-retained.json')
        manifest.rename(retained)
        damaged = json.loads(retained.read_text())
        damaged['engine_version'] = 'synthetic-changed-configuration'
        manifest.write_text(json.dumps(damaged))
        refused = client.get('/v1/tasks/observation-complete/result')
        assert refused.status_code == 503 and refused.json()['error']['code'] == 'result_evidence_unavailable'
        assert hashlib.sha256(retained.read_bytes()).hexdigest() == originals[str(manifest)]
        assert fixture.factory_calls == original_factories and len(fixture.requests) == 2
        assert fixture.side_requests == {'source': [], 'accounting': []}
    finally:
        fixture.released.set()
        if running is not None:
            running.result(timeout=20)
        workers.shutdown()
        fixture.close()


def test_custom_run_dependent_completed_reader_keeps_original_factory_protocol(local_service):
    from dataclasses import replace
    from cloudyfan_jev.budget import BudgetedCore
    from cloudyfan_jev.engine import Core
    from cloudyfan_monitor.integration import RunBoundCore
    bundle = local_service.bundle
    bundle.upstream.fail_tiers = set()
    service = bundle.app.gateway.service
    original = service.contracts['demo-cloudyfan']
    template, calls = original.runner.core, []
    class CustomReadCore(Core):
        def __init__(self, *args, run_id, **kwargs):
            super().__init__(*args, **kwargs)
            self.read_run_id = run_id

        def _with_costs(self, result):
            if result['run_id'] != self.read_run_id:
                raise ValueError('Completed reader requires its actual per-run construction')
            return super()._with_costs(result)

    def factory(run_id, client, local):
        calls.append(run_id)
        return CustomReadCore(client, template.profiles, template.decision, run_id=run_id,
            limits=template.limits, system=template.system, local_backend=local,
            diagnostic_contract=template.diagnostic_contract, execution_policy=template.execution_policy)
    bound_template = factory('custom-reader-template', template.client, template.local_backend)
    runner = BudgetedCore(RunBoundCore(bound_template, factory), original.runner.store,
        model_bounds=original.runner.model_bounds, tool_bounds={}, verify_bound=original.runner.verify_bound)
    service.contracts['demo-cloudyfan'] = replace(original, runner=runner)
    client = local_service.client()
    created = client.post('/v1/tasks', json={**TASK, 'run_id': 'custom-reader'})
    assert created.status_code == 200 and created.json()['status'] == 'passed', created.text
    before = len(calls)
    observed = client.get('/v1/tasks/custom-reader/result')
    assert observed.status_code == 200 and observed.json()['answer'] == '2', observed.text
    assert len(calls) == before + 1
    assert calls[-1] == service.internal_id('studio', 'custom-reader')
    assert len(bundle.upstream.calls) == 1


def test_cancelled_acceptance_does_not_reopen_thirty_day_access_on_replay_or_resume(local_service):
    import fcntl
    host = build_resilience_host(local_service.bundle.root / 'expired-acceptance', clock=local_service.clock,
        origin=local_service.origin)
    local_service.bundle = host.bundle
    local_service.server.set_app(host.app)
    operator, client = local_service.client('operator'), local_service.client()
    assert operator.post('/monitor/capacity/activate', json={
        'revision': 0, 'version': 'synthetic-queued-admission-v1'}).status_code == 200
    payload = {**TASK, 'run_id': 'expired-acceptance', 'prompt': 'PRIVATE_EXPIRED_RESILIENCE_TEXT 1+1'}
    identity = host.queue.service.internal_id('studio', payload['run_id'])
    directory = host.queue.service.root / identity
    directory.mkdir(mode=0o700)
    with (directory / '.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert client.post('/v1/tasks', json=payload).status_code == 202
        assert client.post('/v1/tasks/expired-acceptance/cancel', json={}).status_code == 200
    lifecycle = host.queue.service.retention.lookup(identity)
    local_service.clock.now = lifecycle['expires_at'] + 1
    reopened = build_resilience_host(host.bundle.root, clock=local_service.clock, origin=local_service.origin)
    try:
        local_service.bundle = reopened.bundle
        local_service.server.set_app(reopened.app)
        for path, body in (('/v1/tasks/expired-acceptance/resume', {}), ('/v1/tasks', payload)):
            response = client.post(path, json=body)
            assert response.status_code == 410, response.text
            assert 'PRIVATE_EXPIRED_RESILIENCE_TEXT' not in response.text
        assert client.get('/v1/tasks/expired-acceptance').status_code == 410
        assert client.get('/v1/tasks/expired-acceptance/result').status_code == 410
        assert host.queue.service.retention.lookup(identity)['expires_at'] == lifecycle['expires_at']
        assert reopened.upstream.calls == host.upstream.calls == []
    finally:
        reopened.close()
        host.close()


def test_queued_known_and_unknown_fees_keep_original_access_and_year_long_evidence(local_service):
    from datetime import datetime
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    fixture = build_resilience_health_host(local_service.bundle.root / 'retained-health',
        clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = fixture.host
    local_service.server.set_app(fixture.app)
    operator, client = local_service.client('operator'), local_service.client()
    day = 86400
    try:
        assert operator.post('/monitor/capacity/activate', json={
            'revision': 0, 'version': fixture.policy_version}).status_code == 200
        payload = {'category': fixture.category, 'prompt': 'PRIVATE_RETAINED_CAPACITY_TEXT Return 1+1.'}
        assert client.post('/v1/tasks', json={**payload, 'run_id': 'retained-known'}).status_code == 202
        assert fixture.queue.dispatch_next()['status'] == 'passed'
        fixture.flags['scenario'] = 'unknown_fee'
        assert client.post('/v1/tasks', json={**payload, 'run_id': 'retained-unknown'}).status_code == 202
        unknown = fixture.queue.dispatch_next()
        assert unknown['status'] == 'interrupted_pending'
        budget = fixture.budgets['primary'].report()
        assert budget['known_usd'] == '.01' or budget['known_usd'] == '0.01'
        assert budget['unknown_events'] == 1 and budget['dispatch_blocked'] is True
        start = fixture.host.service.retention.lookup(
            fixture.host.service.internal_id('studio', 'retained-known'))
        originals = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for directory in (
            fixture.host.root / 'receipts-primary', fixture.host.root / 'health')
            for p in directory.rglob('*') if p.is_file()}
        calls = list(fixture.requests)
        local_service.clock.now = start['expires_at'] - 1
        assert client.get('/v1/tasks/retained-known/result').status_code == 200
        local_service.clock.now += 1
        assert client.get('/v1/tasks/retained-known/result').status_code == 410
        for elapsed in (31 * day, 365 * day, 366 * day):
            local_service.clock.now = start['created_at'] + elapsed
            for run in ('retained-known', 'retained-unknown'):
                assert client.get('/v1/tasks/' + run).status_code == 410
                assert client.post('/v1/tasks/' + run + '/resume', json={}).status_code == 410
            preview = operator.post('/monitor/retention/preview', json={
                'client': 'studio', 'run_id': 'retained-unknown'})
            assert preview.status_code == 200, preview.text
            saved = preview.json()
            assert saved['policy']['evidence_min_days'] == 365 and saved['policy']['automatic_deletion'] is False
            assert saved['evidence_retain_until'] >= start['created_at'] + 365 * day
            assert 'unknown_accounting' in saved['blockers'] and saved['eligible'] is False
            assert 'PRIVATE_RETAINED_CAPACITY_TEXT' not in preview.text
            capacity = operator.get('/monitor/capacity').json()['dispatch']
            assert any(group['concurrency_used'] >= 1 for group in capacity['groups'])
            assert operator.get('/monitor/queue').status_code == 200
        reopened = build_resilience_health_host(fixture.host.root, clock=local_service.clock,
            origin=local_service.origin)
        try:
            local_service.bundle = reopened.host
            local_service.server.set_app(reopened.app)
            assert client.post('/v1/tasks/retained-known/resume', json={}).status_code == 410
            assert reopened.budgets['primary'].report() == budget
            assert reopened.requests == [] and fixture.requests == calls
            assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value for path, value in originals.items())
            assert reopened.host.service.retention.lookup(
                reopened.host.service.internal_id('studio', 'retained-known'))['expires_at'] == start['expires_at']
            for root in (reopened.queue.integrity.root, reopened.controller.store.integrity.root):
                assert all(b'PRIVATE_RETAINED_CAPACITY_TEXT' not in p.read_bytes() for p in root.rglob('*.json'))
        finally:
            reopened.close()
    finally:
        fixture.close()
