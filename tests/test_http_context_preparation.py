"""#50：參考區塊經真實 TaskService／BudgetedCore／Core 準備、凍結並派送；上游為離線假 API。"""
import json
from dataclasses import replace

import httpx
import pytest
import socket

from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy
from test_budget import controlled
from test_context_preparation import POLICY, _context, _policy
from test_engine import API, ArithmeticEnvironment


def _service(tmp_path, *, policy=POLICY):
    api = API(first='2')
    runner = controlled(tmp_path / 'runner', api, total='2')
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                   classifier='cloud:typesafe')
    for profile in runner.core.profiles.values():
        profile['execution_location'] = 'cloud:fixture'
    contracts = {'plain': RegisteredContract('public-v1', runner, lambda workspace: ArithmeticEnvironment())}
    if policy is not None:
        contracts['blocks'] = RegisteredContract('public-v1', runner, lambda workspace: ArithmeticEnvironment(),
                                                 context_policy=policy)
    service = TaskService(tmp_path / 'service', contracts=contracts, allowed_principal='admin')
    app = HTTPApp(service, authenticate=lambda environ:
                  'admin' if environ.get('HTTP_AUTHORIZATION') == 'Bearer fixture-private-token' else None)
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture',
                          headers={'Authorization': 'Bearer fixture-private-token'})
    return client, service, api


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('HTTP tests must not open real sockets')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


CONTEXT = _context(('keep', '必要：數字 42。', False, 0), ('maybe', 'x' * 400, True, 5), ('tail', 'y' * 20, True, 9))


def _user_content(request):
    # 假 API 記錄的是實際送出的 payload（dict）。
    return [m['content'] for m in request['messages'] if m['role'] == 'user'][0]


def test_prepared_content_is_frozen_and_is_exactly_the_user_content_sent(tmp_path):
    client, service, api = _service(tmp_path)
    response = client.post('/v1/tasks', json={'run_id': 'ctx-1', 'contract_id': 'blocks',
                                              'prompt': '請回答 1+1', 'context': CONTEXT})
    assert response.status_code == 200 and response.json()['status'] == 'passed', response.text
    job = json.loads(next((tmp_path / 'service').glob('*/job.json')).read_text())
    prepared = job['prepared']
    assert [row['decision'] for row in prepared['blocks']] == ['kept', 'omitted', 'kept']
    assert job['registered']['context_policy']['version'] == 'extractive-v1'
    assert _user_content(api.requests[0]) == prepared['content']
    assert prepared['content'].startswith('請回答 1+1') and '必要：數字 42。' in prepared['content']
    assert 'x' * 400 not in prepared['content']


def test_identical_resend_does_not_prepare_or_dispatch_again_and_any_change_conflicts(tmp_path, monkeypatch):
    client, service, api = _service(tmp_path)
    body = {'run_id': 'ctx-2', 'contract_id': 'blocks', 'prompt': 'Q', 'context': CONTEXT}
    assert client.post('/v1/tasks', json=body).status_code == 200
    assert client.post('/v1/tasks', json=body).status_code == 200
    assert len(api.requests) == 1
    changed = {**body, 'context': {**CONTEXT, 'blocks': [{**CONTEXT['blocks'][0], 'priority': 1}, *CONTEXT['blocks'][1:]]}}
    assert client.post('/v1/tasks', json=changed).json()['error']['code'] == 'frozen_task_changed'
    plain = {key: value for key, value in body.items() if key != 'context'}
    assert client.post('/v1/tasks', json=plain).json()['error']['code'] == 'frozen_task_changed'
    assert len(api.requests) == 1


def test_old_format_is_unchanged_and_contracts_without_a_policy_reject_blocks(tmp_path):
    client, service, api = _service(tmp_path)
    old = client.post('/v1/tasks', json={'run_id': 'old-1', 'contract_id': 'plain', 'prompt': 'Return 1+1'})
    assert old.status_code == 200 and _user_content(api.requests[0]) == 'Return 1+1'
    job = json.loads(next(p for p in (tmp_path / 'service').glob('*/job.json')).read_text())
    assert 'prepared' not in job and 'context_policy' not in job['registered']
    rejected = client.post('/v1/tasks', json={'run_id': 'blocks-on-plain', 'contract_id': 'plain', 'prompt': 'Q',
                                              'context': CONTEXT})
    assert rejected.json()['error']['code'] == 'context_preparation_not_registered'
    assert len(api.requests) == 1


@pytest.mark.parametrize('context,policy,code', [
    (_context(('big', 'z' * 500, False, 0)), POLICY, 'required_context_over_limit'),
    (CONTEXT, _policy(token_guarantee='required'), 'token_method_untrusted'),
], ids=['required-over-limit', 'token-untrusted'])
def test_unsafe_preparation_is_rejected_before_any_post_or_task_record(tmp_path, context, policy, code):
    client, service, api = _service(tmp_path, policy=policy)
    response = client.post('/v1/tasks', json={'run_id': 'unsafe', 'contract_id': 'blocks', 'prompt': 'Q',
                                              'context': context})
    assert response.json()['error']['code'] == code
    assert api.requests == [] and not list((tmp_path / 'service').glob('*/job.json'))


