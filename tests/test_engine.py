import copy
import json
from decimal import Decimal

import httpx
import pytest

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits


TASK = {'prompt': 'Return only the integer sum of 1 and 1.'}


class API:
    def __init__(self, first='3', choices=('low',), messages=None):
        self.first, self.choices, self.messages = first, iter(choices), iter(messages) if messages else None
        self.requests = []
        self.generation_count = 0

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(copy.deepcopy(p))
        i = len(self.requests)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(p['questions']))
            choice = next(self.choices)
            return httpx.Response(200, json={'id': f'gen-test-{i}', 'model': p['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0001, 'input_tokens': 10, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': choice,
                    'probabilities': {k: .8 if k == choice else .2 for k in p['questions'][stage]['criteria']}}}})
        self.generation_count += 1
        msg = next(self.messages) if self.messages else {'role': 'assistant', 'content': self.first if self.generation_count == 1 else '2'}
        return httpx.Response(200, json={'id': f'gen-test-{i}', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': .01, 'prompt_tokens': 5, 'completion_tokens': 1},
            'choices': [{'message': msg, 'finish_reason': 'stop'}]})


def core(tmp_path, api, limits=None):
    http = httpx.Client(transport=httpx.MockTransport(api))
    profiles, decision = example_config()
    return Core(Client(http, 'mock-only-key', tmp_path), profiles, decision, limits=limits)


@pytest.mark.parametrize('choices,tier,count', [(('low',), 'low', 3), (('mid', 'mid'), 'mid', 4), (('mid', 'high'), 'high', 4)])
def test_complete_tree_no_generation_between_decisions(tmp_path, choices, tier, count):
    api = API(choices=choices)
    result = core(tmp_path, api).run('tree', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == ['open_source', tier]
    assert len(api.requests) == count
    assert [p['model'] for p in api.requests] == ['example/open_source'] + ['typesafe/jev-1.13'] * len(choices) + ['example/' + tier]
    assert Decimal(result['costs']['api_usd']) == Decimal('.02') + Decimal('.0001') * len(choices)
    assert Decimal(result['costs']['hypothetical_open_source_zero_usd']) == Decimal('.01') + Decimal('.0001') * len(choices)
    assert result['selection']['classifier_calls'] == len(choices)


def test_public_pass_zero_classifiers_and_completed_reuse(tmp_path):
    api = API(first='2')
    engine = core(tmp_path, api)
    first = engine.run('pass', TASK, ArithmeticEnvironment, contract_version='v1')
    second = engine.run('pass', TASK, lambda: pytest.fail('Completed result must not execute tools'), contract_version='v1')
    assert first == second
    assert len(api.requests) == 1
    assert first['selection'] is None and first['final_public_pass'] is True


@pytest.mark.parametrize('mode,first_tier', [('fixed_high', 'high'), ('low_first', 'low'), ('open_source_first', 'open_source')])
def test_baseline_failure_goes_directly_high(tmp_path, mode, first_tier):
    api = API()
    result = core(tmp_path, api).run('baseline', TASK, ArithmeticEnvironment, contract_version='v1', mode=mode)
    assert [a['tier'] for a in result['attempts']] == [first_tier, 'high']
    assert result['selection'] is None and len(api.requests) == 2


@pytest.mark.parametrize('first', ['', None])
def test_empty_output_receives_same_bounded_repair(tmp_path, first):
    result = core(tmp_path, API(first=first)).run('empty', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['attempts'][0]['stop'] == 'empty_or_exhausted'
    assert result['status'] == 'passed' and len(result['attempts']) == 2


@pytest.mark.parametrize('status,expected', [(False, 'failed'), (None, 'unverifiable')])
def test_final_failure_or_unknown_never_success_or_third_attempt(tmp_path, status, expected):
    class Environment(ArithmeticEnvironment):
        def validate(self, text):
            return {'passed': status, 'checks': ['not_established']}
    api = API()
    result = core(tmp_path, api).run('unknown', TASK, Environment, contract_version='v1')
    assert result['status'] == expected and len(result['attempts']) == 2
    assert len(api.requests) == 3


@pytest.mark.parametrize('change', ['task', 'contract', 'config', 'mode'])
def test_resume_rejects_changed_frozen_inputs_before_paid_call(tmp_path, change):
    api = API(first='2')
    engine = core(tmp_path, api)
    engine.run('fixed', TASK, ArithmeticEnvironment, contract_version='v1')
    task, version, mode = TASK, 'v1', 'cloudyfan'
    if change == 'task': task = {**TASK, 'input_sha256': 'changed'}
    if change == 'contract': version = 'v2'
    if change == 'mode': mode = 'low_first'
    if change == 'config': engine.profiles['low']['capability_card'] = {'changed': True}
    with pytest.raises(ValueError, match='Resume evidence changed'):
        engine.run('fixed', task, ArithmeticEnvironment, contract_version=version, mode=mode)
    assert len(api.requests) == 1


@pytest.mark.parametrize('bad', [True, -1, 0, 1.5])
def test_limits_strict(bad):
    with pytest.raises(ValueError): Limits(max_turns=bad)


def test_crash_after_paid_response_resumes_fresh_tools_without_resend(tmp_path):
    messages = [
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1', 'type': 'function',
            'function': {'name': 'write', 'arguments': '{"value":"2"}'}}]},
        {'role': 'assistant', 'content': 'done'}]
    api = API(messages=messages)
    instances = []
    class Env:
        replayable_tools = frozenset({"write"})

        def __init__(self):
            self.value = None
            instances.append(self)
        def tools_schema(self): return [{'type': 'function', 'function': {'name': 'write', 'parameters': {'type': 'object'}}}]
        def call(self, name, args):
            assert self.value is None, 'must start in fresh sandbox'
            self.value = args['value']
            return {'ok': True, 'value': self.value}
        def validate(self, final):
            if len(instances) == 1: raise RuntimeError('validator interrupted')
            return {'passed': self.value == '2'}
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='validator interrupted'):
        engine.run('replay', TASK, Env, contract_version='v1')
    result = engine.run('replay', TASK, Env, contract_version='v1')
    assert result['status'] == 'passed' and len(api.requests) == 2
    assert len(instances) == 2 and result['resumed'] and result['wall_seconds'] is None


