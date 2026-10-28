"""Safe retained history after synchronous creation with either legal clock type."""
import io
import json
import math
import os
from pathlib import Path

import pytest

from cloudyfan_monitor.integration import build_local_integration
from task_features_fixture import ArtifactRecorder
from test_local_http_integration import (TASK, local_service as local_service,
                                         loopback_network as loopback_network)


@pytest.mark.parametrize('clock_type', [int, float], ids=['integer-clock', 'float-clock'])
def test_nonqueued_retained_history_survives_reopen_and_access_expiry(local_service, tmp_path, monkeypatch, clock_type):
    local = local_service
    bundle = local.bundle
    local.clock.now = clock_type(math.ceil(local.clock.now))
    accepted_at = local.clock()
    bundle.upstream.fail_tiers = set()
    destination = Path(os.environ.get('CLOUDYFAN_MONITOR_EVIDENCE_DIR', tmp_path / 'evidence'))
    directory = destination / ('retained-history-' + clock_type.__name__)
    directory.mkdir(parents=True, mode=0o700)
    http, wire = ArtifactRecorder(directory), ArtifactRecorder(directory, 'upstream')
    operator, client = local.client('operator'), local.client()

    def record(response):
        response.read()
        http.capture({'method': response.request.method, 'path': response.request.url.path,
                      'port': response.request.url.port, 'status': response.status_code,
                      'authorization_recorded': False},
                     {'request_body': response.request.content, 'response_body': response.content})

    for caller in (operator, client):
        caller.event_hooks['response'].append(record)
    bundle.upstream.before_response = lambda request: wire.capture(
        {'method': request.method, 'path': request.url.path}, {'body': request.content})
    opened = []
    try:
        policy = operator.get('/monitor/observation-policy').json()
        active = operator.post('/monitor/observation-policy/activate', json={
            'revision': policy['revision'], 'version': 'features-v2-advice-v1'})
        assert active.status_code == 200
        payload = {**TASK, 'run_id': 'retained-clock',
                   'prompt': 'PRIVATE_RETAINED_CLOCK_CANARY Return only the integer sum of 1 and 1.'}
        completed = client.post('/v1/tasks', json=payload)
        assert completed.status_code == 200 and completed.json()['status'] == 'passed'
        features, recommendation = completed.json()['task_features'], completed.json()['routing_recommendation']
        assert features['status'] == 'available'
        original = operator.get('/monitor/snapshot')
        assert original.status_code == 200
        initial = original.json()
        ledger = initial['ledger']
        assert len(ledger['rows']) == 1
        assert ledger['rows'][0]['provider'] == 'Example'
        assert ledger['rows'][0]['model']
        assert ledger['rows'][0]['tokens'] == {'prompt_tokens': 20, 'completion_tokens': 1}
        task = next(row for row in initial['tasks'] if row['run'] == payload['run_id'])
        binding = task['operator_observation']['task_decision']['binding']
        assert binding['contract_version'] == 'monitor-arithmetic-v1'
        disabled = operator.post('/monitor/observation-policy/activate', json={
            'revision': active.json()['revision'], 'version': None})
        assert disabled.status_code == 200
        reopened = build_local_integration(bundle.root, origin=local.origin, clock=local.clock, seed=False)
        local.stack.callback(next(iter(reopened.app.gateway.service.contracts.values())).runner.core.client.http.close)
        local.server.set_app(reopened.app)
        client.headers['Authorization'] = 'Bearer ' + reopened.credentials['studio'].token
        assert operator.get('/monitor/observation-policy').json() == disabled.json()
        observed = client.get('/v1/tasks/' + payload['run_id'])
        assert observed.status_code == 200
        assert observed.json()['task_features'] == features
        assert observed.json()['routing_recommendation'] == recommendation
        budget = next(iter(reopened.app.gateway.service.contracts.values())).runner.store.path
        budget_before = budget.read_bytes()
        raw_roots = (bundle.root / 'receipts', bundle.root / 'tasks' / features['binding']['internal_id'])
        original_open = io.open

        def record_raw_open(file, mode='r', *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and 'r' in mode:
                path = Path(file).absolute()
                if any(path == root or root in path.parents for root in raw_roots):
                    opened.append(str(path))
            return original_open(file, mode, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(io, 'open', record_raw_open)
            assert operator.get('/monitor/snapshot').status_code == 200
            assert opened, 'Raw-open positive control never observed the legal original reader'
            opened.clear()
            for day in (30, 365):
                local.clock.now = clock_type(accepted_at + day * 86400)
                if day == 30:
                    assert client.get('/v1/tasks/' + payload['run_id']).status_code == 410
                response = operator.get('/monitor/snapshot')
                assert response.status_code == 200
                view = response.json()
                assert view['ledger'] == ledger, 'Original public event metadata was lost after access expiry'
                task = next(row for row in view['tasks'] if row['run'] == payload['run_id'])
                assert task['operator_observation']['task_decision']['binding'] == binding
                assert task['operator_observation']['task_features'] == features
                assert task['operator_observation']['routing_recommendation'] == recommendation
                assert 'PRIVATE_RETAINED_CLOCK_CANARY' not in response.text
                assert opened == [], 'Expired history reopened original raw evidence'
        assert budget.read_bytes() == budget_before
        assert len(bundle.upstream.calls) == 1 and reopened.upstream.calls == []
    finally:
        receipt = {'schema_version': 1, 'scope': 'Actual nonqueue HTTP with real Store/identity/Core and MockTransport',
                   'clock_type': clock_type.__name__, 'http_responses': http.rows, 'upstream_wire': wire.rows,
                   'expired_raw_open_count': len(opened),
                   'artifact_verification': {'http': http.verify(), 'upstream': wire.verify()}}
        (directory / 'fixture-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        assert receipt['artifact_verification']['http']['status'] == receipt['artifact_verification']['upstream']['status'] == 'verified'
