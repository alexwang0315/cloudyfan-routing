"""Pinned official SDK -> real WSGI app/Core -> mocked model upstream.

No SDK method or CloudyFan collaborator is replaced. Transport faults are
injected only at the downstream HTTP boundary. Socket access remains forbidden.
"""
from importlib.metadata import version
from types import SimpleNamespace

import httpx
import ollama
import pytest

from cloudyfan_jev.http_api import TaskService
from cloudyfan_jev.identity import BearerAuthenticator, IdentityStore
from cloudyfan_jev.ollama_api import OllamaApp
from test_engine import ArithmeticEnvironment
from test_http_api import service_client


ALIAS = 'cloudyfan/arithmetic:v1'
PROMPT = 'Return 1+1'
SDK_VERSION = '0.6.3'


@pytest.fixture(autouse=True)
def isolated_sdk_environment(monkeypatch):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    monkeypatch.delenv('OLLAMA_HOST', raising=False)


def sdk_system(tmp_path, **kwargs):
    assert version('ollama') == SDK_VERSION
    original, seed, runner, api, workspaces = service_client(tmp_path, **kwargs)
    original.close()
    clock = [1000.0]
    identity = IdentityStore(tmp_path / 'identity' / 'tokens.sqlite3',
                             pepper=b'sdk-fixture-pepper-never-a-real-secret', clock=lambda: clock[0])
    identity.create_principal('admin')
    credential = identity.issue('admin', scopes=['models:read', 'tasks:read', 'tasks:write'], expires_at=2000)
    service = TaskService(tmp_path / 'sdk-service', contracts=seed.contracts,
                          allowed_principals={'admin'}, principal_active=identity.is_active)
    app = OllamaApp(service, models={ALIAS: 'arithmetic'}, authenticate=BearerAuthenticator(identity),
                    clock=lambda: '2026-10-18T10:00:00Z')
    requests = []

    def entry(environ, start_response):
        requests.append({'method': environ['REQUEST_METHOD'], 'path': environ['PATH_INFO'],
                         'run_id': environ.get('HTTP_X_CLOUDYFAN_RUN_ID')})
        return app(environ, start_response)

    def client(run_id='sdk-1', *, token=credential.token, application=entry):
        headers = {}
        if run_id is not None:
            headers['X-CloudyFan-Run-ID'] = run_id
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        return ollama.Client(host='https://sdk.fixture.invalid', headers=headers, trust_env=False,
                             follow_redirects=False, timeout=1.0, transport=httpx.WSGITransport(application))

    def query(run_id='sdk-1', *, result=False):
        with httpx.Client(base_url='https://sdk.fixture.invalid', transport=httpx.WSGITransport(entry),
                          trust_env=False, headers={'Authorization': 'Bearer ' + credential.token}) as caller:
            return caller.get('/v1/tasks/' + run_id + ('/result' if result else ''))

    def resume(run_id='sdk-1'):
        with httpx.Client(base_url='https://sdk.fixture.invalid', transport=httpx.WSGITransport(entry),
                          trust_env=False, headers={'Authorization': 'Bearer ' + credential.token}) as caller:
            return caller.post('/v1/tasks/' + run_id + '/resume', json={})

    return SimpleNamespace(client=client, query=query, resume=resume, service=service, runner=runner, api=api,
                           identity=identity, credential=credential, app=entry, requests=requests,
                           workspaces=workspaces, clock=clock)


def test_official_sdk_lists_alias_and_generates_publicly_validated_text(tmp_path):
    system = sdk_system(tmp_path)
    with system.client() as client:
        listed = client.list()
        assert isinstance(listed, ollama.ListResponse)
        assert [model.model for model in listed.models] == [ALIAS]
        response = client.generate(model=ALIAS, prompt=PROMPT, stream=False)
    assert isinstance(response, ollama.GenerateResponse)
    assert response.model == ALIAS and response.response == '2' and response.done is True
    # The official response model discards extensions. Read complete accounting
    # through the already registered task API, never invent native SDK fields.
    assert 'cloudyfan' not in response.model_dump()
    result = system.query(result=True)
    assert result.status_code == 200
    assert result.json()['answer'] == response.response
    assert result.json()['costs']['complete_api_usd'] == '0.01'
    assert len(system.api.requests) == 1


