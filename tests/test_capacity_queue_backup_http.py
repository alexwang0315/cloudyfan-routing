"""Original category backup MRC, including real classification and continuation."""
from datetime import timedelta
import hashlib

import pytest

from test_history_observation import history_service as history_service
from test_local_http_integration import loopback_network as loopback_network
from capacity_queue_fixture import attach_queue


@pytest.mark.parametrize('history_service', [{'classifiers': ('low_mid', 'mid_high')}], indirect=True)
def test_actual_backup_category_waits_after_generation_and_classifier_without_resetting_policy(history_service):
    from cloudyfan_jev.scheduler import QueueLimits
    from capacity_sources_fixture import limit
    host = history_service.bundle
    host.scenario = 'quality_chain'
    clock = lambda: host.values['now'].timestamp()
    now = clock()
    observations = [limit('rpm', 1, now), limit('tpm', None, now, status='not_applicable'), limit('concurrency', 1, now)]
    observations[0].update(window_seconds=1, reset_at=now+1)
    config = {'schema_version': 1, 'version': 'synthetic-admission-config-v1',
        'bindings': [{'id': 'synthetic-binding', 'provider': 'openrouter', 'account_ref': 'synthetic-backup-account',
            'credential_ref': 'synthetic-backup-key', 'endpoint_ref': 'chat-and-classifier', 'groups': ['shared']}],
        'groups': [{'id': 'shared', 'scope': {'kind': 'account', 'ref': 'synthetic-backup-account'},
            'source': {'kind': 'controlled_import', 'version': 'synthetic-reviewed-v1'},
            'supplier_limits': [], 'local_limits': observations}]}
    queue = attach_queue(host, clock, config=config, seconds=90, limits=QueueLimits(8, 2, 4, 90))
    history_service.server.set_app(host.app)
    operator, client = history_service.client('operator'), history_service.client()
    operator.post('/monitor/capacity/activate', json={'revision': 0, 'version': 'synthetic-queued-admission-v1'})
    payload = {'run_id': 'queued-real-backup', 'category': host.values['plan']['category_id'], 'prompt': 'Return only 1+1.'}
    assert client.post('/v1/tasks', json=payload).status_code == 202
    first_response = None
    for index in range(5):
        state = queue.dispatch_next()
        assert len(host.calls) == index + 1, state
        assert state['status'] == ('passed' if index == 4 else 'queued'), state
        if index == 0:
            first_response = next((host.root / 'receipts/calls').glob('*/response.json'))
            original_hash = hashlib.sha256(first_response.read_bytes()).hexdigest()
        if index < 4:
            assert queue.dispatch_next() is None
            waiting = client.get('/v1/tasks/queued-real-backup').json()
            assert waiting['capacity_reason'] == 'capacity_rpm_exhausted'
            assert len(host.calls) == index + 1
            host.values['now'] += timedelta(seconds=2)
    result = client.get('/v1/tasks/queued-real-backup/result').json()
    assert len(result['provenance']['attempts']) == 3
    assert len(result['provenance']['classifier_calls']) == 2
    assert result['costs']['complete_api_usd'] == '0.032'
    assert hashlib.sha256(first_response.read_bytes()).hexdigest() == original_hash
    assert all(request['method'] == 'POST' for request in host.requests)
    report = host.budget.report()
    assert len(report['events']) == 5 and len(report['reservations']) == 1 and report['reserved_usd'] == '0'
