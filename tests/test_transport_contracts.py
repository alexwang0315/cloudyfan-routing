"""Shared task scenarios through real authenticated WSGI adapters; upstream is mocked."""
from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest

from cloudyfan_jev.budget import EnforcedBound, ToolBound, ToolReceipt
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.http_api import HTTPApp, RegisteredContract, TaskService
from cloudyfan_jev.ollama_api import OllamaApp
from cloudyfan_jev.privacy import ExecutionPolicy
from cloudyfan_jev.scheduler import QueueHTTPApp, QueueLimits, QueueScheduler
from test_budget import controlled
from test_engine import API, ArithmeticEnvironment

PROMPT = 'Return 1+1'
TASK = {'run_id': 'contract-1', 'contract_id': 'arithmetic', 'prompt': PROMPT}
ALIAS = 'cloudyfan/arithmetic:v1'
PRIVATE = 'PRIVATE_CAPABILITY_AND_ACCOUNT_DATA'


@dataclass
class Transport:
    kind: str
    client: httpx.Client
    service: TaskService
    runner: object
    api: API
    queue: QueueScheduler | None

    def submit(self, *, owner='alice', prompt=PROMPT):
        headers = {'Authorization': 'Bearer ' + owner, 'X-CloudyFan-Run-ID': TASK['run_id']}
        if self.kind.startswith('ollama'):
            payload = {'model': ALIAS, 'stream': False}
            if self.kind == 'ollama_chat':
                payload['messages'] = [{'role': 'user', 'content': prompt}]
            else:
                payload['prompt'] = prompt
            return self.client.post('/api/' + self.kind.removeprefix('ollama_'), json=payload, headers=headers)
        return self.client.post('/v1/tasks', json={**TASK, 'prompt': prompt}, headers=headers)

    def dispatch(self):
        if self.queue:
            return self.queue.dispatch_next()

    def status(self, owner='alice'):
        return self.client.get('/v1/tasks/contract-1', headers={'Authorization': 'Bearer ' + owner})

    def result(self, owner='alice'):
        return self.client.get('/v1/tasks/contract-1/result', headers={'Authorization': 'Bearer ' + owner})

    def resume(self, owner='alice'):
        return self.client.post('/v1/tasks/contract-1/resume', json={},
                                headers={'Authorization': 'Bearer ' + owner})


@pytest.fixture(params=['http', 'ollama_generate', 'ollama_chat', 'queue'])
def transport(request, tmp_path):
    """One registered contract, identity policy and fixture API for every protocol."""
    clients = []

    def create(*, api=None, endpoint=None, environment=ArithmeticEnvironment, tools=False):
        api = api or API(first='2', choices=('mid', 'high'))
        runner = controlled(tmp_path / 'runner', endpoint or api,
            limits=Limits(max_turns=2, max_tool_calls=1),
            tools={'lookup': ToolBound(EnforcedBound('.05', 'fixture tool bound'), True)} if tools else {})
        runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
            classifier='cloud:typesafe', tools={'lookup': 'cloud:fixture'} if tools else {})
        for tier, profile in runner.core.profiles.items():
            profile['execution_location'] = 'cloud:fixture'
            profile['capability_card'] = {'deployment_notes': PRIVATE,
                'score_selection': {'snapshot_id': 'snapshot-1', 'policy_id': 'policy-1', 'config_id': tier,
                                    'requirements': {'description': PRIVATE}},
                'benchmark_evidence': {'source': 'fixture', 'source_version': 'v1', 'metric': 'arithmetic',
                                       'raw': PRIVATE}}
        service = TaskService(tmp_path / 'service', allowed_principals={'alice', 'bob'},
            contracts={'arithmetic': RegisteredContract('public-v1', runner, lambda _: environment(),
                                                        tool_names=('lookup',) if tools else ())})
        def authenticate(environ):
            return {'Bearer alice': 'alice', 'Bearer bob': 'bob'}.get(environ.get('HTTP_AUTHORIZATION'))
        queue = None
        if request.param == 'queue':
            queue = QueueScheduler(service, tmp_path / 'queue.sqlite', limits=QueueLimits(4, 1, 2, 30),
                                   clock=lambda: 1000.0, measurement_kind='synthetic')
            app = QueueHTTPApp(queue, authenticate=authenticate)
        elif request.param.startswith('ollama'):
            app = OllamaApp(service, models={ALIAS: 'arithmetic'}, authenticate=authenticate,
                            clock=lambda: '2026-10-18T10:00:00Z')
        else:
            app = HTTPApp(service, authenticate=authenticate)
        client = httpx.Client(transport=httpx.WSGITransport(app), base_url='http://fixture')
        clients.extend([client, runner.client.http])
        return Transport(request.param, client, service, runner, api, queue)

    yield create
    for client in clients:
        client.close()


