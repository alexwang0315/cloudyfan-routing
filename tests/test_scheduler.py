
import httpx
import pytest

from cloudyfan_jev.http_api import RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.scheduler import QueueLimits, QueueScheduler, QueueHTTPApp
from test_budget import controlled
from test_engine import API, ArithmeticEnvironment


class Clock:
    value = 1000.0
    def __call__(self): return self.value
    def advance(self, value): self.value += value


def scheduler(tmp_path, *, api=None, limits=None, clock=None):
    api = api or API(first='2')
    runner = controlled(tmp_path / 'runner', api, total='10')
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture', classifier='cloud:typesafe')
    for profile in runner.core.profiles.values(): profile['execution_location'] = 'cloud:fixture'
    workspaces = []
    def factory(workspace):
        workspaces.append(workspace)
        return ArithmeticEnvironment()
    service = TaskService(tmp_path / 'service', contracts={'sum': RegisteredContract('sum-v1', runner, factory)},
                          allowed_principals={'alice', 'bob'})
    queue = QueueScheduler(service, tmp_path / 'queue.sqlite', limits=limits or QueueLimits(4, 1, 2, 30), clock=clock or Clock(), measurement_kind='synthetic')
    app = QueueHTTPApp(queue, authenticate=lambda env: {'Bearer A': 'alice', 'Bearer B': 'bob'}.get(env.get('HTTP_AUTHORIZATION')))
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture')
    return queue, client, runner, api, workspaces


def post(client, owner, run='same'):
    return client.post('/v1/tasks', headers={'Authorization': 'Bearer ' + owner},
                       json={'run_id': run, 'contract_id': 'sum', 'prompt': 'Return 1+1'})


