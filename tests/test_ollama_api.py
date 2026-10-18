"""CloudyFan Ollama protocol consumer v1; not an actual Ollama SDK test."""
import httpx
import pytest

from cloudyfan_jev.ollama_api import OllamaApp
from test_http_api import service_client


def consumer(tmp_path, **kwargs):
    original, service, runner, api, workspaces = service_client(tmp_path, **kwargs)
    original.close()
    app = OllamaApp(service, models={'cloudyfan/arithmetic:v1': 'arithmetic'},
        authenticate=lambda env: 'admin' if env.get('HTTP_AUTHORIZATION') == 'Bearer fixture-private-token' else None,
        clock=lambda: '2026-10-18T10:00:00Z')
    client = httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture',
                          headers={'Authorization': 'Bearer fixture-private-token', 'X-CloudyFan-Run-ID': 'ollama-1'})
    return client, service, runner, api, workspaces


BODY = {'model': 'cloudyfan/arithmetic:v1', 'prompt': 'Return 1+1', 'stream': False}


def test_protocol_consumer_lists_alias_then_generates_validated_text(tmp_path):
    client, _, _, api, _ = consumer(tmp_path)
    listed = client.get('/api/tags').json()
    assert listed['models'] == [{'name': BODY['model'], 'model': BODY['model']}]
    assert listed['cloudyfan']['protocol_version'] == 'cloudyfan-ollama-text-v1'
    response = client.post('/api/generate', json=BODY)
    assert response.status_code == 200
    data = response.json()
    assert data['model'] == BODY['model'] and data['response'] == '2' and data['done'] is True
    assert data['created_at'] == '2026-10-18T10:00:00Z'
    assert data['cloudyfan']['status'] == 'passed'
    assert data['cloudyfan']['costs']['complete_api_usd'] == '0.01'
    assert 'eval_count' not in data and 'total_duration' not in data
    assert len(api.requests) == 1


@pytest.mark.parametrize('payload', [
    {**BODY, 'stream': True}, {key: val for key, val in BODY.items() if key != 'stream'},
    {**BODY, 'stream': 0}, {**BODY, 'messages': []}, {**BODY, 'tools': []},
    {**BODY, 'images': []}, {**BODY, 'audio': 'private'}, {**BODY, 'video': 'private'},
    {**BODY, 'context': [1]}, {**BODY, 'system': 'override'}, {**BODY, 'options': {}},
    {**BODY, 'raw': False}, {**BODY, 'format': 'json'}, {**BODY, 'keep_alive': '0'},
    {**BODY, 'think': False}, {**BODY, 'model': 'unregistered'}, [],
])
def test_unsupported_protocol_input_is_rejected_before_dispatch(tmp_path, payload):
    client, _, _, api, _ = consumer(tmp_path)
    response = client.post('/api/generate', json=payload)
    assert response.status_code in (404, 422)
    assert isinstance(response.json()['error'], str)
    assert api.requests == []


def test_single_user_chat_returns_native_message_shape(tmp_path):
    client, _, _, api, _ = consumer(tmp_path)
    response = client.post('/api/chat', json={'model': BODY['model'], 'stream': False,
                          'messages': [{'role': 'user', 'content': BODY['prompt']}]})
    assert response.status_code == 200
    assert response.json()['message'] == {'role': 'assistant', 'content': '2'}
    assert 'response' not in response.json()
    assert len(api.requests) == 1


@pytest.mark.parametrize('messages', [[], [{'role': 'system', 'content': 'private'}],
    [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'earlier'}],
    [{'role': 'user', 'content': 'text', 'images': ['private']}],
    [{'role': 'user', 'content': [{'type': 'text', 'text': 'text'}]}]])
def test_chat_history_media_and_nontext_are_explicitly_rejected(tmp_path, messages):
    client, _, _, api, _ = consumer(tmp_path)
    response = client.post('/api/chat', json={'model': BODY['model'], 'messages': messages, 'stream': False})
    assert response.status_code == 422
    assert response.json()['error'] == 'only_single_user_text_supported'
    assert api.requests == []


@pytest.mark.parametrize('passed,status,error', [(False, 'failed', 'public_validation_failed'),
                                                (None, 'unverifiable', 'public_validation_unavailable')])
def test_nonempty_unvalidated_answer_is_not_protocol_success(tmp_path, passed, status, error):
    from test_engine import ArithmeticEnvironment
    class Env(ArithmeticEnvironment):
        def validate(self, answer):
            return {'passed': passed}
    client, _, _, api, _ = consumer(tmp_path, environment=Env)
    response = client.post('/api/generate', json=BODY)
    assert response.status_code == 422
    assert response.json()['error'] == error
    assert response.json()['cloudyfan']['status'] == status
    assert response.json()['cloudyfan']['costs']['complete_api_usd'] == '0.0201'
    assert 'response' not in response.json()
    result = client.get('/v1/tasks/ollama-1/result').json()
    assert result['final_public_pass'] is passed
    assert len(api.requests) == 3


