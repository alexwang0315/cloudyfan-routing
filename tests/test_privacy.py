"""Public Core.run privacy seam; all network boundaries are offline spies."""
import socket

import httpx
import pytest

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.privacy import ExecutionPolicy, PolicyDenied


@pytest.fixture(autouse=True)
def no_real_sockets(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('A privacy test attempted a real socket')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def make_core(tmp_path, policy):
    requests = []
    def unexpected(request):
        requests.append(request)
        pytest.fail('Restricted task escaped to cloud')
    profiles, decision = example_config()
    client = Client(httpx.Client(transport=httpx.MockTransport(unexpected)), 'mock-key', tmp_path)
    return Core(client, profiles, decision, execution_policy=policy), requests


def test_local_only_stops_cloud_generation_before_any_outbound_request(tmp_path):
    engine, requests = make_core(tmp_path, ExecutionPolicy(mode='local_only', origin='home:alice'))
    with pytest.raises(PolicyDenied) as stopped:
        engine.run('private', {'prompt': 'sensitive-home-data'}, ArithmeticEnvironment, contract_version='v1')
    assert stopped.value.body() == {'error': {'code': 'location_policy_denied',
        'message': 'Execution location policy denied dispatch',
        'details': {'stage': 'generation', 'reason': 'unknown_location'}}}
    assert requests == []
    assert 'sensitive-home-data' not in str(stopped.value)


def test_cloud_classifier_with_unknown_location_is_stopped_after_first_answer(tmp_path):
    requests = []
    def endpoint(request):
        requests.append(request)
        assert len(requests) == 1, 'Classifier request escaped the policy gate'
        return httpx.Response(200, json={'id': 'gen-policy-1', 'model': 'example/open_source',
            'provider': 'Example', 'usage': {'cost': .01},
            'choices': [{'message': {'content': '3'}, 'finish_reason': 'stop'}]})
    profiles, decision = example_config()
    profiles['open_source']['execution_location'] = 'cloud:example'
    engine = Core(Client(httpx.Client(transport=httpx.MockTransport(endpoint)), 'mock-key', tmp_path),
                  profiles, decision, execution_policy=ExecutionPolicy(mode='unrestricted', origin='home:alice'))
    with pytest.raises(PolicyDenied, match='policy denied'):
        engine.run('classifier', {'prompt': '1+1'}, ArithmeticEnvironment, contract_version='v1')
    assert len(requests) == 1


def test_tool_unknown_location_cannot_execute_or_leak_arguments(tmp_path):
    requests = []
    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'gen-policy-tool', 'model': 'example/open_source',
            'provider': 'Example', 'usage': {'cost': .01}, 'choices': [{'finish_reason': 'tool_calls',
            'message': {'content': None, 'tool_calls': [{'id': 't1', 'type': 'function',
                'function': {'name': 'external_search', 'arguments': '{"query":"private data"}'}}]}}]})
    class Env(ArithmeticEnvironment):
        def call(self, name, arguments):
            pytest.fail('Unregistered tool executed')
    profiles, decision = example_config()
    profiles['open_source']['execution_location'] = 'cloud:example'
    engine = Core(Client(httpx.Client(transport=httpx.MockTransport(endpoint)), 'mock-key', tmp_path),
                  profiles, decision, execution_policy=ExecutionPolicy(mode='unrestricted', origin='home:alice'))
    with pytest.raises(PolicyDenied) as stopped:
        engine.run('tool', {'prompt': 'private data'}, Env, contract_version='v1')
    assert stopped.value.details == {'stage': 'tool', 'reason': 'unknown_location'}
    assert 'private data' not in str(stopped.value)
    assert len(requests) == 1


@pytest.mark.parametrize('location,backend,reason', [
    ('company:alice', 'local', 'outside_origin'),
    ('home:bob', 'local', 'outside_origin'),
    ('cloud:example', None, 'outside_origin'),
    ('home:alice', None, 'backend_location_mismatch'),
    ('home:alice', 'local', 'backend_location_mismatch'),
])
def test_local_only_cannot_be_bypassed_by_relabeling_cloud_or_another_site(tmp_path, location, backend, reason):
    engine, requests = make_core(tmp_path, ExecutionPolicy(mode='local_only', origin='home:alice'))
    engine.profiles['open_source'].update(execution_location=location, backend=backend)
    with pytest.raises(PolicyDenied) as stopped:
        engine.run('site', {'prompt': '1+1'}, ArithmeticEnvironment, contract_version='v1')
    assert stopped.value.details == {'stage': 'generation', 'reason': reason}
    assert requests == []


@pytest.mark.parametrize('change', ['remove', 'origin', 'tools', 'classifier', 'version'])
def test_resume_cannot_change_frozen_privacy_policy(tmp_path, change):
    calls = []
    def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'gen-policy-pass', 'model': 'example/open_source',
            'provider': 'Example', 'usage': {'cost': .01},
            'choices': [{'message': {'content': '2'}, 'finish_reason': 'stop'}]})
    profiles, decision = example_config()
    profiles['open_source']['execution_location'] = 'cloud:example'
    client = Client(httpx.Client(transport=httpx.MockTransport(endpoint)), 'mock-key', tmp_path)
    options = dict(mode='unrestricted', origin='home:alice', classifier='cloud:typesafe')
    engine = Core(client, profiles, decision, execution_policy=ExecutionPolicy(**options))
    result = engine.run('frozen', {'prompt': '1+1'}, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    if change != 'remove':
        options[change] = {'origin': 'company:alice', 'tools': {'search': 'cloud:search'},
                           'classifier': 'cloud:other', 'version': 'v2'}[change]
    changed = Core(client, profiles, decision, execution_policy=None if change == 'remove' else ExecutionPolicy(**options))
    with pytest.raises(ValueError, match='Resume evidence changed'):
        changed.run('frozen', {'prompt': '1+1'}, ArithmeticEnvironment, contract_version='v1')
    assert len(calls) == 1