def test_resume_after_interruption_reuses_the_frozen_preparation_without_resending(tmp_path, monkeypatch):
    class InterruptOnce(ArithmeticEnvironment):
        attempts = 0

        def validate(self, answer):
            type(self).attempts += 1
            if self.attempts == 1:
                raise RuntimeError('PRIVATE interruption')
            return super().validate(answer)

    client, service, api = _service(tmp_path)
    service.contracts['blocks'] = replace(service.contracts['blocks'], environment_factory=lambda w: InterruptOnce())
    body = {'run_id': 'ctx-resume', 'contract_id': 'blocks', 'prompt': 'Q', 'context': CONTEXT}
    first = client.post('/v1/tasks', json=body)
    assert first.json()['status'] == 'engineering_interrupted'
    job = next((tmp_path / 'service').glob('*/job.json'))
    frozen = job.read_bytes()
    from cloudyfan_jev import context_preparation
    monkeypatch.setattr(context_preparation, 'prepare', lambda *a, **k: pytest.fail('Resume must not prepare again'))
    resumed = client.post('/v1/tasks/ctx-resume/resume', json={})
    assert resumed.status_code == 200 and resumed.json()['status'] == 'passed'
    assert len(api.requests) == 1 and job.read_bytes() == frozen


def test_tool_round_trip_keeps_the_prepared_user_content_and_paired_tool_messages(tmp_path):
    from cloudyfan_jev.budget import EnforcedBound, ToolBound
    api = API(messages=[{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function',
        'function': {'name': 'write', 'arguments': '{"value":"2"}'}}]}, {'role': 'assistant', 'content': '2'}])

    class Env:
        replayable_tools = ('write',)

        def __init__(self, workspace):
            self.workspace = workspace

        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'write', 'parameters': {'type': 'object'}}}]

        def call(self, name, arguments):
            (self.workspace / 'answer.txt').write_text(arguments['value'])
            return {'ok': True}

        def validate(self, answer):
            return {'passed': (self.workspace / 'answer.txt').read_text() == '2'}

    runner = controlled(tmp_path / 'runner', api, total='2')
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture', classifier='cloud:typesafe')
    runner.core.execution_policy.tools['write'] = 'home:fixture'
    runner.tool_bounds['write'] = ToolBound(EnforcedBound('0', 'fixture local zero fee'))
    for profile in runner.core.profiles.values():
        profile['execution_location'] = 'cloud:fixture'
    contract = RegisteredContract('public-v1', runner, Env, tool_names=('write',), context_policy=POLICY)
    service = TaskService(tmp_path / 'service', contracts={'blocks': contract}, allowed_principal='admin')
    client = httpx.Client(transport=httpx.WSGITransport(HTTPApp(service, authenticate=lambda e: 'admin')),
                          base_url='http://fixture')
    response = client.post('/v1/tasks', json={'run_id': 'ctx-tool', 'contract_id': 'blocks', 'prompt': 'Q',
                                              'context': CONTEXT})
    assert response.status_code == 200 and response.json()['status'] == 'passed', response.text
    prepared = json.loads(next((tmp_path / 'service').glob('*/job.json')).read_text())['prepared']['content']
    assert len(api.requests) == 2
    for request in api.requests:
        assert _user_content(request) == prepared, 'Every turn must carry the same frozen prepared content'
    second = api.requests[1]['messages']
    calls = [m for m in second if m.get('tool_calls')]
    results = [m for m in second if m['role'] == 'tool']
    assert len(calls) == len(results) == 1 and results[0]['tool_call_id'] == 't1'


def test_corrupted_frozen_preparation_is_unverifiable_and_never_resent(tmp_path):
    class InterruptOnce(ArithmeticEnvironment):
        attempts = 0

        def validate(self, answer):
            type(self).attempts += 1
            if self.attempts == 1:
                raise RuntimeError('PRIVATE interruption')
            return super().validate(answer)

    client, service, api = _service(tmp_path)
    service.contracts['blocks'] = replace(service.contracts['blocks'], environment_factory=lambda w: InterruptOnce())
    body = {'run_id': 'ctx-corrupt', 'contract_id': 'blocks', 'prompt': 'Q', 'context': CONTEXT}
    assert client.post('/v1/tasks', json=body).json()['status'] == 'engineering_interrupted'
    path = next((tmp_path / 'service').glob('*/job.json'))
    job = json.loads(path.read_text())
    job['prepared']['content'] += ' tampered'
    path.chmod(0o600)
    path.write_text(json.dumps(job))
    response = client.post('/v1/tasks/ctx-corrupt/resume', json={})
    assert response.json()['error']['code'] == 'frozen_preparation_unverifiable'
    assert len(api.requests) == 1


def test_a_policy_update_never_rewrites_an_existing_frozen_task(tmp_path):
    client, service, api = _service(tmp_path)
    body = {'run_id': 'ctx-policy', 'contract_id': 'blocks', 'prompt': 'Q', 'context': CONTEXT}
    assert client.post('/v1/tasks', json=body).status_code == 200
    path = next((tmp_path / 'service').glob('*/job.json'))
    frozen = path.read_bytes()
    service.contracts['blocks'] = replace(service.contracts['blocks'], context_policy={**POLICY, 'allow_omit': False})
    assert client.post('/v1/tasks', json=body).json()['error']['code'] == 'frozen_task_changed'
    assert path.read_bytes() == frozen and len(api.requests) == 1