def test_interrupted_validation_requires_explicit_resume_not_repeated_post(tmp_path):
    from test_engine import ArithmeticEnvironment
    class Env(ArithmeticEnvironment):
        count = 0
        def validate(self, answer):
            type(self).count += 1
            if self.count == 1:
                raise RuntimeError('private validation interruption')
            return super().validate(answer)
    client, _, _, api, workspaces = consumer(tmp_path, environment=Env)
    response = client.post('/api/generate', json=BODY)
    assert response.status_code == 503
    assert response.json()['cloudyfan']['status'] == 'engineering_interrupted'
    repeated = client.post('/api/generate', json=BODY)
    assert repeated.status_code == 503
    assert len(workspaces) == 1
    assert client.get('/v1/tasks/ollama-1').json()['status'] == 'engineering_interrupted'
    assert client.post('/v1/tasks/ollama-1/resume', json={}).json()['status'] == 'passed'
    assert client.get('/v1/tasks/ollama-1/result').json()['answer'] == '2'
    assert len(api.requests) == 1 and len(workspaces) == 2


def test_lost_response_reconnect_queries_original_run_without_new_dispatch(tmp_path):
    client, service, _, api, _ = consumer(tmp_path)
    app = OllamaApp(service, models={BODY['model']: 'arithmetic'}, authenticate=lambda _: 'admin')
    def lost_response(environ, start_response):
        list(app(environ, start_response))
        raise httpx.ReadTimeout('fixture lost transport response')
    with httpx.Client(transport=httpx.WSGITransport(lost_response), base_url='http://fixture',
                       headers={'X-CloudyFan-Run-ID': 'ollama-1'}) as lost:
        with pytest.raises(httpx.ReadTimeout):
            lost.post('/api/generate', json=BODY)
    assert client.get('/v1/tasks/ollama-1').json()['status'] == 'passed'
    assert client.get('/v1/tasks/ollama-1/result').json()['answer'] == '2'
    assert client.post('/api/generate', json=BODY).json()['response'] == '2'
    assert len(api.requests) == 1
    changed = client.post('/api/generate', json={**BODY, 'prompt': 'different task'})
    assert changed.status_code == 409 and changed.json()['error'] == 'frozen_task_changed'
    assert len(api.requests) == 1


def test_upstream_timeout_unknown_charge_is_queryable_but_not_retried(tmp_path):
    posts = []
    def timeout(request):
        posts.append(request.method)
        raise httpx.ReadTimeout('fixture potentially paid timeout')
    client, _, _, _, _ = consumer(tmp_path, api=timeout)
    first = client.post('/api/generate', json=BODY)
    assert first.status_code == 409
    assert first.json()['cloudyfan']['status'] == 'accounting_interrupted'
    assert first.json()['cloudyfan']['costs']['complete_api_usd'] is None
    assert client.get('/v1/tasks/ollama-1').json()['status'] == 'accounting_interrupted'
    assert client.post('/api/generate', json=BODY).status_code == 409
    assert client.post('/v1/tasks/ollama-1/resume', json={}).status_code == 409
    assert posts == ['POST']


@pytest.mark.parametrize('path', ['/api/tags', '/api/chat', '/api/generate', '/api/version'])
def test_native_endpoints_authenticate_before_any_work(tmp_path, path):
    client, _, _, api, _ = consumer(tmp_path)
    response = client.post(path, json=BODY, headers={'Authorization': 'Bearer wrong'})
    assert response.status_code == 401 and response.json() == {'error': 'unauthorized'}
    assert api.requests == []


def test_native_input_integrity_run_identity_and_contract_registration(tmp_path):
    client, service, _, api, _ = consumer(tmp_path)
    client.headers.pop('X-CloudyFan-Run-ID')
    assert client.post('/api/generate', json=BODY).status_code == 422
    client.headers['X-CloudyFan-Run-ID'] = 'ollama-1'
    duplicate = '{"model":"a","model":"b","prompt":"text","stream":false}'
    assert client.post('/api/generate', content=duplicate,
                        headers={'Content-Type': 'application/json'}).json()['error'] == 'invalid_json'
    assert client.post('/api/generate', content='not json',
                        headers={'Content-Type': 'text/plain'}).status_code == 415
    assert client.post('/api/generate', content='x' * 65537,
                        headers={'Content-Type': 'application/json'}).status_code == 413
    with pytest.raises(ValueError, match='registered validation'):
        OllamaApp(service, models={'free-chat': 'no-public-validator'}, authenticate=lambda _: 'admin')
    assert api.requests == []


def test_native_audit_and_errors_are_redacted(tmp_path):
    _, service, _, api, _ = consumer(tmp_path)
    events = []
    def broken_auth(environ):
        raise RuntimeError('SECRET credential private stack')
    app = OllamaApp(service, models={BODY['model']: 'arithmetic'}, authenticate=broken_auth, audit=events.append)
    with httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture') as caller:
        response = caller.get('/api/tags')
    assert response.status_code == 500 and response.json() == {'error': 'service_interrupted'}
    assert events == [{'event': 'ollama_protocol_request', 'http_status': 500}]
    assert api.requests == []


@pytest.mark.parametrize('first,tiers,count', [('2', ['open_source'], 1),
                                             ('3', ['open_source', 'high'], 4)])
def test_native_result_preserves_task_provenance_without_extra_generation(tmp_path, first, tiers, count):
    from test_engine import API
    client, _, _, api, _ = consumer(tmp_path, api=API(first=first, choices=('mid', 'high')))
    response = client.post('/api/generate', json=BODY)
    provenance = response.json()['cloudyfan']['provenance']
    assert [attempt['tier'] for attempt in provenance['attempts']] == tiers
    assert client.get('/v1/tasks/ollama-1/result').json()['provenance'] == provenance
    assert client.post('/api/generate', json=BODY).json()['cloudyfan']['provenance'] == provenance
    assert len(api.requests) == count
