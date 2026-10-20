"""Private HTTP seam with real Core/BudgetedCore and offline upstream."""
import httpx
import pytest
import socket

from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService
from cloudyfan_jev.privacy import ExecutionPolicy
from test_budget import controlled
from test_engine import API, ArithmeticEnvironment


def service_client(tmp_path, *, api=None, environment=None, policy=None, total='2', verifier=lambda *a: True):
    api = api or API(first='2')
    runner = controlled(tmp_path / 'runner', api, total=total, verifier=verifier)
    runner.core.execution_policy = policy or ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                            classifier='cloud:typesafe')
    for profile in runner.core.profiles.values():
        profile['execution_location'] = 'cloud:fixture'
    workspaces = []
    def factory(workspace):
        workspaces.append(workspace)
        return (environment or ArithmeticEnvironment)()
    contract = RegisteredContract('public-v1', runner, factory)
    service = TaskService(tmp_path / 'service', contracts={'arithmetic': contract}, allowed_principal='admin')
    app = HTTPApp(service, authenticate=lambda environ:
                  'admin' if environ.get('HTTP_AUTHORIZATION') == 'Bearer fixture-private-token' else None)
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture',
                          headers={'Authorization': 'Bearer fixture-private-token'})
    return client, service, runner, api, workspaces


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('HTTP tests must not open real sockets')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


TASK = {'run_id': 'task-1', 'contract_id': 'arithmetic', 'prompt': 'Return 1+1'}


def test_http_submit_status_result_resume_is_one_paid_flow(tmp_path):
    client, service, runner, api, workspaces = service_client(tmp_path)
    submitted = client.post('/v1/tasks', json=TASK)
    assert submitted.status_code == 200
    assert submitted.json()['status'] == 'passed'
    assert 'answer' not in submitted.json()
    status = client.get('/v1/tasks/task-1')
    assert status.json()['status'] == 'passed'
    result = client.get('/v1/tasks/task-1/result')
    assert result.json()['answer'] == '2'
    assert result.json()['costs']['complete_api_usd'] == '0.01'
    assert client.post('/v1/tasks/task-1/resume', json={}).status_code == 200
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    assert len(api.requests) == 1
    assert len(workspaces) == 1 and workspaces[0].is_dir()
    assert 'shared' not in result.text and 'fixture-private-token' not in result.text


def test_http_engineering_interruption_keeps_paid_answer_and_resumes_fresh_workspace(tmp_path):
    class InterruptOnce(ArithmeticEnvironment):
        attempts = 0
        def validate(self, answer):
            type(self).attempts += 1
            if self.attempts == 1:
                raise RuntimeError('SECRET traceback and PRIVATE answer')
            return super().validate(answer)
    client, service, runner, api, workspaces = service_client(tmp_path, environment=InterruptOnce)
    first = client.post('/v1/tasks', json=TASK)
    assert first.status_code == 503
    assert first.json()['status'] == 'engineering_interrupted'
    assert 'SECRET' not in first.text and 'PRIVATE' not in first.text
    assert client.get('/v1/tasks/task-1/result').status_code == 409
    resumed = client.post('/v1/tasks/task-1/resume', json={})
    assert resumed.status_code == 200 and resumed.json()['status'] == 'passed'
    assert len(api.requests) == 1
    assert len(workspaces) == 2 and workspaces[0] != workspaces[1]


def test_http_rejects_unregistered_tool_as_policy_before_external_effect(tmp_path):
    class Env(ArithmeticEnvironment):
        def call(self, name, arguments):
            raise AssertionError('Unregistered tool escaped')
    api = API(messages=[{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function',
               'function': {'name': 'external', 'arguments': '{}'}}]}])
    client, service, runner, api, _ = service_client(tmp_path, api=api, environment=Env)
    # A server budget bound alone must not grant the contract tool permission.
    from cloudyfan_jev.budget import EnforcedBound, ToolBound
    runner.tool_bounds['external'] = ToolBound(EnforcedBound('0', 'fixture'))
    runner.core.execution_policy.tools['external'] = 'cloud:fixture'
    result = client.post('/v1/tasks', json=TASK)
    assert result.status_code == 403
    assert result.json()['status'] == 'policy_denied'
    assert result.json()['error']['code'] == 'budget_admission_denied'
    assert runner.store.report()['unknown_events'] == 0
    assert len(api.requests) == 1