def test_authenticated_queue_owns_namespaces_and_does_not_dispatch_on_submit(tmp_path):
    queue, client, runner, api, workspaces = scheduler(tmp_path)
    assert post(client, 'A').json()['status'] == 'queued'
    assert post(client, 'B').json()['status'] == 'queued'
    assert len(api.requests) == 0 and runner.store.report()['reserved_usd'] == '0'
    assert queue.dispatch_next()['status'] == 'passed'
    assert queue.dispatch_next()['status'] == 'passed'
    alice = client.get('/v1/tasks/same/result', headers={'Authorization': 'Bearer A'})
    bob = client.get('/v1/tasks/same/result', headers={'Authorization': 'Bearer B'})
    assert alice.json()['answer'] == bob.json()['answer'] == '2'
    assert alice.json()['costs']['complete_api_usd'] == bob.json()['costs']['complete_api_usd'] == '0.01'
    assert workspaces[0].parent != workspaces[1].parent
    assert 'shared' not in alice.text and 'receipt' not in alice.text
    assert post(client, 'A', 'alice-only').status_code == 202
    assert client.get('/v1/tasks/alice-only', headers={'Authorization': 'Bearer B'}).status_code == 404
    assert client.get('/v1/tasks/same', headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_capacity_quota_and_deadline_deny_before_paid_dispatch(tmp_path):
    clock = Clock()
    queue, client, runner, api, _ = scheduler(tmp_path, limits=QueueLimits(2, 1, 1, 5), clock=clock)
    assert post(client, 'A', 'a1').status_code == 202
    over_quota = post(client, 'A', 'a2')
    assert over_quota.status_code == 429 and over_quota.json()['error']['code'] == 'principal_quota_full'
    assert post(client, 'B', 'b1').status_code == 202
    full = post(client, 'B', 'b2')
    assert full.status_code == 429 and full.json()['error']['code'] == 'queue_full'
    clock.advance(5)
    assert queue.dispatch_next() is None
    assert queue.status('alice', 'a1')['reason'] == 'queue_deadline_expired'
    assert queue.status('bob', 'b1')['status'] == 'expired'
    assert api.requests == [] and runner.store.report()['events'] == []
    assert [row['reason'] for row in queue.admission_history('alice')] == ['principal_quota_full']
    assert [row['reason'] for row in queue.admission_history('bob')] == ['queue_full']


def test_queue_cancel_is_free_and_dispatched_cancel_preserves_unknown_fee(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    started, release = Event(), Event()
    def delayed(req):
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json={'model': 'example/open_source', 'usage': {}})
    queue, client, runner, _, _ = scheduler(tmp_path, api=delayed)
    post(client, 'A', 'cancel-queued')
    response = client.post('/v1/tasks/cancel-queued/cancel', headers={'Authorization': 'Bearer A'}, json={})
    assert response.json()['status'] == 'cancelled'
    assert response.json()['costs']['complete_api_usd'] == '0'
    assert runner.store.report()['events'] == []
    post(client, 'A', 'cancel-running')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(queue.dispatch_next)
        assert started.wait(5)
        response = client.post('/v1/tasks/cancel-running/cancel', headers={'Authorization': 'Bearer A'}, json={})
        assert response.json()['cancellation_requested'] is True
        assert response.json()['provider_cancellation_confirmed'] is False
        assert response.json()['costs']['complete_api_usd'] is None
        release.set()
        result = future.result(5)
    assert result['status'] == 'accounting_interrupted'
    assert result['costs']['unknown_events'] == 1
    assert runner.store.report()['reserved_usd'] != '0'
    internal = queue.service.internal_id('alice', 'cancel-running')
    receipt = runner.client.root / 'calls' / f'run-{internal}-a0-t0'
    assert (receipt / 'intent.json').exists() and (receipt / 'response.json').exists()


def test_concurrency_slot_is_atomic_while_other_caller_queues(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    started, release = Event(), Event()
    api = API(first='2')
    def delayed(req):
        started.set()
        assert release.wait(5)
        return api(req)
    queue, client, runner, _, _ = scheduler(tmp_path, api=delayed)
    post(client, 'A', 'first'); post(client, 'B', 'second')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(queue.dispatch_next)
        assert started.wait(5)
        assert pool.submit(queue.dispatch_next).result(5) is None
        assert queue.status('bob', 'second')['status'] == 'queued'
        release.set()
        assert first.result(5)['status'] == 'passed'
    assert queue.dispatch_next()['status'] == 'passed'
    assert len(api.requests) == 2


def test_restart_preserves_pending_reservation_and_never_replays(tmp_path):
    class SimulatedProcessDeath(BaseException): pass
    calls = []
    def interrupted(req):
        calls.append(req)
        raise SimulatedProcessDeath()
    queue, client, runner, _, _ = scheduler(tmp_path, api=interrupted)
    post(client, 'A', 'crash')
    with pytest.raises(SimulatedProcessDeath): queue.dispatch_next()
    before = runner.store.report()
    reopened = QueueScheduler(queue.service, queue.path, limits=queue.limits, clock=queue.clock, measurement_kind=queue.measurement_kind)
    assert reopened.dispatch_next() is None
    assert reopened.status('alice', 'crash')['status'] == 'running'
    assert reopened.recover_inflight(workers_stopped=True) == 1
    assert reopened.status('alice', 'crash')['status'] == 'interrupted_pending'
    assert reopened.dispatch_next() is None
    assert runner.store.report()['reserved_usd'] == before['reserved_usd']
    assert runner.store.report()['unknown_events'] == 1
    assert len(calls) == 1
    assert client.post('/v1/tasks/crash/resume', headers={'Authorization': 'Bearer A'}, json={}).status_code == 404


def test_fixed_synthetic_load_reports_wait_and_end_to_end_separately(tmp_path):
    clock = Clock()
    api = API(first='2')
    def timed(req):
        clock.advance(2)
        return api(req)
    queue, client, _, _, _ = scheduler(tmp_path, api=timed, clock=clock)
    post(client, 'A', 'one'); post(client, 'B', 'two')
    clock.advance(3)
    first = queue.dispatch_next()
    second = queue.dispatch_next()
    assert (first['queue_seconds'], first['end_to_end_seconds']) == (3, 5)
    assert (second['queue_seconds'], second['end_to_end_seconds']) == (5, 7)
    assert first['measurement_kind'] == second['measurement_kind'] == 'synthetic'
    assert first['production_slo_validated'] is False


def test_concurrent_admission_capacity_cannot_be_oversubscribed(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    queue, client, _, api, _ = scheduler(tmp_path, limits=QueueLimits(1, 1, 1, 30))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda owner: post(client, owner, owner), ['A', 'B']))
    assert sorted(response.status_code for response in responses) == [202, 429]
    assert api.requests == []


def test_same_owner_run_collision_rejects_changed_task_and_cross_paths(tmp_path):
    queue, client, _, api, _ = scheduler(tmp_path)
    assert post(client, 'A', 'fixed').status_code == 202
    changed = client.post('/v1/tasks', headers={'Authorization': 'Bearer A'},
                          json={'run_id': 'fixed', 'contract_id': 'sum', 'prompt': 'Another task'})
    assert changed.status_code == 409
    for path in ('/receipts', '/workspace', '/v1/tasks/fixed/result'):
        assert client.get(path, headers={'Authorization': 'Bearer B'}).status_code == 404
    assert api.requests == []


def test_backward_clock_and_changed_registered_config_stop_before_paid_call(tmp_path):
    from cloudyfan_jev.http_api import ServiceError
    clock = Clock()
    queue, client, _, api, _ = scheduler(tmp_path, clock=clock)
    post(client, 'A', 'clock')
    clock.advance(-1)
    with pytest.raises(ServiceError, match='queue_clock_invalid'):
        queue.dispatch_next()
    assert api.requests == []
    clock.advance(2)
    queue.service.contracts['sum'].runner.core.profiles['high']['effort'] = 'high'
    assert queue.dispatch_next()['status'] == 'interrupted_pending'
    assert queue.status('alice', 'clock')['reason'] == 'frozen_configuration_changed'
    assert api.requests == []


@pytest.fixture(autouse=True)
def forbid_real_sockets(monkeypatch):
    import socket
    def blocked(*args, **kwargs): pytest.fail('Scheduler tests must not open sockets')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


def test_authorization_expiry_at_dispatch_keeps_queued_admission_unpaid(tmp_path):
    from datetime import datetime, timezone
    queue, client, runner, api, _ = scheduler(tmp_path)
    assert post(client, 'A', 'expire-auth').status_code == 202
    runner.store.clock = lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)
    result = queue.dispatch_next()
    assert result['status'] == 'policy_denied'
    assert result['costs']['complete_api_usd'] == '0'
    assert api.requests == []