def test_official_sdk_single_user_chat_preserves_alias_and_task_provenance(tmp_path):
    system = sdk_system(tmp_path)
    with system.client(run_id='sdk-chat') as client:
        response = client.chat(model=ALIAS, messages=[{'role': 'user', 'content': PROMPT}], stream=False)
    assert isinstance(response, ollama.ChatResponse)
    assert response.model == ALIAS and response.message.role == 'assistant'
    assert response.message.content == '2' and response.done is True
    result = system.query('sdk-chat', result=True).json()
    assert result['answer'] == response.message.content
    assert result['final_public_pass'] is True
    assert result['provenance']['attempts'][0]['calls'][0]['actual_model'] == 'example/open_source'
    assert len(system.api.requests) == 1


@pytest.mark.parametrize('denial', ['missing', 'invalid', 'revoked', 'disabled', 'expired', 'read-only'])
def test_official_sdk_authentication_failure_is_explicit_and_cannot_dispatch(tmp_path, denial):
    system = sdk_system(tmp_path)
    token = system.credential.token
    if denial == 'missing':
        token = None
    elif denial == 'invalid':
        token = system.credential.token[:-1]  # Malformed disposable credential; never a stored secret.
    elif denial == 'revoked':
        system.identity.revoke(system.credential.id)
    elif denial == 'disabled':
        system.identity.set_active('admin', False)
    elif denial == 'expired':
        system.clock[0] = 2000.0
    else:
        token = system.identity.issue('admin', scopes=['tasks:read'], expires_at=2000).token
    with system.client(token=token) as client, pytest.raises(ollama.ResponseError) as caught:
        client.generate(model=ALIAS, prompt=PROMPT, stream=False)
    assert caught.value.status_code == 401 and caught.value.error == 'unauthorized'
    assert system.credential.token not in str(caught.value)
    assert system.api.requests == [] and system.workspaces == []


def test_official_sdk_model_listing_and_generation_require_distinct_server_scopes(tmp_path):
    system = sdk_system(tmp_path)
    reader = system.identity.issue('admin', scopes=['models:read'], expires_at=2000)
    writer = system.identity.issue('admin', scopes=['tasks:write'], expires_at=2000)
    with system.client(token=reader.token) as client:
        assert client.list().models[0].model == ALIAS
        with pytest.raises(ollama.ResponseError) as denied:
            client.generate(model=ALIAS, prompt=PROMPT, stream=False)
        assert denied.value.status_code == 401
    assert system.api.requests == []
    with system.client(token=writer.token) as client:
        with pytest.raises(ollama.ResponseError) as denied:
            client.list()
        assert denied.value.status_code == 401
        assert client.generate(model=ALIAS, prompt=PROMPT, stream=False).response == '2'
    assert len(system.api.requests) == 1


@pytest.mark.parametrize('run_id', [None, '', '../unsafe'])
def test_official_sdk_requires_a_stable_caller_owned_run_id(tmp_path, run_id):
    system = sdk_system(tmp_path)
    with system.client(run_id=run_id) as client, pytest.raises(ollama.ResponseError) as caught:
        client.generate(model=ALIAS, prompt=PROMPT, stream=False)
    assert caught.value.status_code == 422 and caught.value.error == 'invalid_run_id'
    assert system.api.requests == []


def test_official_sdk_unknown_alias_is_an_error_without_upstream_side_effects(tmp_path):
    system = sdk_system(tmp_path)
    with system.client() as client, pytest.raises(ollama.ResponseError) as caught:
        client.generate(model='unregistered:model', prompt=PROMPT, stream=False)
    assert caught.value.status_code == 404 and caught.value.error == 'model_not_registered'
    assert system.api.requests == []


