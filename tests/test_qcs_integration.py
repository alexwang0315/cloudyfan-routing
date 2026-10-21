import json

import httpx

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Limits
from cloudyfan_jev.scores.integration import ScoreCore
from test_qcs import inputs, conditional, interval, prepare_qcs
from test_scores import REQUIREMENTS, NOW
import pytest

TASK = {'prompt': 'Return only the integer sum of 1 and 1.', 'workload_id': 'arithmetic-small'}


def test_direct_mid_really_omits_jev_and_replays_without_new_post(tmp_path):
    requests = []

    def upstream(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert not request.url.path.endswith('/decisions')
        return httpx.Response(200, json={'id': f'gen-qcs-{len(requests)}',
            'model': payload['model'], 'provider': 'Example', 'usage': {'cost': '0.01'},
            'choices': [{'message': {'content': '3' if payload['model'] == 'example/open_source' else '2'}}]})

    snap, evidence, policy = inputs()
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    client = Client(httpx.Client(transport=httpx.MockTransport(upstream)), 'fixture-only', tmp_path)
    _, decision = example_config()
    core = ScoreCore(client, snap, REQUIREMENTS, decision, run_id='qcs', clock=lambda: NOW,
                     qcs_plan=plan, limits=Limits(max_turns=1))
    result = core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid']
    assert result['selection']['classifier_calls'] == 0
    assert result['costs']['api_usd'] == '0.02' and result['costs']['classifier_usd'] == '0'
    assert core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
    assert len(requests) == 2


def make_core(tmp_path, handler, *, settings=None, run_id='qcs', now=NOW):
    snap, evidence, policy = settings or inputs()
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    client = Client(httpx.Client(transport=httpx.MockTransport(handler)), 'fixture-only', tmp_path)
    _, decision = example_config()
    return ScoreCore(client, snap, REQUIREMENTS, decision, run_id=run_id, clock=lambda: now,
                     qcs_plan=plan, limits=Limits(max_turns=1))


def reply(payload, n, answer='2', *, status=200, cost='0.01'):
    return httpx.Response(status, json={'id': f'gen-qcs-{n}', 'model': payload['model'],
        'provider': 'Example', 'usage': {'cost': cost},
        'choices': [{'message': {'content': answer}}]})


def test_latest_mid_failure_reaches_only_second_classifier_with_qcs_context(tmp_path):
    snap, evidence, policy = inputs()
    evidence['high']['generation_cost_usd'] = {'lower': '1', 'upper': '1'}
    evidence['high']['complete_cost_usd'] = {'lower': '1', 'upper': '1'}
    for key in ('mid_high:low', 'mid_high:mid'):
        policy['stages'][key]['paths']['high']['cost_usd'] = {'lower': '1', 'upper': '1'}
        policy['stages'][key]['classify_flow_ms_upper'] = '110'
        policy['stages'][key]['paths']['mid']['cost_usd'] = interval('1')
    conditional((snap, evidence, policy), 'mid_high:mid', 'mid_high', 'mid', {'mid': '.1', 'high': '1'})
    requests = []

    def upstream(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if request.url.path.endswith('/decisions'):
            assert list(payload['questions']) == ['mid_high']
            state = payload['state']
            assert state['step']['policy_version'] == 'hierarchical-qcs-v1'
            assert state['observations']['previous_tier'] == 'mid'
            assert state['observations']['previous_answer'] == 'bad-mid'
            assert state['candidates']['mid']['capability_card']['qcs']['speed']['statistic'] == 'p95'
            return httpx.Response(200, json={'id': f'gen-qcs-{len(requests)}',
                'model': payload['model'], 'provider': 'TypeSafe', 'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'mid_high': {'type': 'choice', 'choice': 'high', 'probabilities': {'mid': .2, 'high': .8}}}})
        tier = payload['model'].split('/')[-1]
        return reply(payload, len(requests), '2' if tier == 'high' else 'bad-' + tier)

    core = make_core(tmp_path, upstream, settings=(snap, evidence, policy))
    result = core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid', 'high']
    assert result['selection']['classifier_calls'] == 1 and result['costs']['api_usd'] == '0.031'
    assert result['selection']['decisions'][-1]['fallback'] is False
    assert core.configuration_descriptor()['routing_version'] == 'hierarchical-qcs-v1'


def test_validation_interruption_resumes_existing_answers_and_free_decision(tmp_path):
    from cloudyfan_jev.sql import SqlValidationUnavailable
    calls, interrupted = [], []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return reply(payload, len(calls), '3' if len(calls) == 1 else '2')

    class Environment(ArithmeticEnvironment):
        def validate(self, text):
            if text == '2' and not interrupted:
                interrupted.append(True)
                raise SqlValidationUnavailable('SQL_VALIDATION_TIMEOUT')
            return super().validate(text)

    core = make_core(tmp_path, upstream)
    with pytest.raises(SqlValidationUnavailable):
        core.run('qcs', TASK, Environment, contract_version='v1')
    assert len(calls) == 2
    assert core.run('qcs', TASK, Environment, contract_version='v1')['status'] == 'passed'
    assert len(calls) == 2


def test_budget_reserves_only_reachable_generations_and_no_classifier_bound(tmp_path):
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    from cloudyfan_jev.engine import Limits
    from decimal import Decimal
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return reply(payload, len(calls), '3' if len(calls) == 1 else '2')

    core = make_core(tmp_path / 'client', upstream)
    core.limits = Limits(max_turns=1)
    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('mock-only', Decimal('0.3'), '2099-01-01T00:00:00Z'))
    # No low or classifier bound exists: neither can be dispatched by this plan.
    bounds = {core.profiles[t]['model_version']: EnforcedBound('0.1', 'synthetic')
              for t in ('open_source', 'mid', 'high')}
    result = BudgetedCore(core, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True).run(
        'qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.3'
    assert result['budget']['run_total_usd'] == '0.02' and len(calls) == 2


def test_workload_or_policy_change_refuses_without_resending(tmp_path):
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return reply(payload, len(calls))

    core = make_core(tmp_path, upstream)
    with pytest.raises(ValueError, match='workload'):
        core.run('qcs', {**TASK, 'workload_id': 'another'}, ArithmeticEnvironment, contract_version='v1')
    assert not calls
    core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    settings = inputs()
    settings[2]['minimum_saving_usd'] = '0.1'
    changed = make_core(tmp_path, upstream, settings=settings)
    with pytest.raises(ValueError, match='manifest'):
        changed.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(calls) == 1


def test_historical_unknown_still_blocks_a_zero_classifier_plan(tmp_path):
    from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, BudgetedCore, EnforcedBound
    from cloudyfan_jev.engine import Limits
    from decimal import Decimal

    core = make_core(tmp_path / 'client', lambda request: pytest.fail('must not dispatch'))
    core.limits = Limits(max_turns=1)
    store = BudgetStore(tmp_path / 'budget.sqlite')
    store.authorize(Authorization('mock-only', Decimal('10'), '2099-01-01T00:00:00Z'))
    store.import_history('old-unknown', cost_usd=None)
    bounds = {core.profiles[t]['model_version']: EnforcedBound('0.1', 'synthetic')
              for t in ('open_source', 'mid', 'high')}
    with pytest.raises(BudgetError, match='unknown'):
        BudgetedCore(core, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True).run(
            'qcs', TASK, ArithmeticEnvironment, contract_version='v1')


def test_expired_estimates_can_replay_original_manifest_but_cannot_start_new_run(tmp_path):
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return reply(payload, len(calls))

    core = make_core(tmp_path, upstream)
    core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    expired = make_core(tmp_path, upstream, now='2030-01-01T00:00:00Z')
    assert expired.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
    assert len(calls) == 1
    with pytest.raises(ValueError):
        make_core(tmp_path, upstream, run_id='fresh', now='2030-01-01T00:00:00Z')


def test_paid_known_429_uses_direct_upgrade_and_retains_its_fee(tmp_path):
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(429, json={'id': 'gen-reject-qcs', 'model': payload['model'],
                'provider': 'Example', 'usage': {'cost': '0.001'},
                'error': {'code': 429, 'metadata': {'provider_name': 'Example', 'provider_code': 'rate_limited'}}})
        return reply(payload, len(calls))

    core = make_core(tmp_path, upstream)
    # Actual billed rejection is enough; no zero-fee exception is installed.
    result = core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'mid']
    assert result['costs']['api_usd'] == '0.011'


def test_missing_low_pool_runs_and_replays_without_structural_placeholder(tmp_path):
    from cloudyfan_jev.scores.catalog import digest
    settings = inputs()
    settings[0]['pools']['low'] = []
    settings[0]['snapshot_id'] = digest({k: v for k, v in settings[0].items() if k != 'snapshot_id'})
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return reply(payload, len(calls), '3' if len(calls) == 1 else '2')

    core = make_core(tmp_path, upstream, settings=settings)
    assert 'low' not in core.profiles
    assert core.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
    replay = make_core(tmp_path, upstream, settings=settings)
    assert replay.run('qcs', TASK, ArithmeticEnvironment, contract_version='v1')['status'] == 'passed'
    assert len(calls) == 2


def test_classifier_version_change_cannot_reuse_conditional_calibration(tmp_path):
    from test_qcs import classifiable
    snap, evidence, policy = classifiable()
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    _, decision = example_config()
    decision['version'] = 'different-version'
    client = Client(httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('no POST'))), 'mock', tmp_path)
    with pytest.raises(ValueError, match='classifier version'):
        ScoreCore(client, snap, REQUIREMENTS, decision, run_id='qcs', clock=lambda: NOW, qcs_plan=plan)


@pytest.mark.parametrize('change', ['limits', 'system', 'validation'])
def test_flow_calibration_cannot_be_reused_with_changed_execution_contract(tmp_path, change):
    from cloudyfan_jev.engine import Limits
    calls = []

    def upstream(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return reply(payload, len(calls))

    core = make_core(tmp_path, upstream)
    if change == 'limits': core.limits = Limits(max_turns=10)
    if change == 'system': core.system = 'different generation instructions'
    with pytest.raises(ValueError, match='execution contract'):
        core.run('qcs', TASK, ArithmeticEnvironment, contract_version='other' if change == 'validation' else 'v1')
    assert not calls