@pytest.mark.parametrize('values', [(0, 1, 1, 1), (1, True, 1, 1), (1, 1, 0, 1), (1, 1, 1, float('nan'))])
def test_queue_bounds_require_explicit_valid_values(values):
    with pytest.raises(ValueError): QueueLimits(*values)


def test_restart_rejects_relaxed_queue_limits(tmp_path):
    queue, _, _, _, _ = scheduler(tmp_path)
    with pytest.raises(ValueError, match='Frozen queue configuration changed'):
        QueueScheduler(queue.service, queue.path, limits=QueueLimits(100, 100, 100, 100), clock=queue.clock,
                       measurement_kind=queue.measurement_kind)


@pytest.mark.parametrize('first,tiers,count', [('2', ['open_source'], 1),
                                             ('3', ['open_source', 'high'], 4)])
def test_queued_result_propagates_task_owned_provenance(tmp_path, first, tiers, count):
    queue, client, runner, api, _ = scheduler(tmp_path, api=API(first=first, choices=('mid', 'high')))
    assert post(client, 'A', 'traceable').status_code == 202
    assert queue.dispatch_next()['status'] == 'passed'
    result = client.get('/v1/tasks/traceable/result', headers={'Authorization': 'Bearer A'})
    provenance = result.json()['provenance']
    assert [attempt['tier'] for attempt in provenance['attempts']] == tiers
    assert provenance['attempts'][-1]['calls'][0]['actual_model'] == 'example/' + tiers[-1]
    assert client.get('/v1/tasks/traceable/result', headers={'Authorization': 'Bearer A'}).json()['provenance'] == provenance
    assert client.get('/v1/tasks/traceable/result', headers={'Authorization': 'Bearer B'}).status_code == 404
    assert len(api.requests) == count