def test_reasoning_retained_inside_attempt_stripped_for_repair(tmp_path):
    api = API(messages=[{'role': 'assistant', 'content': '3', 'reasoning': 'first-private-thinking',
                         'reasoning_details': [{'type': 'reasoning.text', 'text': 'first-private-thinking'}]},
                        {'role': 'assistant', 'content': '2'}])
    core(tmp_path, api).run('reasoning', TASK, ArithmeticEnvironment, contract_version='v1')
    assert 'first-private-thinking' not in json.dumps(api.requests[1:])
    assert 'first-private-thinking' in (tmp_path/'calls/run-reasoning-a0-t0/response.json').read_text()


def test_hidden_verification_field_blocks_before_selector_or_repair(tmp_path):
    class Hidden(ArithmeticEnvironment):
        def validate(self, text): return {'passed': False, 'hidden_answer': 'secret'}
    api = API()
    with pytest.raises(ValueError, match='Private evaluation'):
        core(tmp_path, api).run('hidden', TASK, Hidden, contract_version='v1')
    assert len(api.requests) == 1


@pytest.mark.parametrize('field,value', [('model', 'example/unapproved'), ('provider', 'Unapproved')])
def test_identity_mismatch_stops_selection_and_repair(tmp_path, field, value):
    api = API()
    def bad(req):
        raw = json.loads(api(req).content)
        raw[field] = value
        return httpx.Response(200, json=raw)
    with pytest.raises(RuntimeError, match='mismatch'):
        core(tmp_path, bad).run('identity', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 1


def test_reconciliation_replaces_not_adds_cost_on_completed_read(tmp_path):
    api = API(first='2')
    def endpoint(req):
        if req.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1', 'total_cost': .012,
                                                     'model': 'example/open_source', 'provider_name': 'Example'}})
        return api(req)
    engine = core(tmp_path, endpoint)
    assert engine.run('bill', TASK, ArithmeticEnvironment, contract_version='v1')['costs']['api_usd'] == '0.01'
    engine.client.reconcile('run-bill-a0-t0')
    result = engine.run('bill', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['costs']['api_usd'] == '0.012' and len(api.requests) == 1


@pytest.mark.parametrize('field,value', [('model', 'example/changed'), ('provider_name', 'Changed')])
def test_reconciled_identity_change_blocks_completed_result(tmp_path, field, value):
    api = API(first='2')
    def endpoint(req):
        if req.method == 'GET':
            data = {'id': 'gen-test-1', 'total_cost': .012, 'model': 'example/open_source', 'provider_name': 'Example'}
            data[field] = value
            return httpx.Response(200, json={'data': data})
        return api(req)
    engine = core(tmp_path, endpoint)
    engine.run('identitybill', TASK, ArithmeticEnvironment, contract_version='v1')
    engine.client.reconcile('run-identitybill-a0-t0')
    with pytest.raises(RuntimeError, match='Reconciled identity mismatch'):
        engine.run('identitybill', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 1


def test_malformed_tool_invocation_stops_with_retained_cost(tmp_path):
    from cloudyfan_jev.ledger import summarize
    api = API(messages=[{'content': None, 'tool_calls': [{'function': {'name': 'bad'}}]}])
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='Malformed tool invocation'):
        engine.run('malformed', TASK, ArithmeticEnvironment, contract_version='v1')
    ledger = summarize(engine.client)
    assert ledger['total_api_usd'] == '0.01' and ledger['calls'] == 1
    assert not (tmp_path/'runs/malformed/result.json').exists()


def test_unknown_paid_fee_stops_before_classifier_and_remains_in_ledger(tmp_path):
    from cloudyfan_jev.ledger import summarize
    api = API()
    def endpoint(req):
        if req.method == 'GET': return httpx.Response(404, json={'error': 'missing'})
        data = json.loads(api(req).content)
        del data['usage']['cost']
        return httpx.Response(200, json=data)
    engine = core(tmp_path, endpoint)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('unknownfee', TASK, ArithmeticEnvironment, contract_version='v1')
    report = summarize(engine.client)
    assert report['unknown_calls'] == 1 and report['total_api_usd'] is None
    assert len(api.requests) == 1 and report['calls'] == 1
