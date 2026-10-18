"""RG-04/RG-05 composition through the real service and transport boundaries."""

import httpx
import pytest

from cloudyfan_jev.http_api import TaskService, ServiceError
from cloudyfan_jev.identity import IdentityStore, BearerAuthenticator
from cloudyfan_jev.retention import RetentionPolicy
from cloudyfan_jev.scheduler import QueueScheduler, QueueHTTPApp, QueueLimits
from test_http_api import service_client, TASK
from test_engine import API, ArithmeticEnvironment


def system(tmp_path):
    clock = [1000.0]
    api = API(first='2')
    def endpoint(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': request.url.params['id'], 'total_cost': '.012',
                'model': 'example/open_source', 'provider_name': 'Example'}})
        return api(request)
    _, original, runner, _, _ = service_client(tmp_path / 'managed', api=endpoint)
    identity = IdentityStore(tmp_path / 'identity.sqlite', pepper=b'offline-pepper-not-real-secret' * 2,
                             clock=lambda: clock[0])
    identity.create_principal('admin')
    credential = identity.issue('admin', scopes={'tasks:read', 'tasks:write'}, expires_at=10000)
    service = TaskService(original.root, contracts=original.contracts, allowed_principal='admin',
        principal_active=identity.is_active, retention_policy=RetentionPolicy('fixture-60s', 60),
        clock=lambda: clock[0], retention_protected_paths=(identity.path,))
    service.retention_open_files = lambda path: []  # disposable fixture, no external writers
    queue = QueueScheduler(service, tmp_path / 'queue.sqlite', limits=QueueLimits(4, 1, 2, 300),
                           clock=lambda: clock[0], measurement_kind='synthetic')
    client = httpx.Client(transport=httpx.WSGITransport(QueueHTTPApp(queue,
        authenticate=BearerAuthenticator(identity))), base_url='https://fixture',
        headers={'Authorization': 'Bearer ' + credential.token})
    return clock, api, runner, identity, service, queue, client


def test_disabled_account_stops_accepted_queue_job_before_model_dispatch(tmp_path):
    clock, api, runner, identity, service, queue, client = system(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 202
    identity.set_active('admin', False)
    with pytest.raises(ServiceError, match='unauthorized'):
        queue.dispatch_next()
    assert api.requests == []
    assert runner.store.report()['known_usd'] == '0'
    assert client.get('/v1/tasks/task-1').status_code == 401


def test_approved_retention_erases_task_and_queue_prompt_but_keeps_billing_and_no_reuse(tmp_path):
    clock, api, runner, identity, service, queue, client = system(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 202
    assert queue.dispatch_next()['status'] == 'passed'
    internal = service.internal_id('admin', 'task-1')
    runner.client.reconcile('run-' + internal + '-a0-t0')
    runner.sync_client(runner.client)
    clock[0] += 61
    preview = service.retention_preview('admin', 'task-1')
    assert preview['eligible'] is True
    assert preview['queue']['request_erased'] is False
    approved = service.retention_apply('admin', 'task-1', preview,
        approval={'plan_sha256': preview['sha256'], 'reference': 'fixture:exact-approved-plan'})
    assert approved['status'] == 'retired'
    assert runner.store.report()['verified_usd'] == '0.012'
    assert identity.is_active('admin') is True
    assert client.get('/v1/tasks/task-1/result').status_code == 410
    assert client.post('/v1/tasks', json=TASK).status_code == 410
    assert queue.retention_snapshot(internal)['request_erased'] is True
    assert TASK['prompt'].encode() not in queue.path.read_bytes()
    assert not list((runner.client.root / 'calls').glob('run-' + internal + '-*'))
    assert not (service.root / internal / 'job.json').exists()
    assert (runner.client.root / 'runs' / internal / '.retired.json').is_file()
    with pytest.raises(ValueError, match='Retired run'):
        runner.core.run(internal, {'prompt': TASK['prompt']}, ArithmeticEnvironment, contract_version='public-v1')
    assert len(api.requests) == 1


def test_queue_maintenance_fences_another_scheduler_and_preserves_rejected_prompt(tmp_path):
    clock, api, runner, identity, service, queue, client = system(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 202
    other = QueueScheduler(service, queue.path, limits=queue.limits,
                           clock=lambda: clock[0], measurement_kind='synthetic')
    with queue.retention_maintenance('operator-inventory') as fence:
        assert fence['workers_stopped'] is True
        assert other.dispatch_next() is None
        response = client.post('/v1/tasks', json={**TASK, 'run_id': 'blocked'})
        assert response.status_code == 503
    assert queue.dispatch_next()['status'] == 'passed'
    assert len(api.requests) == 1


def test_pending_or_changed_queue_copy_cannot_be_erased(tmp_path):
    clock, api, runner, identity, service, queue, client = system(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 202
    internal = service.internal_id('admin', 'task-1')
    snapshot = queue.retention_snapshot(internal)
    with pytest.raises(ServiceError, match='not_terminal'):
        queue.retention_erase(internal, snapshot['request_sha256'], 'fixture:approval')
    assert queue.dispatch_next()['status'] == 'passed'
    with pytest.raises(ServiceError, match='changed'):
        queue.retention_erase(internal, '0' * 64, 'fixture:approval')
    assert queue.retention_snapshot(internal)['request_erased'] is False
    assert len(api.requests) == 1


def test_retention_protects_shared_queue_database_even_with_bad_storage_layout(tmp_path):
    from test_retention import verified_task
    client, service, runner, api, clock = verified_task(tmp_path)
    internal = service.internal_id('admin', 'task-1')
    queue_path = service.root / internal / 'shared-queue.sqlite'
    QueueScheduler(service, queue_path, limits=QueueLimits(4, 1, 2, 300),
                   clock=lambda: clock[0], measurement_kind='synthetic')
    plan = service.retention_preview('admin', 'task-1')
    assert plan['eligible'] is False
    assert 'protected_database_overlap' in plan['blockers']
    assert queue_path.is_file()
    assert runner.store.report()['verified_usd'] == '0.012'


def test_cancelled_never_dispatched_queue_task_can_expire_and_purge_with_zero_fees(tmp_path):
    clock, api, runner, identity, service, queue, client = system(tmp_path)
    assert client.post('/v1/tasks', json=TASK).status_code == 202
    assert client.post('/v1/tasks/task-1/cancel', json={}).json()['status'] == 'cancelled'
    clock[0] += 61
    plan = service.retention_preview('admin', 'task-1')
    assert plan['eligible'] is True and plan['never_dispatched'] is True
    service.retention_apply('admin', 'task-1', plan,
        approval={'plan_sha256': plan['sha256'], 'reference': 'fixture:cancelled-never-dispatched'})
    internal = service.internal_id('admin', 'task-1')
    assert queue.retention_snapshot(internal)['request_erased'] is True
    assert runner.store.report()['known_usd'] == '0'
    assert runner.store.report()['events'] == []
    assert client.post('/v1/tasks', json=TASK).status_code == 410
    with pytest.raises(ValueError, match='Retired run'):
        runner.core.run(internal, {'prompt': TASK['prompt']}, ArithmeticEnvironment, contract_version='public-v1')
    assert api.requests == []