def test_interrupted_status_retains_cost_and_completed_result_tracks_reconciliation(tmp_path):
    import json
    api = API(first='2')
    def endpoint(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1', 'total_cost': .012,
                'model': 'example/open_source', 'provider_name': 'Example'}})
        return api(request)
    client, service, runner, _, _ = service_client(tmp_path, api=endpoint)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    assert client.get('/v1/tasks/task-1').json()['costs']['complete_api_usd'] == '0.01'
    internal = service.internal_id('admin', 'task-1')
    runner.client.reconcile('run-' + internal + '-a0-t0')
    result = client.get('/v1/tasks/task-1/result')
    assert result.json()['costs']['complete_api_usd'] == '0.012'
    assert '0.01"' not in json.dumps(result.json()['costs'])
    assert len(api.requests) == 1


@pytest.mark.parametrize('path', ['/v1/tasks', '/v1/tasks/task-1', '/v1/tasks/task-1/result',
                                 '/v1/tasks/task-1/resume', '/receipts', '/workspace'])
def test_http_authentication_precedes_every_endpoint(tmp_path, path):
    client, service, runner, api, _ = service_client(tmp_path)
    response = client.get(path, headers={'Authorization': 'Bearer wrong'})
    assert response.status_code == 401 and response.json() == {'error': {'code': 'unauthorized'}}
    assert api.requests == []


@pytest.mark.parametrize('field,value', [('messages', []), ('stream', True), ('image', 'data:...'),
    ('tools', []), ('validator', 'execute code'), ('policy', {'mode': 'unrestricted'}),
    ('model', 'arbitrary'), ('principal', 'admin'), ('inputs', {'hidden_answer': 'secret'})])
def test_http_rejects_unsupported_input_before_model_dispatch(tmp_path, field, value):
    client, _, _, api, _ = service_client(tmp_path)
    response = client.post('/v1/tasks', json={**TASK, field: value})
    assert response.status_code == 422 and response.json()['error']['code'] == 'unsupported_task_fields'
    assert api.requests == []


@pytest.mark.parametrize('change', ['prompt', 'contract_version', 'model', 'policy', 'diagnostics', 'bounds'])
def test_queued_job_cannot_execute_with_changed_frozen_inputs_or_registered_configuration(tmp_path, change):
    from dataclasses import replace
    from cloudyfan_jev.budget import EnforcedBound
    from cloudyfan_jev.diagnostics import DiagnosticContract
    client, service, runner, api, _ = service_client(tmp_path)
    service.create('admin', TASK)
    if change == 'prompt':
        response = client.post('/v1/tasks', json={**TASK, 'prompt': 'different'})
    else:
        if change == 'contract_version':
            service.contracts['arithmetic'] = replace(service.contracts['arithmetic'], version='v2')
        if change == 'model': runner.core.profiles['high']['capability_card'] = {'changed': True}
        if change == 'policy': runner.core.execution_policy = ExecutionPolicy(mode='local_only', origin='home:fixture')
        if change == 'diagnostics': runner.core.diagnostic_contract = DiagnosticContract(version='v2')
        if change == 'bounds': runner.model_bounds['example/high'] = EnforcedBound('.02', 'changed')
        response = client.post('/v1/tasks/task-1/resume', json={})
    assert response.status_code == 409
    assert api.requests == []


