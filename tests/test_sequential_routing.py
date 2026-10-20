"""Sequential two-repair policy: only public diagnostics, MockTransport, no APIs."""
from datetime import datetime
from decimal import Decimal
import json

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, BudgetedCore, EnforcedBound
from cloudyfan_jev.demo import ArithmeticEnvironment
from cloudyfan_jev.engine import Limits
from test_engine import API, TASK, core


def messages(*values):
    return [{'role': 'assistant', 'content': value} for value in values]


@pytest.mark.parametrize('choices,answers,tiers,count', [
    ((), ('2',), ['open_source'], 1),
    (('low',), ('3', '2'), ['open_source', 'low'], 3),
    (('mid',), ('3', '2'), ['open_source', 'mid'], 3),
    (('low', 'high'), ('3', '4', '2'), ['open_source', 'low', 'high'], 5),
    (('low', 'mid'), ('3', '4', '2'), ['open_source', 'low', 'mid'], 5),
    (('mid', 'high'), ('3', '4', '2'), ['open_source', 'mid', 'high'], 5),
    (('mid', 'mid'), ('3', '4', '2'), ['open_source', 'mid', 'mid'], 5),
])
def test_each_stage_generates_before_any_next_decision(tmp_path, choices, answers, tiers, count):
    api = API(choices=choices, messages=messages(*answers))
    result = core(tmp_path, api).run('sequence', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == tiers
    assert len(api.requests) == count
    order = [next(iter(p['questions'])) if 'questions' in p else p['model'] for p in api.requests]
    expected = ['example/open_source']
    if len(tiers) > 1: expected += ['low_mid', 'example/' + tiers[1]]
    if len(tiers) > 2: expected += ['mid_high', 'example/' + tiers[2]]
    assert order == expected
    assert Decimal(result['costs']['api_usd']) == Decimal('.01') * len(tiers) + Decimal('.0001') * len(choices)
    if choices:
        assert result['selection']['classifier_calls'] == len(choices)
        assert len(result['selection']['calls']) == len(choices)
    if len(tiers) == 3:
        state = api.requests[3]['state']
        assert state['observations']['previous_answer'] == '4'
        assert state['observations']['previous_tier'] == tiers[1]
        assert state['observations']['previous_configuration']['model_version'] == 'example/'+tiers[1]
        assert 'final attempt' not in api.requests[2]['messages'][-1]['content'].lower()
        assert 'final attempt' in api.requests[4]['messages'][-1]['content'].lower()
        assert 'no intermediate' not in api.requests[3]['questions']['mid_high']['instructions'].lower()


@pytest.mark.parametrize('passed,status', [(False, 'failed'), (None, 'unverifiable')])
def test_third_failure_stops_without_fourth_attempt(tmp_path, passed, status):
    class Unmet(ArithmeticEnvironment):
        def validate(self, text): return {'passed': passed, 'checks': ['not_established']}
    api = API(choices=('low', 'high'))
    result = core(tmp_path, api).run('bounded', TASK, Unmet, contract_version='v1')
    assert result['status'] == status and len(result['attempts']) == 3
    assert len(api.requests) == 5


def test_second_answer_saved_before_validation_interruption_and_resume_no_resend(tmp_path):
    api = API(choices=('low', 'high'), messages=messages('3', '4', '2'))
    interrupted = False
    class Env(ArithmeticEnvironment):
        def validate(self, text):
            nonlocal interrupted
            if text == '4' and not interrupted:
                interrupted = True
                raise RuntimeError('second validation interrupted')
            return super().validate(text)
    engine = core(tmp_path, api)
    with pytest.raises(RuntimeError, match='second validation interrupted'):
        engine.run('resume', TASK, Env, contract_version='v1')
    assert len(api.requests) == 3
    result = engine.run('resume', TASK, Env, contract_version='v1')
    assert result['status'] == 'passed' and len(api.requests) == 5
    engine.run('resume', TASK, lambda: pytest.fail('completed replay must not create environment'), contract_version='v1')
    assert len(api.requests) == 5 and result['costs']['api_usd'] == '0.0302'


def test_second_unknown_fee_stops_before_next_decision(tmp_path):
    api = API(choices=('low', 'high'), messages=messages('3', '4', '2'))
    def serve(request):
        if request.method == 'GET': return httpx.Response(404, json={'error': 'not available'})
        response = api(request)
        if len(api.requests) == 3:
            raw = json.loads(response.content); raw['usage'].pop('cost')
            return httpx.Response(200, json=raw)
        return response
    with pytest.raises(RuntimeError, match='incomplete billing'):
        core(tmp_path, serve).run('unknown', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 3
    assert all('mid_high' not in p.get('questions', {}) for p in api.requests)


def budgeted(tmp_path, api, total='1'):
    engine = core(tmp_path/'client', api, Limits(max_turns=1))
    store = BudgetStore(tmp_path/'budget.sqlite', clock=lambda: datetime.fromisoformat('2026-10-18T00:00:00+00:00'))
    store.authorize(Authorization('offline-fixture', total, '2099-01-01T00:00:00+00:00'))
    caps = {'open_source': '.01', 'low': '.02', 'mid': '.04', 'high': '.10'}
    bounds = {p['model_version']: EnforcedBound(caps[t], 'mock-only-cap') for t,p in engine.profiles.items()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'mock-only-cap')
    return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *args: True)


def test_reservation_covers_three_generation_phases_and_both_decisions(tmp_path):
    api = API(choices=('low','high'), messages=messages('3','4','2'))
    app = budgeted(tmp_path, api)
    assert app._admit('cloudyfan') == Decimal('.152')
    result = app.run('budget', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed' and result['budget']['run_total_usd'] == '0.0302'
    assert app.store.report()['reservations'][0]['amount'] == '0.152'


def test_new_full_reservation_rejects_before_any_post(tmp_path):
    api = API()
    app = budgeted(tmp_path, api, total='.151')
    with pytest.raises(BudgetError, match='balance'):
        app.run('insufficient', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def test_three_cost_resolved_rejections_stop_after_final_stage(tmp_path):
    from test_provider_rejection import RateLimitedAPI, NoMissingAnswerValidation
    api = RateLimitedAPI(rejects=(1,3,5), choices=('low','high'))
    result = core(tmp_path,api).run('three429',TASK,NoMissingAnswerValidation,contract_version='v1')
    assert result['status']=='unverifiable' and len(result['attempts'])==3
    assert len(api.posts)==5 and result['costs']['api_usd']=='0.0062'


def test_baseline_modes_retain_two_generation_attempt_limit(tmp_path):
    for mode in ('fixed_high','low_first','open_source_first'):
        class Unmet(ArithmeticEnvironment):
            def validate(self,text): return {'passed':False}
        api=API()
        result=core(tmp_path/mode,api).run('baseline',TASK,Unmet,contract_version='v1',mode=mode)
        assert len(result['attempts'])==2 and result['selection'] is None
        assert len(api.requests)==2


def test_old_two_attempt_experiment_contract_cannot_execute_as_new_policy():
    from test_experiments import setup_plan
    from test_qualification import NOW
    from cloudyfan_jev.experiments import build_plan
    spec,pack,review=setup_plan()
    spec['max_generation_attempts']=2
    spec['repair_policy']='one-public-diagnostic-repair-v1'
    plan=build_plan(spec,pack,review,now=NOW)
    assert 'execution:invalid_contract' in plan['blockers']


def test_unknown_second_classifier_fee_blocks_final_generation(tmp_path):
    from cloudyfan_jev.ledger import summarize
    api=API(choices=('low','high'),messages=messages('3','4','2'))
    def serve(request):
        if request.method=='GET': return httpx.Response(404,json={'error':'not available'})
        response=api(request)
        if len(api.requests)==4:
            data=json.loads(response.content);data['usage'].pop('cost')
            return httpx.Response(200,json=data)
        return response
    engine=core(tmp_path,serve)
    with pytest.raises(RuntimeError,match='incomplete billing'):
        engine.run('classifierunknown',TASK,ArithmeticEnvironment,contract_version='v1')
    assert len(api.requests)==4
    assert summarize(engine.client)['unknown_calls']==1


def test_saved_third_answer_resumes_without_repeating_either_classifier(tmp_path):
    api=API(choices=('low','high'),messages=messages('3','4','2'))
    fail=True
    class Env(ArithmeticEnvironment):
        def validate(self,text):
            nonlocal fail
            if text=='2' and fail:
                fail=False
                raise RuntimeError('third validation interrupted')
            return super().validate(text)
    engine=core(tmp_path,api)
    with pytest.raises(RuntimeError,match='third validation interrupted'):
        engine.run('thirdresume',TASK,Env,contract_version='v1')
    assert len(api.requests)==5
    result=engine.run('thirdresume',TASK,Env,contract_version='v1')
    assert result['status']=='passed' and len(api.requests)==5
    assert result['costs']['api_usd']=='0.0302'


def test_reasoning_from_both_earlier_answers_is_absent_from_final_requests(tmp_path):
    replies=messages('3','4','2')
    replies[0]['reasoning']='FIRST_REASONING_SENTINEL'
    replies[1]['reasoning']='SECOND_REASONING_SENTINEL'
    api=API(choices=('low','high'),messages=replies)
    result=core(tmp_path,api).run('reasoning3',TASK,ArithmeticEnvironment,contract_version='v1')
    assert result['status']=='passed'
    assert 'FIRST_REASONING_SENTINEL' not in json.dumps(api.requests[3:])
    assert 'SECOND_REASONING_SENTINEL' not in json.dumps(api.requests[3:])


def test_second_selector_uses_latest_public_diagnostic_and_configuration(tmp_path):
    class Env(ArithmeticEnvironment):
        def validate(self, text):
            return {'passed': text == '2', 'checks': ['public-check-for-' + text]}
    api = API(choices=('low', 'high'), messages=messages('3', '4', '2'))
    result = core(tmp_path, api).run('latest-diagnostic', TASK, Env, contract_version='v1')
    first = api.requests[1]['state']['observations']
    second = api.requests[3]['state']['observations']
    assert first['public_diagnostics']['checks'] == ['public-check-for-3']
    assert second['public_diagnostics']['checks'] == ['public-check-for-4']
    assert second['previous_configuration']['model_version'] == 'example/low'
    assert second['previous_answer'] == '4' and second['previous_tier'] == 'low'
    assert result['status'] == 'passed'


def test_legacy_manifest_refuses_resume_before_environment_or_post(tmp_path):
    from cloudyfan_jev.scores.catalog import digest
    api = API()
    engine = core(tmp_path, api)
    legacy = {**engine.configuration_descriptor(), 'task': TASK, 'mode': 'cloudyfan',
              'contract_version': 'v1'}
    legacy.update(engine_version='0.1.1', routing_version='hierarchical50-binary-v2')
    legacy.pop('generation_attempt_limits')
    directory = engine.client.root / 'runs' / 'legacy'
    directory.mkdir(parents=True)
    manifest = directory / 'manifest.json'
    manifest.write_text(json.dumps(legacy))
    original = digest(json.loads(manifest.read_text()))
    with pytest.raises(ValueError, match='Resume evidence changed: manifest'):
        engine.run('legacy', TASK, lambda: pytest.fail('must not start environment'),
                   contract_version='v1')
    assert api.requests == [] and digest(json.loads(manifest.read_text())) == original


def test_legacy_plan_is_reportable_but_not_executable():
    from cloudyfan_jev.experiments import build_plan, verify_plan, verify_plan_integrity
    from cloudyfan_jev.reports import report_binding
    from cloudyfan_jev.scores.catalog import digest
    from test_experiments import setup_plan
    from test_qualification import NOW
    spec, pack, review = setup_plan()
    legacy = build_plan(spec, pack, review, now=NOW)
    legacy.update(schema_version='cloudyfan-experiment-plan-v1',
                  routing_version='hierarchical50-binary-v2')
    legacy['spec'].update(max_generation_attempts=2, repair_policy='one-public-diagnostic-repair-v1')
    legacy['plan_id'] = digest({k: v for k, v in legacy.items() if k != 'plan_id'})
    original = digest(legacy)
    verify_plan_integrity(legacy)
    binding = report_binding(legacy, 'cloudyfan', 'holdout-1')
    assert binding['max_generation_attempts'] == 2
    assert binding['routing_version'] == 'hierarchical50-binary-v2'
    with pytest.raises(ValueError, match='Frozen routing implementation version differs'):
        verify_plan(legacy, now=NOW, allow_synthetic=True)
    assert digest(legacy) == original