@pytest.mark.parametrize('passed,expected_error,expected_status', [
    (False, 'public_validation_failed', 'failed'),
    (None, 'public_validation_unavailable', 'unverifiable'),
])
def test_official_sdk_never_treats_failed_or_unknown_public_validation_as_success(
        tmp_path, passed, expected_error, expected_status):
    class Environment(ArithmeticEnvironment):
        def validate(self, answer):
            return {'passed': passed}
    system = sdk_system(tmp_path, environment=Environment)
    with system.client() as client, pytest.raises(ollama.ResponseError) as caught:
        client.generate(model=ALIAS, prompt=PROMPT, stream=False)
    assert caught.value.status_code == 422 and caught.value.error == expected_error
    result = system.query(result=True).json()
    assert result['status'] == expected_status and result['final_public_pass'] is passed
    assert result['costs']['complete_api_usd'] == '0.0201'
    assert len(system.api.requests) == 3  # generation + classification + repair, all mocked


def test_official_sdk_surfaces_budget_denial_without_model_dispatch(tmp_path):
    system = sdk_system(tmp_path, total='0.001')
    with system.client() as client, pytest.raises(ollama.ResponseError) as caught:
        client.generate(model=ALIAS, prompt=PROMPT, stream=False)
    assert caught.value.status_code == 403 and caught.value.error == 'budget_admission_denied'
    assert system.api.requests == []


def test_official_sdk_validation_interruption_requires_explicit_task_resume(tmp_path):
    class InterruptOnce(ArithmeticEnvironment):
        attempts = 0

        def validate(self, answer):
            type(self).attempts += 1
            if self.attempts == 1:
                raise RuntimeError('SECRET validation implementation details')
            return super().validate(answer)

    system = sdk_system(tmp_path, environment=InterruptOnce)
    with system.client() as client:
        for _ in range(2):
            with pytest.raises(ollama.ResponseError) as caught:
                client.generate(model=ALIAS, prompt=PROMPT, stream=False)
            assert caught.value.status_code == 503
            assert 'SECRET' not in str(caught.value)
    assert system.query().json()['status'] == 'engineering_interrupted'
    assert len(system.api.requests) == 1 and len(system.workspaces) == 1
    assert system.resume().json()['status'] == 'passed'
    assert system.query(result=True).json()['answer'] == '2'
    assert len(system.api.requests) == 1 and len(system.workspaces) == 2


@pytest.mark.parametrize('method', ['generate', 'chat'])
def test_official_sdk_lost_completed_response_recovers_original_run_without_resend(tmp_path, method):
    system = sdk_system(tmp_path)

    def lost_response(environ, start_response):
        list(system.app(environ, start_response))
        raise httpx.ReadTimeout('synthetic response lost after completed task')

    def invoke(client, prompt=PROMPT):
        if method == 'generate':
            return client.generate(model=ALIAS, prompt=prompt, stream=False).response
        return client.chat(model=ALIAS, messages=[{'role': 'user', 'content': prompt}],
                           stream=False).message.content

    with system.client(application=lost_response) as client, pytest.raises(httpx.ReadTimeout):
        invoke(client)
    # One downstream attempt: the real SDK did not automatically retry the POST.
    assert [r['path'] for r in system.requests] == ['/api/' + method]
    assert len(system.api.requests) == 1
    assert system.query().json()['status'] == 'passed'
    original = system.query(result=True).json()
    assert original['answer'] == '2' and original['costs']['complete_api_usd'] == '0.01'
    with system.client() as reconnected:
        assert invoke(reconnected) == original['answer']  # explicit lookup POST, identical run ID
        with pytest.raises(ollama.ResponseError) as changed:
            invoke(reconnected, prompt='different frozen task')
    assert changed.value.status_code == 409 and changed.value.error == 'frozen_task_changed'
    assert len(system.api.requests) == 1
    assert system.query(result=True).json()['provenance'] == original['provenance']


def test_official_sdk_connect_failure_has_no_implicit_retry_and_keeps_caller_run_id(tmp_path):
    system = sdk_system(tmp_path)
    attempts = []

    def cannot_connect(environ, start_response):
        attempts.append(environ.get('HTTP_X_CLOUDYFAN_RUN_ID'))
        raise httpx.ConnectError('synthetic failure before server admission')

    with system.client(application=cannot_connect) as client, pytest.raises(ConnectionError):
        client.generate(model=ALIAS, prompt=PROMPT, stream=False)
    assert attempts == ['sdk-1'] and system.api.requests == []
    assert system.query().status_code == 404
    with system.client() as reconnected:
        assert reconnected.generate(model=ALIAS, prompt=PROMPT, stream=False).response == '2'
    assert len(system.api.requests) == 1


