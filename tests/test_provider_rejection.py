"""No real HTTP: bounded escalation after cost-resolved, no-answer 429."""
from copy import deepcopy
from decimal import Decimal
import json

import httpx
import pytest

from cloudyfan_jev.client import rate_limit_rejection
from cloudyfan_jev.demo import ArithmeticEnvironment
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.ledger import summarize
from test_engine import API, TASK, core


class RateLimitedAPI(API):
    def __init__(self, *, rejects=(1,), cost='.002', choices=('low',), mutate=None, receipt=False):
        super().__init__(first='2', choices=choices)
        self.rejects, self.cost, self.mutate, self.receipt = rejects, cost, mutate, receipt
        self.posts, self.gets = [], []
        self.receipts = {}

    def __call__(self, request):
        if request.method == 'GET':
            self.gets.append(str(request.url))
            gid = request.url.params['id']
            if not self.receipt:
                return httpx.Response(404, json={'error': 'not found'})
            return httpx.Response(200, json={'data': self.receipts[gid]})
        payload = json.loads(request.content)
        self.posts.append(payload)
        n = len(self.posts)
        if n in self.rejects:
            gid = f'gen-rejected-{n}'
            data = {'error': {'code': 429, 'message': 'Provider returned error',
                             'metadata': {'provider_name': 'TypeSafe' if payload['model'].startswith('typesafe/') else 'Example',
                                          'raw': 'private upstream detail'}},
                    'openrouter_metadata': {'requested': payload['model']},
                    'usage': {'cost': self.cost, 'completion_tokens': 0}}
            if self.mutate:
                self.mutate(data)
            self.receipts[gid] = {'id': gid, 'total_cost': '.003', 'model': payload['model'],
                                  'provider_name': 'Example'}
            return httpx.Response(429, json=data, headers={'x-generation-id': gid})
        response = super().__call__(request)
        return response


class NoMissingAnswerValidation(ArithmeticEnvironment):
    def validate(self, final_text):
        assert final_text, 'Provider rejection is not a task answer to grade'
        return super().validate(final_text)


@pytest.mark.parametrize('choices,tier', [(('low',), 'low'), (('mid', 'mid'), 'mid'), (('mid', 'high'), 'high')])
def test_cloudyfan_429_uses_existing_binary_tree_once_and_replays_no_posts(tmp_path, choices, tier):
    api = RateLimitedAPI(choices=choices)
    engine = core(tmp_path, api, Limits(max_turns=3))
    result = engine.run('rate', TASK, NoMissingAnswerValidation, contract_version='v1')
    assert result['status'] == 'passed' and result['first_public_pass'] is None
    assert [a['tier'] for a in result['attempts']] == (['open_source', 'mid', tier] if len(choices)==2 else ['open_source', tier])
    first = result['attempts'][0]
    assert first['stop'] == 'provider_rate_limited' and first['final'] == ''
    assert first['public']['checks'] == ['PROVIDER_RATE_LIMITED']
    assert first['calls'][0]['model'] is None  # Do not invent an actual generated model.
    assert first['calls'][0]['requested_model'] == 'example/open_source'
    assert result['selection']['classifier_calls'] == len(choices)
    assert [p['model'] for p in api.posts] == (['example/open_source', 'typesafe/jev-1.13', 'example/mid',
        'typesafe/jev-1.13', 'example/'+tier] if len(choices)==2 else ['example/open_source','typesafe/jev-1.13','example/'+tier])
    assert len(api.posts) == (5 if len(choices)==2 else 3)  # No same-endpoint automatic retry.
    assert Decimal(result['costs']['api_usd']) == Decimal('.002') + Decimal('.01')*len(choices) + Decimal('.0001') * len(choices)
    assert summarize(engine.client)['total_api_usd'] == result['costs']['api_usd']
    assert 'private upstream detail' not in json.dumps(api.posts[1:])
    assert engine.run('rate', TASK, NoMissingAnswerValidation, contract_version='v1') == result
    assert engine.read_completed('rate') == result
    assert len(api.posts) == (5 if len(choices)==2 else 3)