class LookupEnvironment(ArithmeticEnvironment):
    def tools_schema(self):
        return [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]

    def call(self, name, arguments):
        assert name == 'lookup'
        return ToolReceipt({'value': '2'}, '.04', PRIVATE, True)


def assert_provenance(result, tiers, calls_per_attempt):
    provenance = result['provenance']
    assert provenance['contract_version'] == 'public-v1'
    assert len(provenance['config_sha256']) == 64
    assert provenance['engine_version'] and provenance['routing_version']
    assert [attempt['tier'] for attempt in provenance['attempts']] == tiers
    calls = []
    for attempt, tier, count in zip(provenance['attempts'], tiers, calls_per_attempt, strict=True):
        assert len(attempt['calls']) == count
        for call in attempt['calls']:
            assert call['actual_model'] == 'example/' + tier
            assert call['actual_provider'] == 'Example'
            assert call['configuration']['model_version'] == 'example/' + tier
            assert call['configuration']['effort'] == 'low'
            assert call['configuration']['execution_location'] == 'cloud:fixture'
            assert call['score'] == {'snapshot_id': 'snapshot-1', 'policy_id': 'policy-1', 'config_id': tier,
                                    'source': 'fixture', 'source_version': 'v1', 'metric': 'arithmetic'}
            calls.append(call)
    classifiers = provenance['classifier_calls']
    assert len(classifiers) == (2 if len(tiers) == 2 else 0)
    for call in classifiers:
        assert call['actual_model'] == call['configuration']['model_version'] == 'typesafe/jev-1.13'
        assert call['actual_provider'] == 'TypeSafe'
        assert call['configuration']['effort'] is None
        assert all(value is None for value in call['score'].values())
    calls += classifiers
    assert len({call['generation_id'] for call in calls}) == len(calls)
    assert len({call['call_id'] for call in calls}) == len(calls)
    assert all(call['generation_id'].startswith('gen-test-') for call in calls)


@pytest.mark.parametrize('scenario', ['first_pass', 'tool_then_routed_repair'])
def test_completed_contract_preserves_provenance_full_cost_and_no_repeat(transport, scenario):
    repair = scenario == 'tool_then_routed_repair'
    messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'lookup-1', 'type': 'function',
            'function': {'name': 'lookup', 'arguments': '{}'}}]},
        {'role': 'assistant', 'content': '3'}, {'role': 'assistant', 'content': '2'}]
    t = transport(api=API(first='2', choices=('mid', 'high'), messages=messages if repair else None),
                  environment=LookupEnvironment if repair else ArithmeticEnvironment, tools=repair)
    t.runner.store.import_history(PRIVATE, cost_usd='.08', receipt_id=PRIVATE + '-history', verified=True)
    submitted = t.submit()
    assert submitted.status_code == (202 if t.queue else 200)
    if t.queue:
        assert t.api.requests == []
        assert t.dispatch()['status'] == 'passed'
    result = t.result()
    assert result.status_code == 200
    body = result.json()
    assert body['answer'] == '2' and body['status'] == 'passed'
    assert body['first_public_pass'] is (not repair) and body['final_public_pass'] is True
    # Independent arithmetic: 3 generation turns + 2 classifiers + 1 billed tool.
    expected_cost = Decimal('.01') if not repair else Decimal('.01') * 3 + Decimal('.0001') * 2 + Decimal('.04')
    assert Decimal(body['costs']['complete_api_usd']) == expected_cost
    assert body['costs']['unknown_events'] == 0 and body['costs']['host_and_human_usd'] is None
    assert_provenance(body, ['open_source', 'high'] if repair else ['open_source'], [2, 1] if repair else [1])
    assert PRIVATE not in result.text and PROMPT not in result.text
    count = 5 if repair else 1
    assert len(t.api.requests) == count
    repeated = t.submit()
    assert repeated.status_code == 200
    if t.kind.startswith('ollama'):
        native = repeated.json()
        assert native['model'] == ALIAS and native['done'] is True
        assert native['cloudyfan']['provenance'] == body['provenance']
        assert native['cloudyfan']['costs'] == body['costs']
        if t.kind == 'ollama_chat':
            assert native['message'] == {'role': 'assistant', 'content': '2'} and 'response' not in native
        else:
            assert native['response'] == '2' and 'message' not in native
    assert t.status().json()['status'] == 'passed'
    assert t.resume().status_code == (404 if t.queue else 200)
    assert t.result().json()['provenance'] == body['provenance']
    assert t.dispatch() is None
    assert len(t.api.requests) == count
    assert Decimal(t.runner.store.report()['known_usd']) == expected_cost + Decimal('.08')


