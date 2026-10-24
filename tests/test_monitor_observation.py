"""Read-only task observation never waits for unrelated upstream model work."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from cloudyfan_jev.http_api import ServiceError
from test_engine import API
from test_http_api import TASK
from test_retention import retained_service


def test_completed_task_observation_does_not_wait_for_another_task_upstream(tmp_path):
    entered, release = Event(), Event()
    upstream = API(first='2')
    dispatched = []

    def endpoint(request):
        dispatched.append(request.method)
        if len(dispatched) == 2:
            entered.set()
            assert release.wait(5), 'Controlled mock upstream was not released'
        return upstream(request)

    _, service, _, _, _ = retained_service(tmp_path, api=endpoint)
    service.create('admin', TASK)
    assert service.execute('admin', TASK['run_id'])['status'] == 'passed'
    service.create('admin', {**TASK, 'run_id': 'task-2'})
    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(service.execute, 'admin', 'task-2')
        try:
            assert entered.wait(2), 'Second task did not reach the mock upstream'
            observed = workers.submit(service.result, 'admin', TASK['run_id'], refresh=False).result(timeout=1)
            assert observed['status'] == 'passed' and observed['answer'] == '2'
            assert observed['costs']['complete_api_usd'] == '0.01'
            assert dispatched == ['POST', 'POST']
        finally:
            release.set()
        assert running.result(timeout=2)['status'] == 'passed'
    assert service.result('admin', TASK['run_id'])['costs']['complete_api_usd'] == '0.01'


def reconciled_task(tmp_path, *, model='example/open_source'):
    upstream = API(first='2')
    methods = []

    def endpoint(request):
        methods.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1', 'total_cost': '.012',
                'model': model, 'provider_name': 'Example'}})
        return upstream(request)

    _, service, runner, _, _ = retained_service(tmp_path, api=endpoint)
    service.create('admin', TASK)
    assert service.execute('admin', TASK['run_id'])['status'] == 'passed'
    runner.client.reconcile('run-' + service.internal_id('admin', TASK['run_id']) + '-a0-t0')
    return service, methods


def test_observation_reads_existing_accounting_and_default_result_still_refreshes(tmp_path):
    service, methods = reconciled_task(tmp_path)
    observed = service.result('admin', TASK['run_id'], refresh=False)
    assert observed['costs']['complete_api_usd'] == '0.01'
    refreshed = service.result('admin', TASK['run_id'])
    assert refreshed['costs']['complete_api_usd'] == '0.012'
    assert methods == ['POST', 'GET']


def test_observation_still_rejects_mismatched_reconciled_identity(tmp_path):
    service, methods = reconciled_task(tmp_path, model='wrong/model')
    with pytest.raises(ServiceError, match='result_evidence_unavailable'):
        service.result('admin', TASK['run_id'], refresh=False)
    assert methods == ['POST', 'GET']


@pytest.mark.parametrize('refresh', [None, 0, 1, 'false', {}])
def test_result_refresh_requires_an_explicit_bool(tmp_path, refresh):
    _, service, _, _, _ = retained_service(tmp_path)
    service.create('admin', TASK)
    service.execute('admin', TASK['run_id'])
    with pytest.raises(ValueError, match='refresh must be bool'):
        service.result('admin', TASK['run_id'], refresh=refresh)
