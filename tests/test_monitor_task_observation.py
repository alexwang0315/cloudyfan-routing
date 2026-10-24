"""Host observation retains the real task/receipt guards without duplicate reads."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import httpx
import pytest

from cloudyfan_jev.http_api import ServiceError
from cloudyfan_jev.identity import IdentityStore
from test_http_api import TASK
from test_engine import API
from test_monitor_observation import reconciled_task
from test_retention import retained_service


def test_host_observes_ready_and_verified_completed_tasks_without_dispatching(tmp_path):
    _, service, _, upstream, _ = retained_service(tmp_path)
    service.create('admin', TASK)
    ready = service.observe('admin', TASK['run_id'])
    assert ready['state']['status'] == 'ready' and ready['completed_result'] is None
    assert upstream.requests == []
    assert service.execute('admin', TASK['run_id'])['status'] == 'passed'
    observed = service.observe('admin', TASK['run_id'])
    assert observed['state']['status'] == 'passed'
    assert observed['state']['costs']['complete_api_usd'] == '0.01'
    assert observed['completed_result']['final_public_pass'] is True
    assert observed['completed_result']['attempts'][0]['tier'] == 'open_source'
    assert len(upstream.requests) == 1


def test_second_principal_check_clock_failure_is_unavailable_not_inactive(tmp_path):
    _, service, _, upstream, _ = retained_service(tmp_path / 'service')
    service.create('admin', TASK)
    clock = [1000.0]
    identity = IdentityStore(tmp_path / 'identity.sqlite', pepper=b'synthetic-observation-pepper-only-0000',
                             clock=lambda: clock[0])
    identity.create_principal('admin')
    checks = []
    def active(principal):
        checks.append(principal)
        if len(checks) == 2:
            clock[0] = 999.0
        return identity.is_active(principal)
    service.principal_active = active
    with pytest.raises(ServiceError) as caught:
        service.observe('admin', TASK['run_id'])
    assert caught.value.status == 503 and caught.value.code == 'principal_authorization_unavailable'
    assert len(checks) == 2 and upstream.requests == []


@pytest.mark.parametrize('case,code,status', [('expiry', 'task_expired', 410),
    ('contract', 'frozen_configuration_changed', 409), ('inactive', 'unauthorized', 401)])
def test_host_observation_retains_expiry_contract_and_principal_guards(tmp_path, case, code, status):
    _, service, _, upstream, clock = retained_service(tmp_path)
    service.create('admin', TASK)
    service.execute('admin', TASK['run_id'])
    if case == 'expiry':
        clock[0] = 1060.0
    elif case == 'contract':
        service.contracts['arithmetic'] = replace(service.contracts['arithmetic'], version='changed-public-v2')
    else:
        service.principal_active = lambda _: False
    with pytest.raises(ServiceError) as caught:
        service.observe('admin', TASK['run_id'])
    assert caught.value.code == code and caught.value.status == status
    assert len(upstream.requests) == 1


def test_host_observation_revalidates_reconciled_receipt_identity_without_network(tmp_path):
    service, methods = reconciled_task(tmp_path, model='wrong/model')
    with pytest.raises(ServiceError) as caught:
        service.observe('admin', TASK['run_id'])
    assert caught.value.code == 'result_evidence_unavailable' and caught.value.status == 503
    assert methods == ['POST', 'GET']


def test_host_observation_retains_unknown_fees_without_reconciliation_or_success(tmp_path):
    methods, upstream = [], API(first='2')
    def endpoint(request):
        methods.append(request.method)
        if request.method == 'GET':
            return httpx.Response(404)
        body = upstream(request).json()
        body['usage'].pop('cost')
        return httpx.Response(200, json=body)
    _, service, _, _, _ = retained_service(tmp_path, api=endpoint)
    service.create('admin', TASK)
    assert service.execute('admin', TASK['run_id'])['status'] == 'accounting_interrupted'
    before = methods[:]
    observed = service.observe('admin', TASK['run_id'])
    assert observed['state']['status'] == 'accounting_interrupted' and observed['completed_result'] is None
    assert observed['state']['costs']['complete_api_usd'] is None
    assert observed['state']['costs']['unknown_events'] == 1
    assert methods == before


def test_host_observation_of_running_task_stays_nonblocking_and_never_dispatches(tmp_path):
    entered, release = Event(), Event()
    upstream = API(first='2')
    def endpoint(request):
        entered.set()
        assert release.wait(5)
        return upstream(request)
    _, service, _, _, _ = retained_service(tmp_path, api=endpoint)
    service.create('admin', TASK)
    with ThreadPoolExecutor(max_workers=2) as workers:
        running = workers.submit(service.execute, 'admin', TASK['run_id'])
        try:
            assert entered.wait(2)
            observing = workers.submit(service.observe, 'admin', TASK['run_id'])
            with pytest.raises(ServiceError) as caught:
                observing.result(timeout=1)
            assert caught.value.code == 'task_busy' and caught.value.status == 409
        finally:
            release.set()
        assert running.result(timeout=2)['status'] == 'passed'
    assert len(upstream.requests) == 1
