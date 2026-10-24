"""Resilience at real authenticated loopback HTTP; upstream remains MockTransport."""
import hashlib

import httpx

from capacity_resilience_fixture import copied_database_fault, reopen_windows
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
