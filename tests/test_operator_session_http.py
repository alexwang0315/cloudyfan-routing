"""Original operator authentication remains independent of fleet observation."""
from datetime import datetime
import hashlib
import json
from uuid import uuid4

import pytest

from capacity_resilience_fixture import build_resilience_health_host
from test_local_http_integration import local_service as local_service, loopback_network as loopback_network


@pytest.fixture
def operator_host(local_service, tmp_path):
    local_service.clock.now = datetime.fromisoformat('2026-10-21T00:00:00+00:00').timestamp()
    host = build_resilience_health_host(tmp_path / 'operator-host',
        clock=local_service.clock, origin=local_service.origin)
    local_service.bundle = host.host
    local_service.server.set_app(host.app)
    try:
        yield host
    finally:
        host.close()


def fingerprints(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*')) if path.is_file()}


def test_operator_session_confirms_original_guard_without_observation_or_dispatch(
        operator_host, local_service, loopback_network, tmp_path):
    host = operator_host
    operator = local_service.client('operator', timeout=10)
    before = fingerprints(host.host.root)
    response = operator.get('/monitor/session')
    if response.status_code != 200:
        pytest.fail(f'Expected original authorized operator session200; received {response.status_code}')
    private_values = [host.host.operator_token, *(row.token for row in host.host.credentials.values())]
    if any(value.encode() in response.content for value in private_values):
        pytest.fail('Operator session exported a credential')
    payload = response.json()
    expected = {'schema_version': 'monitor-operator-session-v1', 'authenticated': True, 'demo': True}
    if payload != expected:
        pytest.fail('Operator session did not match the fixed safe schema')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert "default-src 'self'" in response.headers['content-security-policy']
    assert operator.get('/monitor/session').json() == expected
    assert fingerprints(host.host.root) == before
    assert len(host.requests) == 0 and not any(host.side_requests.values())
    assert loopback_network.blocked == []
    (tmp_path / 'operator-session-evidence.json').write_text(json.dumps({
        'response': payload, 'all_original_files_unchanged': True,
        'original_files_sha256': before, 'model_requests': 0,
        'source_requests': 0, 'accounting_requests': 0,
        'blocked_socket_attempts': 0}, indent=2))


def test_operator_session_keeps_existing_guards_and_never_grants_client_execution(
        operator_host, local_service, loopback_network, tmp_path):
    host = operator_host
    operator = local_service.client('operator', timeout=10)
    original_token = host.host.operator_token
    records = []

    def observe(label, method, path, expected, **kwargs):
        before = fingerprints(host.host.root)
        response = operator.request(method, path, **kwargs)
        if response.status_code != expected:
            pytest.fail(f'Operator session guard {label} returned unexpected HTTP status')
        assert fingerprints(host.host.root) == before
        assert 'set-cookie' not in response.headers
        records.append({'case': label, 'status': response.status_code})
        return response

    observe('missing', 'GET', '/monitor/session', 401, headers={'Authorization': ''})
    observe('invalid', 'GET', '/monitor/session', 401,
        headers={'Authorization': 'Bearer PRIVATE_SYNTHETIC_INVALID_OPERATOR'})
    for name in ('studio', 'agent'):
        observe('client-' + name, 'GET', '/monitor/session', 401,
            headers={'Authorization': 'Bearer ' + host.host.credentials[name].token})
    host.app.identity_store.revoke(host.host.credentials['agent'].id)
    observe('revoked-client', 'GET', '/monitor/session', 401,
        headers={'Authorization': 'Bearer ' + host.host.credentials['agent'].token})
    observe('wrong-host', 'GET', '/monitor/session', 403, headers={'Host': 'untrusted.invalid'})
    observe('wrong-origin', 'GET', '/monitor/session', 403, headers={'Origin': 'https://untrusted.invalid'})
    observe('query', 'GET', '/monitor/session?refresh=1', 422)
    for method in ('POST', 'PUT', 'DELETE', 'HEAD'):
        observe('method-' + method, method, '/monitor/session', 404)
    observe('GET-body', 'GET', '/monitor/session', 422, json={'action': 'refresh'})
    rotated = uuid4().hex
    host.app.admin_token = rotated
    observe('replaced-operator', 'GET', '/monitor/session', 401,
        headers={'Authorization': 'Bearer ' + original_token})
    operator.headers['Authorization'] = 'Bearer ' + rotated
    session = observe('current-operator', 'GET', '/monitor/session', 200)
    assert session.json()['authenticated'] is True
    observe('operator-is-not-client-dispatch-grant', 'POST', '/v1/tasks', 401,
        json={'run_id': 'operator-cannot-dispatch', 'category': host.category, 'prompt': 'Return 2.'})
    host.app.gateway.demo = False
    actual = observe('original-production-flag', 'GET', '/monitor/session', 200).json()
    if actual != {'schema_version': 'monitor-operator-session-v1', 'authenticated': True, 'demo': False}:
        pytest.fail('Operator session changed the original false demo flag')
    assert len(host.requests) == 0 and not any(host.side_requests.values())
    assert loopback_network.blocked == []
    (tmp_path / 'operator-session-guards.json').write_text(json.dumps({
        'cases': records, 'original_files_unchanged_per_request': True,
        'model_requests': 0, 'source_requests': 0, 'accounting_requests': 0,
        'blocked_socket_attempts': 0}, indent=2))


def test_operator_session_refuses_a_chunked_GET_body_without_observation(
        operator_host, local_service, tmp_path):
    host = operator_host
    operator = local_service.client('operator', timeout=10)
    before = fingerprints(host.host.root)
    response = operator.request('GET', '/monitor/session', content=iter([b'{}']),
        headers={'Content-Type': 'application/json'})
    assert fingerprints(host.host.root) == before
    assert len(host.requests) == 0 and not any(host.side_requests.values())
    (tmp_path / 'operator-session-chunked.json').write_text(json.dumps({
        'status': response.status_code, 'original_files_unchanged': True,
        'model_requests': 0}, indent=2))
    if response.status_code != 422:
        pytest.fail('Operator session accepted an unsupported chunked GET body')