def test_unknown_charge_survives_queries_repeats_and_resume_without_new_post(transport):
    methods = []
    def timeout(request):
        methods.append(request.method)
        raise httpx.ReadTimeout('possibly paid synthetic timeout')
    t = transport(endpoint=timeout)
    first = t.submit()
    assert first.status_code == (202 if t.queue else 409)
    t.dispatch()
    for _ in range(2):
        status = t.status().json()
        assert status['status'] == 'accounting_interrupted'
        assert status['costs']['complete_api_usd'] is None
        assert status['costs']['unknown_events'] == 1
        assert t.result().status_code == 409
        repeated = t.submit()
        assert repeated.status_code == (200 if t.queue else 409)
        assert t.resume().status_code == (404 if t.queue else 409)
        assert t.dispatch() is None
    assert methods == ['POST']
    assert t.runner.store.report()['reserved_usd'] != '0'


def test_owner_isolation_includes_result_status_resume_and_same_public_run_id(transport):
    t = transport()
    t.submit()
    t.dispatch()
    for response in (t.status('bob'), t.result('bob'), t.resume('bob')):
        assert response.status_code == 404
        assert 'provenance' not in response.text and 'example/' not in response.text
    assert len(t.api.requests) == 1
    assert t.submit(owner='bob').status_code == (202 if t.queue else 200)
    t.dispatch()
    alice, bob = t.result().json(), t.result('bob').json()
    assert alice['costs']['complete_api_usd'] == bob['costs']['complete_api_usd'] == '0.01'
    alice_call = alice['provenance']['attempts'][0]['calls'][0]['call_id']
    bob_call = bob['provenance']['attempts'][0]['calls'][0]['call_id']
    assert alice_call != bob_call and bob_call not in str(alice)
    assert len(t.api.requests) == 2
    assert t.client.get('/v1/tasks/contract-1/result', headers={'Authorization': 'Bearer invalid'}).status_code == 401


@pytest.mark.parametrize('change', ['task', 'configuration'])
def test_frozen_task_and_configuration_cannot_be_silently_replaced(transport, change):
    t = transport()
    t.submit()
    t.dispatch()
    if change == 'configuration':
        t.runner.core.profiles['high']['effort'] = 'high'
        repeated = t.submit()
        assert t.result().status_code == 409
        assert t.resume().status_code == (404 if t.queue else 409)
    else:
        repeated = t.submit(prompt='A different task')
        assert t.result().json()['answer'] == '2'
    assert repeated.status_code == (200 if t.queue and change == 'configuration' else 409)
    assert len(t.api.requests) == 1 and t.dispatch() is None


def test_unavailable_public_validation_is_never_protocol_success(transport):
    class Unknown(ArithmeticEnvironment):
        def validate(self, answer):
            return {'passed': None}
    t = transport(environment=Unknown)
    response = t.submit()
    assert response.status_code == (202 if t.queue else 422 if t.kind.startswith('ollama') else 200)
    t.dispatch()
    result = t.result().json()
    assert result['status'] == 'unverifiable' and result['final_public_pass'] is None
    assert result['costs']['complete_api_usd'] == '0.0202'
    if t.kind.startswith('ollama'):
        assert response.json()['error'] == 'public_validation_unavailable'
        assert 'response' not in response.json() and 'message' not in response.json()
    assert len(t.api.requests) == 4


def test_validation_interruption_preserves_answer_and_explicit_resume_policy(transport):
    class InterruptOnce(ArithmeticEnvironment):
        count = 0
        def validate(self, answer):
            type(self).count += 1
            if self.count == 1:
                raise RuntimeError('private synthetic validation interruption')
            return super().validate(answer)
    t = transport(environment=InterruptOnce)
    response = t.submit()
    assert response.status_code == (202 if t.queue else 503)
    t.dispatch()
    assert t.status().json()['status'] == 'engineering_interrupted'
    assert t.status().json()['costs']['complete_api_usd'] == '0.01'
    resumed = t.resume()
    if t.queue:
        assert resumed.status_code == 404
        assert t.dispatch() is None
        assert t.result().status_code == 409
    else:
        assert resumed.status_code == 200 and resumed.json()['status'] == 'passed'
        assert t.result().json()['answer'] == '2'
    assert len(t.api.requests) == 1