def test_official_sdk_unknown_upstream_charge_stays_unknown_across_reconnect_and_resume(tmp_path):
    posts = []

    def upstream_timeout(request):
        posts.append(request.method)
        raise httpx.ReadTimeout('synthetic upstream may already have billed')

    system = sdk_system(tmp_path, api=upstream_timeout)
    for _ in range(2):
        with system.client() as client, pytest.raises(ollama.ResponseError) as caught:
            client.generate(model=ALIAS, prompt=PROMPT, stream=False)
        assert caught.value.status_code == 409
        assert 'synthetic' not in str(caught.value)
    state = system.query().json()
    assert state['status'] == 'accounting_interrupted'
    assert state['costs']['complete_api_usd'] is None and state['costs']['unknown_events'] == 1
    assert system.resume().status_code == 409
    assert posts == ['POST']


@pytest.mark.parametrize('method,parameters,expected_error', [
    ('generate', {'stream': True}, 'only_explicit_nonstream_text_supported'),
    ('chat', {'stream': True}, 'only_explicit_nonstream_text_supported'),
    ('generate', {'images': [b'synthetic image']}, 'unsupported_request_fields'),
    ('generate', {'options': {'temperature': 0.1}}, 'unsupported_request_fields'),
    ('generate', {'think': True}, 'unsupported_request_fields'),
    ('generate', {'keep_alive': '5m'}, 'unsupported_request_fields'),
    ('generate', {'format': 'json'}, 'unsupported_request_fields'),
    ('generate', {'system': 'override'}, 'unsupported_request_fields'),
    ('generate', {'template': 'override'}, 'unsupported_request_fields'),
    ('generate', {'suffix': 'extra'}, 'unsupported_request_fields'),
    ('generate', {'raw': True}, 'unsupported_request_fields'),
    ('generate', {'context': [1]}, 'unsupported_request_fields'),
    ('generate', {'logprobs': True}, 'unsupported_request_fields'),
    ('chat', {'tools': [{'type': 'function', 'function': {'name': 'unsupported',
                        'parameters': {'type': 'object', 'properties': {}}}}]}, 'unsupported_request_fields'),
    ('chat', {'messages': [{'role': 'user', 'content': 'first'},
                          {'role': 'assistant', 'content': 'previous'}]}, 'only_single_user_text_supported'),
    ('chat', {'messages': [{'role': 'user', 'content': PROMPT, 'images': ['ZmFrZQ==']}]},
     'only_single_user_text_supported'),
])
def test_official_sdk_unsupported_capabilities_raise_explicit_errors_before_dispatch(
        tmp_path, method, parameters, expected_error):
    system = sdk_system(tmp_path)
    with system.client() as client, pytest.raises(ollama.ResponseError) as caught:
        options = {'stream': False, **parameters}
        if method == 'generate':
            response = client.generate(model=ALIAS, prompt=PROMPT, **options)
        else:
            options.setdefault('messages', [{'role': 'user', 'content': PROMPT}])
            response = client.chat(model=ALIAS, **options)
        if options['stream']:
            next(response)  # The official SDK defers a streaming HTTP request until iteration.
    assert caught.value.status_code == 422 and caught.value.error == expected_error
    assert len(system.requests) == 1 and system.api.requests == [] and system.workspaces == []


@pytest.mark.parametrize('method,parameters', [
    ('show', {'model': ALIAS}),
    ('embed', {'model': ALIAS, 'input': PROMPT}),
    ('pull', {'model': ALIAS}),
])
def test_official_sdk_other_native_endpoints_fail_closed_without_model_actions(tmp_path, method, parameters):
    system = sdk_system(tmp_path)
    with system.client() as client, pytest.raises(ollama.ResponseError) as caught:
        getattr(client, method)(**parameters)
    # The production BearerAuthenticator has no scope mapping for these routes.
    # It denies before dispatch; it does not enable unsupported SDK methods.
    assert caught.value.status_code == 401 and caught.value.error == 'unauthorized'
    assert len(system.requests) == 1 and system.api.requests == [] and system.workspaces == []