@pytest.mark.parametrize('passed,status', [(False, 'failed'), (None, 'unverifiable')])
def test_http_transport_success_is_not_validation_success(tmp_path, passed, status):
    class Env(ArithmeticEnvironment):
        def validate(self, answer): return {'passed': passed}
    client, _, _, api, _ = service_client(tmp_path, environment=Env)
    response = client.post('/v1/tasks', json=TASK)
    assert response.status_code == 200 and response.json()['status'] == status
    result = client.get('/v1/tasks/task-1/result').json()
    assert result['final_public_pass'] is passed
    assert result['costs']['complete_api_usd'] == '0.0302'
    assert len(api.requests) == 5


def test_http_local_only_and_unconfirmed_budget_stop_before_dispatch(tmp_path):
    client, _, _, api, _ = service_client(tmp_path / 'privacy', policy=ExecutionPolicy(mode='local_only', origin='home:fixture'))
    response = client.post('/v1/tasks', json=TASK)
    assert response.status_code == 403 and response.json()['status'] == 'policy_denied'
    assert api.requests == []
    client, _, _, api, _ = service_client(tmp_path / 'budget', verifier=lambda *a: False)
    assert client.post('/v1/tasks', json=TASK).status_code == 403
    assert api.requests == []


def test_http_unknown_cost_stops_resume_without_repeating_paid_request(tmp_path):
    import json
    api = API(first='2')
    def unknown(request):
        if request.method == 'GET': return httpx.Response(404, json={'error': 'missing'})
        data = json.loads(api(request).content)
        data['usage'].pop('cost')
        return httpx.Response(200, json=data)
    client, service, runner, _, _ = service_client(tmp_path, api=unknown)
    first = client.post('/v1/tasks', json=TASK)
    assert first.json()['status'] == 'accounting_interrupted'
    assert first.json()['costs']['complete_api_usd'] is None
    second = client.post('/v1/tasks/task-1/resume', json={})
    assert second.status_code == 409 and second.json()['status'] == 'accounting_interrupted'
    assert len(api.requests) == 1


def test_http_result_refuses_mismatched_reconciled_identity_without_new_generation(tmp_path):
    api = API(first='2')
    def endpoint(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1', 'total_cost': .012,
                'model': 'wrong/model', 'provider_name': 'Example'}})
        return api(request)
    client, service, runner, _, _ = service_client(tmp_path, api=endpoint)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    runner.client.reconcile('run-' + service.internal_id('admin', 'task-1') + '-a0-t0')
    response = client.get('/v1/tasks/task-1/result')
    assert response.status_code == 503
    assert response.json() == {'error': {'code': 'result_evidence_unavailable'}}
    assert len(api.requests) == 1


def test_http_registered_tool_writes_only_in_its_fresh_private_workspace(tmp_path):
    from dataclasses import replace
    from cloudyfan_jev.budget import EnforcedBound, ToolBound
    api = API(messages=[{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function',
        'function': {'name': 'write', 'arguments': '{"value":"2"}'}}]}, {'role': 'assistant', 'content': '2'}])
    client, service, runner, api, _ = service_client(tmp_path, api=api)
    roots = []
    class Env:
        replayable_tools = ('write',)
        def __init__(self, workspace):
            self.workspace = workspace
            roots.append(workspace)
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'write', 'parameters': {'type': 'object'}}}]
        def call(self, name, arguments):
            (self.workspace / 'answer.txt').write_text(arguments['value'])
            return {'ok': True}
        def validate(self, answer):
            return {'passed': (self.workspace / 'answer.txt').read_text() == '2'}
    service.contracts['arithmetic'] = replace(service.contracts['arithmetic'], environment_factory=Env, tool_names=('write',))
    runner.tool_bounds['write'] = ToolBound(EnforcedBound('0', 'fixture local zero fee'))
    runner.core.execution_policy.tools['write'] = 'home:fixture'
    response = client.post('/v1/tasks', json=TASK)
    assert response.status_code == 200 and response.json()['status'] == 'passed'
    assert response.json()['costs']['complete_api_usd'] == '0.02'
    assert roots[0].parent.name == service.internal_id('admin', 'task-1')
    assert roots[0].stat().st_mode & 0o777 == 0o700
    for path in ('/workspace/answer.txt', '/receipts', '/v1/tasks/task-1/answer.txt'):
        assert client.get(path).status_code == 404


