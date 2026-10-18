"""Retention uses the owned HTTP task seam and disposable private fixtures only."""
import json

import httpx

from cloudyfan_jev.http_api import HTTPApp, TaskService
from cloudyfan_jev.retention import RetentionPolicy
from test_http_api import TASK, service_client


def retained_service(tmp_path, *, ttl=60, api=None):
    _, original, runner, api, _ = service_client(tmp_path, api=api)
    clock = [1000.0]
    service = TaskService(original.root, contracts=original.contracts, allowed_principal='admin',
                          retention_policy=RetentionPolicy('fixture-v1', ttl), clock=lambda: clock[0])
    app = HTTPApp(service, authenticate=lambda environ: 'admin')
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture')
    return client, service, runner, api, clock


def test_expired_task_cannot_be_read_resumed_or_recreated_after_restart(tmp_path):
    client, service, runner, api, clock = retained_service(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    clock[0] = 1060.0
    for method, path, payload in [('GET', '/v1/tasks/task-1', None),
        ('GET', '/v1/tasks/task-1/result', None), ('POST', '/v1/tasks/task-1/resume', {}),
        ('POST', '/v1/tasks', TASK)]:
        response = client.request(method, path, json=payload)
        assert response.status_code == 410
        assert response.json() == {'error': {'code': 'task_expired'}}
    restarted = TaskService(service.root, contracts=service.contracts, allowed_principal='admin',
                           retention_policy=RetentionPolicy('fixture-v1', 60), clock=lambda: 900.0)
    restarted_app = HTTPApp(restarted, authenticate=lambda environ: 'admin')
    restarted_client = httpx.Client(transport=httpx.WSGITransport(restarted_app), base_url='http://fixture')
    assert restarted_client.post('/v1/tasks', json=TASK).status_code == 410
    assert len(api.requests) == 1
    lifecycle_text = (service.root / '.retention' / 'lifecycle.sqlite3').read_bytes()
    assert b'Return 1+1' not in lifecycle_text and b'"answer"' not in lifecycle_text


def test_retention_preview_inventories_all_owned_prompt_copies_and_blocks_provisional_fee(tmp_path):
    client, service, runner, api, clock = retained_service(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    clock[0] = 1061.0
    service.retention_open_files = lambda path: []
    plan = service.retention_preview('admin', 'task-1')
    assert plan['eligible'] is False
    assert 'unverified_cost' in plan['blockers']
    paths = {target['path'] for target in plan['targets']}
    internal = service.internal_id('admin', 'task-1')
    assert str(service.root / internal) in paths
    assert str(runner.client.root / 'runs' / internal) in paths
    assert str(runner.client.root / 'calls' / ('run-' + internal + '-a0-t0')) in paths
    assert not any(target['path'] == str(runner.store.path) for target in plan['targets'])
    assert sum(target['bytes'] for target in plan['targets']) > 0
    assert all(target['sha256'] for target in plan['targets'])
    assert 'Return 1+1' not in json.dumps(plan) and 'fixture-private-token' not in json.dumps(plan)
    assert len(api.requests) == 1



def verified_task(tmp_path):
    from contextlib import contextmanager
    from test_engine import API
    api = API(first='2')
    def endpoint(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1', 'total_cost': '.012',
                'model': 'example/open_source', 'provider_name': 'Example'}})
        return api(request)
    endpoint.requests = api.requests
    client, service, runner, _, clock = retained_service(tmp_path, api=endpoint)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    internal = service.internal_id('admin', 'task-1')
    runner.client.reconcile('run-' + internal + '-a0-t0')
    runner.sync_client(runner.client)
    clock[0] = 1061.0
    service.retention_open_files = lambda path: []
    @contextmanager
    def maintenance(task_id):
        assert task_id == internal
        yield {'workers_stopped': True, 'evidence_ref': 'fixture-exclusive-roots'}
    service.retention_maintenance = maintenance
    return client, service, runner, api, clock