@pytest.mark.parametrize('mode,initial', [('low_first', 'low'), ('open_source_first', 'open_source')])
def test_baselines_cost_known_429_use_single_high_attempt(tmp_path, mode, initial):
    api = RateLimitedAPI(cost='0')
    result = core(tmp_path, api).run('baseline429', TASK, NoMissingAnswerValidation, contract_version='v1', mode=mode)
    assert [a['tier'] for a in result['attempts']] == [initial, 'high']
    assert result['status'] == 'passed' and result['selection'] is None
    assert len(api.posts) == 2 and result['costs']['api_usd'] == '0.01'


def test_unknown_429_fee_and_404_never_become_zero_or_allow_selector(tmp_path):
    api = RateLimitedAPI(cost=None)
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('unknown429', TASK, NoMissingAnswerValidation, contract_version='v1')
    assert len(api.posts) == 1 and len(api.gets) == 1
    ledger = summarize(engine.client)
    assert ledger['unknown_calls'] == 1 and ledger['total_api_usd'] is None
    with pytest.raises(RuntimeError):
        engine.run('other', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1


def test_generation_get_can_resolve_rejection_then_resume_without_resend(tmp_path):
    api = RateLimitedAPI(cost=None)
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('resume429', TASK, ArithmeticEnvironment, contract_version='v1', mode='low_first')
    original = (tmp_path/'calls/run-resume429-a0-t0/response.json').read_bytes()
    api.receipt = True
    result = engine.run('resume429', TASK, NoMissingAnswerValidation, contract_version='v1', mode='low_first')
    assert len(api.posts) == 2 and result['resumed'] and result['status'] == 'passed'
    assert result['costs']['api_usd'] == '0.013'
    assert (tmp_path/'calls/run-resume429-a0-t0/response.json').read_bytes() == original
    assert result['attempts'][0]['calls'][0]['cost_source'] == 'generation_total_cost'


@pytest.mark.parametrize('mode', ['cloudyfan', 'low_first', 'open_source_first'])
def test_second_generation_429_is_terminal_unknown_not_success_or_third_generation(tmp_path, mode):
    api = RateLimitedAPI(rejects=(1, 3) if mode == 'cloudyfan' else (1, 2))
    result = core(tmp_path, api).run('two429', TASK, NoMissingAnswerValidation, contract_version='v1', mode=mode)
    assert len(result['attempts']) == (3 if mode=='cloudyfan' else 2)
    assert result['status'] == ('passed' if mode=='cloudyfan' else 'unverifiable')
    assert result['attempts'][1]['stop'] == 'provider_rate_limited'
    assert len(api.posts) == (5 if mode=='cloudyfan' else 2)


def test_fixed_high_rejection_does_not_retry_the_same_configuration(tmp_path):
    api = RateLimitedAPI()
    result = core(tmp_path, api).run('fixed429', TASK, NoMissingAnswerValidation,
                                   contract_version='v1', mode='fixed_high')
    assert len(api.posts) == 1 and len(result['attempts']) == 1
    assert result['status'] == 'unverifiable' and result['selection'] is None


def test_unknown_repair_fee_stops_at_second_attempt_and_preserves_paid_classifier(tmp_path):
    api = RateLimitedAPI(rejects=(1, 3))
    def second_unknown(data):
        if len(api.posts) == 3:
            data['usage']['cost'] = None
    api.mutate = second_unknown
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('repairunknown', TASK, ArithmeticEnvironment, contract_version='v1')
    report = summarize(engine.client)
    assert len(api.posts) == 3 and report['unknown_calls'] == 1
    assert Decimal(report['known_unique_cost_usd']) == Decimal('.0021')
    assert report['total_api_usd'] is None


@pytest.mark.parametrize('status', [408, 500, 502, 503])
def test_other_http_failures_are_not_reinterpreted_as_rate_limit(tmp_path, status):
    api = RateLimitedAPI()
    def other(request):
        response = api(request)
        return httpx.Response(status, content=response.content, headers=response.headers)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        core(tmp_path, other).run('otherstatus', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1


def test_missing_generation_id_stops_even_when_rejected_fee_is_zero(tmp_path):
    api = RateLimitedAPI(cost='0')
    def missing(request):
        response = api(request)
        response.headers.pop('x-generation-id', None)
        return response
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        core(tmp_path, missing).run('missingid', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1


def test_transport_timeout_does_not_enter_selector_or_upgrade(tmp_path):
    posts = []
    def timeout(request):
        posts.append(request)
        raise httpx.ReadTimeout('private timeout', request=request)
    engine = core(tmp_path, timeout)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('timeout429', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(posts) == 1 and summarize(engine.client)['unknown_calls'] == 1


def test_new_engine_rejects_old_frozen_manifest_without_rewriting_or_dispatch(tmp_path):
    api = RateLimitedAPI(rejects=())
    engine = core(tmp_path, api)
    engine.run('old', TASK, ArithmeticEnvironment, contract_version='v1')
    manifest = tmp_path/'runs/old/manifest.json'
    old = json.loads(manifest.read_text()); old['engine_version'] = '0.1.0'
    manifest.write_text(json.dumps(old))
    before = manifest.read_bytes()
    with pytest.raises(ValueError, match='Resume evidence changed'):
        engine.run('old', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1 and manifest.read_bytes() == before


@pytest.mark.parametrize('mutation', [
    lambda d: d['error']['metadata'].update(provider_name='Other'),
    lambda d: d.update(model='example/unapproved'),
    lambda d: d.update(provider='Other'),
    lambda d: d['openrouter_metadata'].update(requested='example/unapproved'),
    lambda d: d.update(choices=[{'message': {'content': 'partial answer'}}]),
    lambda d: d.update(choices=[{'message': {'tool_calls': [{'id': 'maybe-executed'}]}}]),
    lambda d: d['error'].update(code=500),
    lambda d: d['error'].update(code=True),
    lambda d: d.update(error='malformed'),
    lambda d: d.update(openrouter_metadata='malformed'),
])
def test_ambiguous_or_mismatched_rejection_stops_before_upgrade(tmp_path, mutation):
    api = RateLimitedAPI(mutate=mutation)
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        core(tmp_path, api).run('bad429', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1


def test_selector_429_never_restarts_classifier_or_generates(tmp_path):
    api = RateLimitedAPI(rejects=(1, 2))
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        core(tmp_path, api).run('classifier429', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 2 and api.posts[1]['model'] == 'typesafe/jev-1.13'


def test_duplicate_rejection_generation_id_blocks_upgrade(tmp_path):
    api = RateLimitedAPI(rejects=(1, 2))
    def duplicate(request):
        response = api(request)
        response.headers['x-generation-id'] = 'gen-rejected-1'
        return response
    engine = core(tmp_path, duplicate)
    engine.client.call('prior', 'https://openrouter.ai/api/v1/chat/completions', {'model': 'example/low'})
    # Replaying the same upstream generation under another call ID is not new evidence.
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        engine.run('duplicate429', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 2


def test_budget_keeps_rejected_fee_plus_selector_and_repair_inside_original_reservation(tmp_path):
    from test_budget import controlled
    api = RateLimitedAPI()
    app = controlled(tmp_path, api, limits=Limits(max_turns=1), total='.1')
    result = app.run('budget429', TASK, NoMissingAnswerValidation, contract_version='v1')
    assert result['status'] == 'passed'
    assert Decimal(result['budget']['run_total_usd']) == Decimal('.0121')
    assert Decimal(result['budget']['shared']['reservations'][0]['amount']) == Decimal('.092')
    assert len(api.posts) == 3


def test_reconciled_model_change_cannot_turn_rejection_into_valid_resume(tmp_path):
    api = RateLimitedAPI(cost=None, receipt=True)
    original = api.__call__
    def changed(request):
        response = original(request)
        if request.method == 'GET':
            body = response.json(); body['data']['model'] = 'example/unapproved'
            return httpx.Response(200, json=body)
        return response
    with pytest.raises(RuntimeError, match='Dispatch stopped'):
        core(tmp_path, changed).run('changed429', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.posts) == 1


def test_rejection_parser_never_trusts_duplicate_json_keys_or_missing_provider():
    raw = {'error': {'code': 429, 'metadata': {'provider_name': 'Example'}}}
    record = {'http_status': 429, 'raw': json.dumps(raw)}
    assert rate_limit_rejection(record, 'example/low', ['example/low'], 'Example')
    missing = deepcopy(record); missing['raw'] = '{"error":{"code":429}}'
    assert not rate_limit_rejection(missing, 'example/low', ['example/low'], 'Example')
    duplicate = deepcopy(record); duplicate['raw'] = '{"error":{"code":500,"code":429}}'
    assert not rate_limit_rejection(duplicate, 'example/low', ['example/low'], 'Example')