def test_http_prior_incomplete_intent_blocks_without_automatic_resend(tmp_path):
    from cloudyfan_jev.client import CHAT
    calls = []
    def interrupted(request):
        calls.append(request)
        raise KeyboardInterrupt('fixture process stopped after dispatch')
    client, service, runner, api, _ = service_client(tmp_path, api=interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.client.call('prior-request', CHAT, {'model': 'example/open_source'})
    response = client.post('/v1/tasks', json=TASK)
    assert response.status_code == 409 and response.json()['status'] == 'accounting_interrupted'
    assert len(calls) == 1


def test_http_audit_and_errors_never_echo_authorization_or_exception_text(tmp_path):
    client, service, runner, api, _ = service_client(tmp_path)
    entries = []
    def broken_auth(environ):
        raise RuntimeError('PRIVATE fixture-private-token secret-stacktrace')
    app = HTTPApp(service, authenticate=broken_auth, audit=entries.append)
    with httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture') as caller:
        response = caller.post('/v1/tasks', json=TASK)
    assert response.status_code == 500
    assert response.json() == {'error': {'code': 'service_interrupted'}}
    assert entries == [{'event': 'http_task_request', 'http_status': 500}]
    assert api.requests == []


def test_core_completed_read_rejects_paths_and_missing_result_without_dispatch(tmp_path):
    client, service, runner, api, _ = service_client(tmp_path)
    for identifier in ('../other', 'missing', 'bad/name', ''):
        with pytest.raises(ValueError):
            runner.core.read_completed(identifier)
    assert api.requests == []


def test_http_duplicate_json_keys_are_ambiguous_and_rejected(tmp_path):
    client, _, _, api, _ = service_client(tmp_path)
    response = client.post('/v1/tasks', content='{"run_id":"one","run_id":"two",'
        '"contract_id":"arithmetic","prompt":"1+1"}', headers={'Content-Type': 'application/json'})
    assert response.status_code == 422 and response.json()['error']['code'] == 'invalid_json'
    assert api.requests == []


def test_http_rejects_symlinked_task_directory_without_touching_target(tmp_path):
    client, service, runner, api, _ = service_client(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (service.root / service.internal_id('admin', 'task-1')).symlink_to(outside, target_is_directory=True)
    response = client.post('/v1/tasks', json=TASK)
    assert response.status_code == 403
    assert list(outside.iterdir()) == [] and api.requests == []


def test_http_per_run_result_does_not_expose_shared_account_events_or_other_answers(tmp_path):
    client, service, runner, api, roots = service_client(tmp_path)
    runner.store.import_history('historical-private-owner', cost_usd='0.02', receipt_id='private-other-receipt', verified=True)
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    assert client.post('/v1/tasks', json={**TASK, 'run_id': 'task-2'}).status_code == 200
    result = client.get('/v1/tasks/task-1/result')
    assert result.json()['costs']['complete_api_usd'] == '0.01'
    assert 'historical-private-owner' not in result.text and 'private-other-receipt' not in result.text
    assert 'task-2' not in result.text and 'shared' not in result.text
    assert roots[0].parent != roots[1].parent
    with pytest.raises(Exception, match='unauthorized'):
        service.result('other', 'task-1')


def test_http_result_exposes_safe_observed_and_frozen_first_pass_provenance(tmp_path):
    client, service, runner, api, _ = service_client(tmp_path)
    profile = runner.core.profiles['open_source']
    profile['capability_card'] = {'private_notes': 'NEVER_EXPOSE_CARD',
        'score_selection': {'snapshot_id': 'snapshot-1', 'policy_id': 'policy-1', 'config_id': 'oss-1',
                            'requirements': {'private_context': 'NEVER_EXPOSE_REQUIREMENTS'}},
        'benchmark_evidence': {'source': 'fixture', 'source_version': '2026-10', 'metric': 'sql',
                               'raw': 'NEVER_EXPOSE_EVIDENCE'}}
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    response = client.get('/v1/tasks/task-1/result')
    provenance = response.json()['provenance']
    assert provenance['contract_version'] == 'public-v1'
    assert provenance['classifier_calls'] == []
    attempt = provenance['attempts'][0]
    assert attempt['attempt'] == 0 and attempt['tier'] == 'open_source'
    call = attempt['calls'][0]
    assert (call['actual_model'], call['actual_provider'], call['generation_id']) == (
        'example/open_source', 'Example', 'gen-test-1')
    assert call['configuration']['model_version'] == 'example/open_source'
    assert call['configuration']['effort'] == 'low'
    assert call['configuration']['quantization'] is None
    assert call['score'] == {'snapshot_id': 'snapshot-1', 'policy_id': 'policy-1', 'config_id': 'oss-1',
        'source': 'fixture', 'source_version': '2026-10', 'metric': 'sql'}
    assert 'NEVER_EXPOSE' not in response.text and TASK['prompt'] not in response.text
    assert len(provenance['config_sha256']) == 64
    assert client.get('/v1/tasks/task-1/result').json()['provenance'] == provenance
    assert len(api.requests) == 1


def test_http_provenance_refreshes_validated_reconciled_identity_without_reposting(tmp_path):
    import json
    api = API(first='2')
    def endpoint(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1', 'total_cost': .012,
                'model': 'example/open_source', 'provider_name': 'Example'}})
        data = json.loads(api(request).content)
        data['model'] = 'example/accepted-alias'
        return httpx.Response(200, json=data)
    client, service, runner, _, _ = service_client(tmp_path, api=endpoint)
    runner.core.profiles['open_source']['accepted_models'].append('example/accepted-alias')
    assert client.post('/v1/tasks', json=TASK).status_code == 200
    def model():
        return client.get('/v1/tasks/task-1/result').json()['provenance']['attempts'][0]['calls'][0]['actual_model']
    assert model() == 'example/accepted-alias'
    runner.client.reconcile('run-' + service.internal_id('admin', 'task-1') + '-a0-t0')
    assert model() == 'example/open_source'
    assert client.get('/v1/tasks/task-1/result').json()['costs']['complete_api_usd'] == '0.012'
    assert len(api.requests) == 1


def test_http_routed_repair_provenance_separates_generation_and_classifiers(tmp_path):
    client, _, _, api, _ = service_client(tmp_path, api=API(choices=('mid', 'high')))
    assert client.post('/v1/tasks', json=TASK).json()['status'] == 'passed'
    result = client.get('/v1/tasks/task-1/result').json()
    provenance = result['provenance']
    assert [a['tier'] for a in provenance['attempts']] == ['open_source', 'mid', 'high']
    assert [a['calls'][0]['actual_model'] for a in provenance['attempts']] == [
        'example/open_source', 'example/mid', 'example/high']
    assert [c['actual_model'] for c in provenance['classifier_calls']] == ['typesafe/jev-1.13'] * 2
    for call in provenance['classifier_calls']:
        assert call['actual_provider'] == 'TypeSafe'
        assert call['configuration']['model_version'] == 'typesafe/jev-1.13'
        assert call['configuration']['effort'] is None
        assert all(value is None for value in call['score'].values())
    assert result['first_public_pass'] is False and result['final_public_pass'] is True
    assert client.post('/v1/tasks/task-1/resume', json={}).status_code == 200
    assert client.get('/v1/tasks/task-1/result').json()['provenance'] == provenance
    assert len(api.requests) == 5