def test_exact_approved_fixture_purge_preserves_billing_and_tombstones_without_resending(tmp_path):
    from cloudyfan_jev.http_api import ServiceError
    import pytest
    client, service, runner, api, _ = verified_task(tmp_path)
    plan = service.retention_preview('admin', 'task-1')
    assert plan['eligible'] is True and plan['blockers'] == []
    with pytest.raises(ServiceError, match='retention_approval_required'):
        service.retention_apply('admin', 'task-1', plan, approval=None)
    assert (service.root / service.internal_id('admin', 'task-1') / 'job.json').exists()
    result = service.retention_apply('admin', 'task-1', plan,
        approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only-approved-targets'})
    assert result['status'] == 'retired' and result['plan_sha256'] == plan['sha256']
    for target in plan['targets']:
        for entry in target['entries']:
            if entry['kind'] == 'file':
                assert not (__import__('pathlib').Path(target['path']) / entry['path']).exists()
    assert runner.store.report()['verified_usd'] == '0.012'
    assert runner.store.path.exists()
    assert client.post('/v1/tasks', json=TASK).status_code == 410
    assert client.post('/v1/tasks/task-1/resume', json={}).status_code == 410
    assert len(api.requests) == 1
    marker = runner.client.root / 'runs' / service.internal_id('admin', 'task-1') / '.retired.json'
    assert marker.exists() and 'Return 1+1' not in marker.read_text()
    audit = service.retention.lookup(service.internal_id('admin', 'task-1'))['audit']
    assert audit['billing'][0]['receipt'] == 'gen-test-1'
    assert 'Return 1+1' not in json.dumps(audit) and '"answer"' not in json.dumps(audit)


def test_lifecycle_cannot_be_silently_disabled_or_changed_and_legacy_jobs_fail_closed(tmp_path):
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    _, service, runner, _, _ = retained_service(tmp_path / 'managed')
    service.create('admin', TASK)
    with pytest.raises(ValueError, match='Existing retention metadata'):
        TaskService(service.root, contracts=service.contracts, allowed_principal='admin')
    changed = TaskService(service.root, contracts=service.contracts, allowed_principal='admin',
                          retention_policy=RetentionPolicy('changed', 600))
    with pytest.raises(ServiceError, match='retention_policy_changed'):
        changed.execute('admin', 'task-1')
    _, legacy, _, _, _ = service_client(tmp_path / 'legacy')
    legacy.create('admin', TASK)
    enabled = TaskService(legacy.root, contracts=legacy.contracts, allowed_principal='admin',
                          retention_policy=RetentionPolicy('fixture-v1', 60))
    with pytest.raises(ServiceError, match='retention_metadata_missing'):
        enabled.execute('admin', 'task-1')
    assert runner.store.report()['events'] == []


def test_invalid_create_does_not_start_retention_clock_or_reserve_task_id(tmp_path):
    client, _, _, api, clock = retained_service(tmp_path)
    assert client.post('/v1/tasks', json={**TASK, 'contract_id': 'wrong'}).status_code == 422
    clock[0] = 1200.0
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    assert len(api.requests) == 1


def test_orphaned_or_missing_call_receipts_block_even_when_budget_rows_are_verified(tmp_path):
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    for kind in ('extra', 'missing'):
        _, service, runner, _, _ = verified_task(tmp_path / kind)
        internal = service.internal_id('admin', 'task-1')
        if kind == 'extra':
            unexpected = runner.client.root / 'calls' / ('run-' + internal + '-a99-t0')
            unexpected.mkdir()
            (unexpected / 'response.json').write_text('{}')
        else:
            original = runner.client.root / 'calls' / ('run-' + internal + '-a0-t0')
            original.rename(tmp_path / 'receipt-held-outside-targets')
        plan = service.retention_preview('admin', 'task-1')
        assert plan['eligible'] is False
        assert 'receipt_inventory_mismatch' in plan['blockers']
        with pytest.raises(ServiceError, match='retention_plan_ineligible'):
            service.retention_apply('admin', 'task-1', plan,
                approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only'})
        assert (service.root / internal / 'job.json').exists()


def test_inactive_principal_cannot_read_resume_or_create_even_with_valid_credentials(tmp_path):
    client, service, _, api, _ = retained_service(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    service.principal_active = lambda principal: False
    for method, path, payload in [('GET', '/v1/tasks/task-1', None),
        ('GET', '/v1/tasks/task-1/result', None), ('POST', '/v1/tasks/task-1/resume', {}),
        ('POST', '/v1/tasks', {**TASK, 'run_id': 'new'})]:
        assert client.request(method, path, json=payload).status_code == 401
    assert len(api.requests) == 1


def test_changed_exact_plan_and_missing_maintenance_fence_never_erase(tmp_path):
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    _, service, _, _, _ = verified_task(tmp_path)
    plan = service.retention_preview('admin', 'task-1')
    approval = {'plan_sha256': plan['sha256'], 'reference': 'fixture-only'}
    fence = service.retention_maintenance
    service.retention_maintenance = None
    with pytest.raises(ServiceError, match='retention_maintenance_required'):
        service.retention_apply('admin', 'task-1', plan, approval=approval)
    service.retention_maintenance = fence
    directory = service.root / service.internal_id('admin', 'task-1')
    late_file = directory / 'new-private-data.txt'
    late_file.write_text('new private data')
    with pytest.raises(ServiceError, match='retention_plan_changed'):
        service.retention_apply('admin', 'task-1', plan, approval=approval)
    assert late_file.read_text() == 'new private data' and (directory / 'job.json').exists()
    assert service.retention.lookup(service.internal_id('admin', 'task-1'))['state'] != 'retired'


def test_open_unknown_linked_and_protected_targets_are_ineligible(tmp_path):
    import os
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    for kind in ('open', 'unknown', 'symlink', 'hardlink', 'protected', 'overlap'):
        _, service, runner, _, _ = verified_task(tmp_path / kind)
        directory = service.root / service.internal_id('admin', 'task-1')
        if kind == 'open': service.retention_open_files = lambda path: [str(path / 'private-open')]
        if kind == 'unknown': service.retention_open_files = lambda path: None
        if kind in ('symlink', 'hardlink'):
            outside = tmp_path / (kind + '-must-survive')
            outside.write_text('unrelated private data')
            if kind == 'symlink': (directory / 'linked').symlink_to(outside)
            else: os.link(outside, directory / 'linked')
        if kind == 'protected': service.retention_protected_paths = (directory / 'job.json',)
        if kind == 'overlap':
            # A trusted operator's bad storage composition still cannot broaden a purge.
            service.retention_protected_paths = (runner.client.root / 'runs',)
        plan = service.retention_preview('admin', 'task-1')
        assert plan['eligible'] is False, kind
        with pytest.raises(ServiceError, match='retention_plan_ineligible'):
            service.retention_apply('admin', 'task-1', plan,
                approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only'})
        assert (directory / 'job.json').exists()
        if kind in ('symlink', 'hardlink'): assert outside.read_text() == 'unrelated private data'


def test_interrupted_and_unknown_accounting_preserve_all_evidence_after_expiry(tmp_path):
    from test_engine import API
    for kind in ('unknown', 'interrupt'):
        original = API(first='2')
        def endpoint(request):
            if request.method == 'GET': return httpx.Response(404, json={'error': 'missing'})
            if kind == 'interrupt': raise RuntimeError('transport interrupted')
            response = json.loads(original(request).content)
            response['usage'].pop('cost')
            return httpx.Response(200, json=response)
        client, service, runner, _, clock = retained_service(tmp_path / kind, api=endpoint)
        assert client.post('/v1/tasks', json=TASK).status_code == 409
        clock[0] = 1061.0
        service.retention_open_files = lambda path: []
        plan = service.retention_preview('admin', 'task-1')
        assert plan['eligible'] is False
        assert 'task_not_completed' in plan['blockers']
        assert 'reservation_not_complete' in plan['blockers']
        assert 'unresolved_shared_accounting' in plan['blockers']
        assert runner.store.report()['unknown_events'] == 1
        assert any((target['path'].endswith('-a0-t0')) for target in plan['targets'])
        assert client.post('/v1/tasks/task-1/resume', json={}).status_code == 410


def test_queued_task_requires_terminal_exact_request_erasure_and_failure_keeps_tombstone(tmp_path):
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    client, service, _, api, clock = verified_task(tmp_path)
    clock[0] = 1001.0
    service.mark_queued('admin', 'task-1')
    clock[0] = 1061.0
    plan = service.retention_preview('admin', 'task-1')
    assert 'queue_retention_unavailable' in plan['blockers']
    queue = {'terminal': True, 'request_erased': False, 'request_sha256': 'a' * 64,
             'bytes': 123, 'evidence_ref': 'fixture-terminal-request'}
    service.retention_guard = lambda task_id: dict(queue)
    def failed_erase(task_id, expected_sha256, approval_reference):
        assert expected_sha256 == 'a' * 64 and approval_reference == 'fixture-only'
        raise RuntimeError('interrupted before queue erase')
    service.retention_erase_queue = failed_erase
    plan = service.retention_preview('admin', 'task-1')
    assert plan['eligible'] is True
    with pytest.raises(ServiceError, match='retention_maintenance_interrupted'):
        service.retention_apply('admin', 'task-1', plan,
            approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only'})
    assert service.retention.lookup(service.internal_id('admin', 'task-1'))['state'] == 'retired'
    assert (service.root / service.internal_id('admin', 'task-1') / 'job.json').exists()
    assert client.post('/v1/tasks', json=TASK).status_code == 410
    assert len(api.requests) == 1


def test_unconfirmed_worker_stop_and_busy_writer_do_not_authorize_erasure(tmp_path):
    from contextlib import contextmanager
    import fcntl
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    _, service, _, _, _ = verified_task(tmp_path)
    plan = service.retention_preview('admin', 'task-1')
    approval = {'plan_sha256': plan['sha256'], 'reference': 'fixture-only'}
    fence = service.retention_maintenance
    @contextmanager
    def unconfirmed(task_id):
        yield {'workers_stopped': False, 'evidence_ref': 'fixture-not-stopped'}
    service.retention_maintenance = unconfirmed
    with pytest.raises(ServiceError, match='retention_workers_not_stopped'):
        service.retention_apply('admin', 'task-1', plan, approval=approval)
    service.retention_maintenance = fence
    directory = service.root / service.internal_id('admin', 'task-1')
    with (directory / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ServiceError, match='retention_writer_busy'):
            service.retention_apply('admin', 'task-1', plan, approval=approval)
        fcntl.flock(stream, fcntl.LOCK_UN)
    assert (directory / 'job.json').exists()
    assert service.retention.lookup(service.internal_id('admin', 'task-1'))['state'] != 'retired'


def test_missing_lifecycle_database_is_not_recreated_or_disabled(tmp_path):
    import pytest
    _, service, _, _, _ = retained_service(tmp_path)
    service.create('admin', TASK)
    service.retention.path.rename(tmp_path / 'saved-lifecycle.sqlite3')
    with pytest.raises(ValueError, match='Existing retention metadata'):
        TaskService(service.root, contracts=service.contracts, allowed_principal='admin')
    with pytest.raises(ValueError, match='Retention database missing'):
        TaskService(service.root, contracts=service.contracts, allowed_principal='admin',
                    retention_policy=RetentionPolicy('fixture-v1', 60))


def test_queue_guard_does_not_leak_unexpected_fields_or_accept_contradictory_erasure(tmp_path):
    _, service, _, _, clock = verified_task(tmp_path)
    clock[0] = 1001.0
    service.mark_queued('admin', 'task-1')
    clock[0] = 1061.0
    service.retention_erase_queue = lambda *args: None
    service.retention_guard = lambda task_id: {
        'terminal': True, 'request_erased': True, 'request_sha256': 'a' * 64,
        'bytes': 99, 'evidence_ref': 'fixture-request', 'prompt': 'SECRET PRIVATE QUEUE PROMPT'}
    plan = service.retention_preview('admin', 'task-1')
    assert plan['eligible'] is False
    assert 'SECRET PRIVATE QUEUE PROMPT' not in json.dumps(plan)
    assert 'queue_not_terminal_or_unverified' in plan['blockers']


def test_backward_clock_never_extends_lifecycle_or_dispatches_again_after_restart(tmp_path):
    client, service, _, api, clock = retained_service(tmp_path)
    service.create('admin', TASK)
    clock[0] = 1040.0
    assert client.get('/v1/tasks/task-1').status_code == 200
    restarted = TaskService(service.root, contracts=service.contracts, allowed_principal='admin',
                           retention_policy=RetentionPolicy('fixture-v1', 60), clock=lambda: 1020.0)
    restarted_client = httpx.Client(transport=httpx.WSGITransport(
        HTTPApp(restarted, authenticate=lambda environ: 'admin')), base_url='http://fixture')
    response = restarted_client.post('/v1/tasks/task-1/resume', json={})
    assert response.status_code == 503
    assert response.json() == {'error': {'code': 'retention_clock_rollback'}}
    assert api.requests == []


def test_unapproved_late_file_is_preserved_and_partial_purge_stays_retired(tmp_path):
    import pytest
    from cloudyfan_jev.http_api import ServiceError
    client, service, _, api, clock = verified_task(tmp_path)
    clock[0] = 1001.0
    service.mark_queued('admin', 'task-1')
    clock[0] = 1061.0
    queue = {'terminal': True, 'request_erased': False, 'request_sha256': 'b' * 64,
             'bytes': 123, 'evidence_ref': 'fixture-terminal-request'}
    service.retention_guard = lambda task_id: dict(queue)
    late = service.root / service.internal_id('admin', 'task-1') / 'unapproved.txt'
    def erase(task_id, expected_sha256, reference):
        queue.update(request_erased=True, bytes=0)
        late.write_text('must not erase an unapproved late item')
    service.retention_erase_queue = erase
    plan = service.retention_preview('admin', 'task-1')
    with pytest.raises(ServiceError, match='retention_target_changed'):
        service.retention_apply('admin', 'task-1', plan,
            approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only'})
    assert late.read_text() == 'must not erase an unapproved late item'
    assert service.retention.lookup(service.internal_id('admin', 'task-1'))['state'] == 'retired'
    assert client.post('/v1/tasks', json=TASK).status_code == 410
    assert len(api.requests) == 1


def test_platform_open_file_scan_recognizes_a_real_fixture_handle_and_keeps_own_locks(tmp_path):
    import pytest
    from cloudyfan_jev.retention import open_file_paths
    _, service, _, _, _ = verified_task(tmp_path)
    service.retention_open_files = open_file_paths
    plan = service.retention_preview('admin', 'task-1')
    if any(target['open_files'] is None for target in plan['targets']):
        pytest.skip('Platform lsof inspection is unavailable or partial; apply remains blocked')
    assert plan['eligible'] is True
    directory = service.root / service.internal_id('admin', 'task-1')
    with (directory / 'job.json').open():
        opened = service.retention_preview('admin', 'task-1')
        assert opened['eligible'] is False
        assert any(target['status'] == 'open_files' for target in opened['targets']), [(t['status'], t['open_files']) for t in opened['targets']]
    # Apply reacquires retained task/Core lock handles before its real lsof recheck.
    result = service.retention_apply('admin', 'task-1', plan,
        approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only-real-open-check'})
    assert result['status'] == 'retired'


def test_unrelated_task_creation_progresses_while_another_retention_preview_is_running(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    import pytest
    _, service, _, api, _ = verified_task(tmp_path)
    entered, release = Event(), Event()
    def held_scan(path):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), 'Fixture scan was not released'
        return []
    service.retention_open_files = held_scan
    with ThreadPoolExecutor(max_workers=2) as workers:
        preview = workers.submit(service.retention_preview, 'admin', 'task-1')
        assert entered.wait(5), 'Preview did not reach the controlled inspection seam'
        create = workers.submit(service.create, 'admin', {**TASK, 'run_id': 'independent-task'})
        try:
            try:
                result = create.result(timeout=1)
            except TimeoutError:
                pytest.fail('An unrelated task must not wait for another task retention lock')
            assert result['status'] == 'ready'
        finally:
            release.set()
        assert preview.result(timeout=5)['eligible'] is True
    assert len(api.requests) == 1


def test_terminal_queue_tasks_never_dispatched_can_purge_without_inventing_paid_evidence(tmp_path):
    import pytest
    from cloudyfan_jev.scheduler import QueueLimits, QueueScheduler
    from test_engine import ArithmeticEnvironment
    for terminal in ('cancelled', 'expired'):
        client, service, runner, api, clock = retained_service(tmp_path / terminal)
        queue = QueueScheduler(service, tmp_path / terminal / 'queue.sqlite3',
            limits=QueueLimits(4, 1, 4, 10), clock=lambda: clock[0], measurement_kind='synthetic')
        assert queue.submit('admin', TASK)['queue_state'] == 'queued'
        if terminal == 'cancelled':
            clock[0] = 1001.0
            assert queue.cancel('admin', 'task-1')['queue_state'] == 'cancelled'
        else:
            clock[0] = 1011.0
            assert queue.status('admin', 'task-1')['queue_state'] == 'expired'
        internal = service.internal_id('admin', 'task-1')
        core_run = runner.client.root / 'runs' / internal
        assert not core_run.exists() and runner.store.report()['events'] == []
        clock[0] = 1061.0
        service.retention_open_files = lambda path: []
        plan = service.retention_preview('admin', 'task-1')
        assert plan['eligible'] is True and plan['never_dispatched'] is True
        assert plan['billing'] == []
        assert {target['path'] for target in plan['targets']} == {str(service.root / internal)}
        service.retention_apply('admin', 'task-1', plan,
            approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-never-dispatched'})
        assert (core_run / '.retired.json').exists()
        assert queue.path.exists() and queue.retention_snapshot(internal)['request_erased'] is True
        assert queue.retention_snapshot(internal)['bytes'] == 0
        assert runner.store.report()['events'] == [] and api.requests == []
        audit = service.retention.lookup(internal)['audit']
        assert audit['never_dispatched'] is True and audit['verified_api_usd'] == '0'
        assert audit['billing'] == [] and 'Return 1+1' not in json.dumps(audit)
        assert client.post('/v1/tasks', json=TASK).status_code == 410
        with pytest.raises(ValueError, match='Retired run'):
            runner.core.run(internal, {'prompt': TASK['prompt']}, ArithmeticEnvironment,
                            contract_version='public-v1')
        assert api.requests == []


def test_unstarted_retention_never_treats_orphan_artifacts_or_pending_accounting_as_zero(tmp_path):
    from cloudyfan_jev.http_api import ServiceError
    import pytest
    for evidence in ('core', 'call', 'reservation', 'shared_unknown', 'unsafe_namespace'):
        _, service, runner, api, clock = retained_service(tmp_path / evidence)
        service.create('admin', TASK)
        internal = service.internal_id('admin', 'task-1')
        if evidence == 'core':
            (runner.client.root / 'runs' / internal).mkdir(parents=True)
        if evidence == 'call':
            call = runner.client.root / 'calls' / ('run-' + internal + '-a0-t0')
            call.mkdir()
            (call / 'intent.json').write_text('{}')
        if evidence == 'reservation':
            runner.store.reserve(internal, 'fixture-reserved-but-not-finished', '.1')
        if evidence == 'shared_unknown':
            runner.store.import_history('fixture-unresolved-history', cost_usd=None)
        if evidence == 'unsafe_namespace':
            calls = runner.client.root / 'calls'
            calls.rename(runner.client.root / 'held-empty-calls')
            outside = tmp_path / evidence / 'unmanaged-empty-calls'
            outside.mkdir()
            calls.symlink_to(outside)
        clock[0] = 1061.0
        service.retention_open_files = lambda path: []
        plan = service.retention_preview('admin', 'task-1')
        assert plan['eligible'] is False, evidence
        if evidence in ('core', 'call'):
            assert plan['never_dispatched'] is False
            assert 'never_dispatched_evidence_conflict' in plan['blockers']
        if evidence == 'reservation':
            assert plan['never_dispatched'] is False
            assert 'reservation_not_complete' in plan['blockers']
        if evidence == 'shared_unknown':
            assert 'unresolved_shared_accounting' in plan['blockers']
        if evidence == 'unsafe_namespace':
            assert plan['never_dispatched'] is False
            assert 'unsafe_never_dispatched_namespace' in plan['blockers']
        assert (service.root / internal / 'job.json').exists() and api.requests == []
        with pytest.raises(ServiceError, match='retention_maintenance_required'):
            service.retention_apply('admin', 'task-1', plan,
                approval={'plan_sha256': plan['sha256'], 'reference': 'fixture-only'})
