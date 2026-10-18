"""Public Core.run privacy seam; all network boundaries are offline spies."""
import socket
import json

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


class ToolCapableFixtureBackend:
    """Injected system-boundary fake; the production local adapter is text-only."""
    def __init__(self, backend, handler):
        self.config = backend.descriptor()
        self.config['protocol'] = 'fixture-tools-v1'
        self.root = backend.root
        self.http = httpx.Client(transport=httpx.MockTransport(handler))
    def descriptor(self): return dict(self.config)
    def matches_profile(self, profile):
        return all(profile.get(key) == self.config[key] for key in
                   ('model_version', 'quantization', 'engine', 'execution_location'))
    def _directory(self, cid): return self.root / 'calls' / cid
    def _load(self, directory): return json.loads((directory / 'response.json').read_text())
    def call(self, cid, payload):
        directory = self._directory(cid)
        if (directory / 'response.json').exists():
            return self._load(directory)
        directory.mkdir(parents=True, exist_ok=True)
        response = self.http.post(self.config['endpoint'], json=payload)
        record = {'raw': response.text}
        (directory / 'response.json').write_text(json.dumps(record))
        return record
    def checked(self, record):
        assert json.loads(record['raw'])['model'] == self.config['model_version']
        return {'execution_kind': 'local', 'cost_unknown': False, 'cost_usd': '0'}


def private_local_core(tmp_path, local_handler, *, classifier='cloud:typesafe', tools=None, tool_fixture=False):
    from test_local_backend import local_engine
    base = local_engine(tmp_path, local_handler)
    for profile in base.profiles.values():
        if profile.get('backend') != 'local':
            profile['execution_location'] = 'cloud:example'
    backend = ToolCapableFixtureBackend(base.local_backend, local_handler) if tool_fixture else base.local_backend
    return Core(base.client, base.profiles, base.decision, local_backend=backend,
                execution_policy=ExecutionPolicy(mode='local_only', origin='home:fixture',
                                                 classifier=classifier, tools=tools or {}))


def test_private_local_pass_and_completed_resume_need_no_cloud_or_classifier(tmp_path):
    from test_local_backend import TextContract, response
    local_calls = []
    def local(request):
        local_calls.append(request)
        return response()
    engine = private_local_core(tmp_path, local)
    first = engine.run('local-private', {'prompt': '1+1'}, TextContract, contract_version='v1')
    second = engine.run('local-private', {'prompt': '1+1'}, TextContract, contract_version='v1')
    assert first == second
    assert first['status'] == 'passed' and first['selection'] is None
    assert first['costs']['api_usd'] == '0'
    assert len(local_calls) == 1


@pytest.mark.parametrize('mode,classifier,stage,reason', [
    ('cloudyfan', 'cloud:typesafe', 'classifier', 'outside_origin'),
    ('cloudyfan', None, 'classifier', 'unknown_location'),
    ('cloudyfan', 'home:fixture', 'classifier', 'backend_location_mismatch'),
    ('open_source_first', 'cloud:typesafe', 'generation', 'outside_origin'),
])
def test_private_local_failure_cannot_escape_through_classifier_or_direct_fallback(
        tmp_path, mode, classifier, stage, reason):
    from test_local_backend import TextContract, response
    from cloudyfan_jev.ledger import summarize
    local_calls = []
    def local(request):
        local_calls.append(request)
        return response('3')
    engine = private_local_core(tmp_path, local, classifier=classifier)
    for _ in range(2):
        with pytest.raises(PolicyDenied) as stopped:
            engine.run('no-cloud', {'prompt': '1+1'}, TextContract, contract_version='v1', mode=mode)
        assert stopped.value.details == {'stage': stage, 'reason': reason}
    assert len(local_calls) == 1, 'Resume must reuse the first local response'
    assert summarize(engine.local_backend)['calls'] == 1
    assert summarize(engine.client)['calls'] == 0


@pytest.mark.parametrize('location,reason', [
    (None, 'unknown_location'), ('cloud:search', 'outside_origin'),
    ('company:fixture', 'outside_origin'), ('home:other', 'outside_origin'),
])
def test_private_local_tool_cannot_egress_on_first_attempt_or_resume(tmp_path, location, reason):
    from test_local_backend import TextContract, response
    local_calls = []
    def local(request):
        local_calls.append(request)
        return response(choices=[{'finish_reason': 'tool_calls', 'message': {'content': None,
            'tool_calls': [{'id': 't1', 'type': 'function',
                'function': {'name': 'search', 'arguments': '{"query":"private data"}'}}]}}])
    class Env(TextContract):
        def call(self, name, arguments):
            pytest.fail('Blocked tool executed')
    engine = private_local_core(tmp_path, local, tools={'search': location} if location else {}, tool_fixture=True)
    for _ in range(2):
        with pytest.raises(PolicyDenied) as stopped:
            engine.run('private-tool', {'prompt': 'private data'}, Env, contract_version='v1')
        assert stopped.value.details == {'stage': 'tool', 'reason': reason}
    assert len(local_calls) == 1


def test_allowed_local_tool_and_replay_remain_inside_the_origin(tmp_path):
    from test_local_backend import TextContract, response
    local_calls, environments = [], []
    def local(request):
        local_calls.append(request)
        if len(local_calls) == 1:
            return response(choices=[{'finish_reason': 'tool_calls', 'message': {'content': None,
                'tool_calls': [{'id': 't1', 'type': 'function',
                    'function': {'name': 'write', 'arguments': '{"value":"2"}'}}]}}])
        return response()
    class Env(TextContract):
        replayable_tools = ('write',)

        def __init__(self):
            self.value = None
            environments.append(self)
        def call(self, name, arguments):
            assert name == 'write'
            self.value = arguments['value']
            return {'ok': True}
        def validate(self, text):
            if len(environments) == 1:
                raise RuntimeError('fixture verifier interruption')
            return {'passed': self.value == '2' and text == '2'}
    engine = private_local_core(tmp_path, local, tools={'write': 'home:fixture'}, tool_fixture=True)
    with pytest.raises(RuntimeError, match='fixture verifier interruption'):
        engine.run('tool-replay', {'prompt': '1+1'}, Env, contract_version='v1')
    result = engine.run('tool-replay', {'prompt': '1+1'}, Env, contract_version='v1')
    assert result['status'] == 'passed' and result['resumed'] is True
    assert len(local_calls) == 2 and len(environments) == 2
    assert result['costs']['api_usd'] == '0'
