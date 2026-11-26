"""顧問先分流模式（advisor_triage，ADR 0038）在 Core 與預算預留上的離線測試。

全部離線：只用 httpx.MockTransport，不呼叫任何真實模型。
"""
import copy
from decimal import Decimal
import json

import httpx
import pytest

from cloudyfan_jev.advisor import AdvisorConfig
from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetError, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.triage import (ADVISOR_TRIAGE, OPEN_TASK_NOTE, ROUTE_SYSTEM_V3, OverrideError, TriageConfig,
                                  effective_plan, normalize_overrides)

TASK = {'prompt': 'Return only the integer sum of 1 and 1. Expected output: 2'}
TRIAGER = 'anthropic:claude-haiku-5-5'
ADVISOR = 'oss/advisor-v1'


def profile(model, tier=None, *, effort='xhigh', open_source=None):
    p = {'model': model, 'model_version': model, 'accepted_models': [model], 'provider': 'example',
         'provider_name': 'Example', 'effort': effort}
    if tier is not None:
        p['tier'] = tier
    if open_source is not None:
        p['open_source'] = open_source
    return p


def advisor_config(**fields):
    def role(name):
        return {**profile('oss/' + name + '-v1', effort='medium', open_source=True)}
    values = {'advisor': role('advisor'), 'judge': role('judge'), 'score_snapshot': 'snapshot-20261126-a'}
    values.update(fields)
    return AdvisorConfig(**values)


def triage_config(**fields):
    values = {'triager': profile(TRIAGER, open_source=False),
              'pool': {'sonnet-xhigh': profile('example/sonnet', 'mid'),
                       'fable-high': profile('example/fable', 'high', effort='high'),
                       'haiku-low': profile('example/haiku', 'low', effort='low')},
              'path2_default': 'sonnet-xhigh', 'path2_high_risk': 'fable-high'}
    values.update(fields)
    return TriageConfig(**values)


class VerifiableEnvironment(ArithmeticEnvironment):
    def verifier_available(self, task):
        return True


class OpenEnvironment(ArithmeticEnvironment):
    def verifier_available(self, task):
        return False

    def validate(self, final_text):
        return {'passed': None, 'checks': ['no_public_verifier']}


def route(value, reason='有預期輸出'):
    return json.dumps({'route': value, 'reason': reason})


def delegate(*settings):
    return json.dumps({'mode': 'delegate', 'recommendations': [
        {'setting': s, 'tier': triage_config().pool[s]['tier'], 'reason': '需要判斷'}
        for s in (settings or ('sonnet-xhigh', 'fable-high'))]})


def answer(text, *settings):
    return json.dumps({'mode': 'answer', 'answer': text, 'recommendations': json.loads(delegate(*settings))[
        'recommendations']})


class API:
    """分流顧問依序回 ``triage`` 內容（字串或 HTTP 狀態碼）；作答模型依名稱回答；Jev 回 ``choices``。"""
    def __init__(self, triage=(route('path1'),), answers=None, choices=('low',), advisor=None):
        self.triage, self.answers, self.choices = list(triage), answers or {}, iter(choices)
        self.advisor = advisor or delegate()
        self.requests = []

    def models(self):
        return [p['model'] for p in self.requests]

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(copy.deepcopy(p))
        i = len(self.requests)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(p['questions']))
            choice = next(self.choices, 'high' if stage == 'mid_high' else 'low')
            return httpx.Response(200, json={'id': f'gen-{i}', 'model': p['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0001, 'input_tokens': 10, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': choice, 'probabilities': {
                    k: .8 if k == choice else .2 for k in p['questions'][stage]['criteria']}}}})
        if p['model'] == TRIAGER:
            content = self.triage.pop(0) if len(self.triage) > 1 else self.triage[0]
            if isinstance(content, int):
                return httpx.Response(content, json={'id': f'gen-{i}', 'provider': 'Example', 'usage': {'cost': 0},
                                                     'error': {'code': content, 'message': 'rate limited'}})
            cost = .0005
        elif p['model'] == ADVISOR:
            content, cost = self.advisor, .001
        else:
            content, cost = self.answers.get(p['model'], '3'), .01
        return httpx.Response(200, json={'id': f'gen-{i}', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': cost}, 'choices': [{'message': {'role': 'assistant', 'content': content},
                                                  'finish_reason': 'stop'}]})


def make_core(tmp_path, api, config=None, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(api))
    profiles, decision = example_config()
    kwargs.setdefault('advisor_first', advisor_config())
    return Core(Client(http, 'mock-only-key', tmp_path), profiles, decision,
                advisor_triage=config or triage_config(), **kwargs)


def run(engine, env=VerifiableEnvironment, run_id='r', **kwargs):
    return engine.run(run_id, TASK, env, contract_version='v1', mode=ADVISOR_TRIAGE, **kwargs)


def test_triage_path1_runs_existing_jev_ladder_and_records_triage(tmp_path):
    api = API(answers={'example/open_source': '3', 'example/low': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [TRIAGER, 'example/open_source', 'typesafe/jev-1.13', 'example/low']
    assert api.requests[0]['messages'][0]['content'] == ROUTE_SYSTEM_V3
    assert api.requests[0]['messages'][1]['content'] == TASK['prompt']
    assert result['status'] == 'passed' and result['mode'] == ADVISOR_TRIAGE
    assert result['plan'] == {'route': 'path1', 'route_source': 'triage', 'risk': 'normal', 'escalation': 'auto',
                              'executor': 'ladder', 'setting': None, 'setting_source': 'path1_ladder', 'tier': None}
    assert result['triage']['route'] == 'path1' and result['triage']['retried'] is False
    assert [a['tier'] for a in result['attempts']] == ['open_source', 'low']
    assert result['selection']['classifier_calls'] == 1
    assert result['branch'] == {'kind': 'ladder', 'mode': 'cloudyfan', 'run_id': 'r-p1'}
    assert result['costs']['triage_usd'] == '0.0005'
    assert Decimal(result['costs']['api_usd']) == Decimal('.0005') + Decimal('.02') + Decimal('.0001')


def test_triage_path2_runs_the_advisor_which_delegates_to_the_risk_default(tmp_path):
    api = API(triage=(route('path2', '要人判斷'),), advisor=delegate('fable-high', 'sonnet-xhigh'),
              answers={'example/sonnet': '建議先做 A'})
    result = run(make_core(tmp_path, api), env=OpenEnvironment)
    assert api.models() == [TRIAGER, ADVISOR, 'example/sonnet']
    assert api.requests[1]['messages'][0]['content'].endswith(OPEN_TASK_NOTE)
    assert [p['max_tokens'] for p in api.requests[1:]] == [32000, 32000]
    assert result['status'] == 'unverifiable' and result['final_public_pass'] is None
    assert result['plan']['route'] == 'path2' and result['plan']['executor'] == 'advisor'
    assert result['branch']['end_reason'] == 'delegated_unverifiable'
    attempt = result['attempts'][0]
    assert attempt['tier'] == 'triage:sonnet-xhigh' and attempt['dispatch'] == 'risk_default'
    assert attempt['recommendation_rank'] == 1 and attempt['final'] == '建議先做 A'
    assert result['advisor']['decision']['outcome'] == 'delegate'
    assert result['costs']['advisor_usd'] == '0.001' and result['selection'] is None


def test_advisor_answers_an_open_task_itself_and_stops(tmp_path):
    from cloudyfan_jev.triage import routing_summary
    api = API(triage=(route('path2'),), advisor=answer('先做小規模試驗'))
    engine = make_core(tmp_path, api)
    result = run(engine, env=OpenEnvironment)
    assert api.models() == [TRIAGER, ADVISOR]
    summary = routing_summary(result, engine)
    assert summary['model'] == ADVISOR and summary['end_reason'] == 'advisor_answer_unverifiable'
    assert result['status'] == 'unverifiable' and result['attempts'][0]['final'] == '先做小規模試驗'
    assert result['branch']['end_reason'] == 'advisor_answer_unverifiable'


def test_unusable_advisor_reply_ends_without_dispatch(tmp_path):
    api = API(triage=(route('path2'),), advisor='我覺得應該先做 A')
    result = run(make_core(tmp_path, api), env=OpenEnvironment)
    assert api.models() == [TRIAGER, ADVISOR]
    assert result['status'] == 'advisor_incomplete' and result['attempts'] == []
    assert result['branch']['end_reason'] == 'advisor_incomplete'


def test_failed_verifiable_dispatch_continues_with_advisor_recommendations(tmp_path):
    api = API(triage=(route('path2'),), advisor=delegate('haiku-low', 'fable-high'),
              answers={'example/sonnet': '3', 'example/haiku': '2'})
    result = run(make_core(tmp_path, api), overrides={'route': 'path2'})
    assert api.models() == [TRIAGER, ADVISOR, 'example/sonnet', 'example/haiku']
    assert [a['dispatch'] for a in result['attempts']] == ['risk_default', 'advisor_recommendation']
    assert result['status'] == 'passed' and result['branch']['end_reason'] == 'passed'


def test_triage_requires_the_open_source_advisor_configuration(tmp_path):
    with pytest.raises(ValueError, match='AdvisorConfig'):
        make_core(tmp_path, API(), advisor_first=None)


def test_high_risk_override_uses_high_risk_setting(tmp_path):
    api = API(triage=(route('path2'),))
    result = run(make_core(tmp_path, api), env=OpenEnvironment, overrides={'risk': 'high'})
    assert api.models() == [TRIAGER, ADVISOR, 'example/fable']
    assert result['plan']['risk'] == 'high' and result['plan']['setting_source'] == 'risk_high'
    assert result['overrides'] == {'risk': 'high'}


def test_invalid_triage_reply_is_asked_once_more(tmp_path):
    api = API(triage=('def solve(): return 2', route('path1')), answers={'example/open_source': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [TRIAGER, TRIAGER, 'example/open_source']
    assert result['triage']['retried'] is True and result['triage']['fallback'] is False
    assert [r['outcome'] for r in result['triage']['replies']] == ['invalid', 'classified']
    assert result['triage']['replies'][0]['raw_excerpt'] == 'def solve(): return 2'
    assert result['triage']['calls'][1]['call_id'] == 'run-r-triage-2'
    assert result['costs']['triage_usd'] == '0.0010'


@pytest.mark.parametrize('replies', [('not json', 'still not json'), (429, 429)])
def test_triage_failing_twice_falls_back_to_path2(tmp_path, replies):
    api = API(triage=replies, answers={'example/sonnet': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [TRIAGER, TRIAGER, ADVISOR, 'example/sonnet']
    assert result['triage']['route'] is None and result['triage']['fallback'] is True
    assert result['plan']['route'] == 'path2' and result['plan']['route_source'] == 'triage_fallback'


def test_path1_without_verifier_is_sent_to_path2_and_recorded(tmp_path):
    api = API(triage=(route('path1'),))
    result = run(make_core(tmp_path, api), env=OpenEnvironment)
    assert api.models() == [TRIAGER, ADVISOR, 'example/sonnet']
    assert result['triage']['route'] == 'path1'
    assert result['plan']['route'] == 'path2' and result['plan']['route_source'] == 'no_verifier'


def test_route_override_keeps_triage_original_judgment(tmp_path):
    api = API(triage=(route('path1'),))
    result = run(make_core(tmp_path, api), env=OpenEnvironment, overrides={'route': 'path2'})
    assert api.models() == [TRIAGER, ADVISOR, 'example/sonnet']
    assert result['triage']['route'] == 'path1'
    assert result['plan']['route'] == 'path2' and result['plan']['route_source'] == 'override'
    decision = json.loads((tmp_path / 'runs' / 'r' / 'triage-decision.json').read_text())
    assert decision['triage']['route'] == 'path1' and decision['overrides'] == {'route': 'path2'}


def test_setting_override_answers_once_with_that_setting_even_on_path1(tmp_path):
    api = API(triage=(route('path1'),), answers={'example/haiku': '3'})
    result = run(make_core(tmp_path, api), overrides={'setting': 'haiku-low'})
    assert api.models() == [TRIAGER, 'example/haiku']
    assert result['status'] == 'failed' and len(result['attempts']) == 1
    assert result['plan']['executor'] == 'single' and result['plan']['setting_source'] == 'override'


def test_escalation_off_stops_after_first_path1_answer_without_jev(tmp_path):
    api = API(triage=(route('path1'),), answers={'example/open_source': '3'})
    result = run(make_core(tmp_path, api), overrides={'escalation': 'off'})
    assert api.models() == [TRIAGER, 'example/open_source']
    assert api.requests[1]['max_tokens'] == 8192
    assert result['status'] == 'failed' and result['selection'] is None
    assert [a['tier'] for a in result['attempts']] == ['open_source']


def test_completed_triage_run_is_read_back_without_new_calls(tmp_path):
    api = API(answers={'example/open_source': '2'})
    engine = make_core(tmp_path, api)
    first = run(engine)
    count = len(api.requests)
    again = run(engine, env=lambda: pytest.fail('completed result must not build an environment'))
    assert len(api.requests) == count
    assert again['plan'] == first['plan'] and again['costs'] == first['costs']
    assert engine.read_completed('r')['status'] == 'passed'


def test_resume_after_interruption_does_not_pay_triage_again(tmp_path):
    api = API(answers={'example/open_source': '2'})
    engine = make_core(tmp_path, api)
    def broken():
        raise RuntimeError('workspace failed after triage')
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) == 2:
            broken()
        return VerifiableEnvironment()
    with pytest.raises(RuntimeError):
        run(engine, env=flaky)
    assert api.models() == [TRIAGER]
    result = run(engine)
    assert api.models() == [TRIAGER, 'example/open_source']
    assert result['status'] == 'passed' and result['resumed'] is True


def test_environment_must_declare_verifier_before_any_call(tmp_path):
    api = API()
    with pytest.raises(ValueError, match='verifier_available'):
        run(make_core(tmp_path, api), env=ArithmeticEnvironment)
    assert api.requests == []


@pytest.mark.parametrize('overrides,code', [
    ({'setting': 'gpt-unknown'}, 'setting_not_in_pool'),
    ({'model': 'sonnet'}, 'unsupported_override_field'),
    ({'risk': 'extreme'}, 'invalid_override_value'),
    ({'max_usd': 0.5}, 'invalid_override_value'),
    ({'max_usd': '0'}, 'invalid_override_value'),
    ({'setting': 'haiku-low', 'escalation': 'auto'}, 'override_conflict'),
    ({}, 'overrides_empty'),
    (['risk'], 'overrides_not_object'),
])
def test_core_rejects_invalid_overrides_before_any_call(tmp_path, overrides, code):
    api = API()
    with pytest.raises(OverrideError) as error:
        run(make_core(tmp_path, api), overrides=overrides)
    assert error.value.code == code
    assert api.requests == []


def test_overrides_are_rejected_outside_triage_mode(tmp_path):
    api = API()
    with pytest.raises(ValueError, match='advisor_triage'):
        make_core(tmp_path, api).run('r', TASK, VerifiableEnvironment, contract_version='v1',
                                     mode='cloudyfan', overrides={'risk': 'high'})
    assert api.requests == []


def test_without_triage_config_core_behaviour_and_descriptor_are_unchanged(tmp_path):
    profiles, decision = example_config()
    http = httpx.Client(transport=httpx.MockTransport(API()))
    engine = Core(Client(http, 'mock-only-key', tmp_path), profiles, decision)
    assert ADVISOR_TRIAGE not in engine.workflow_modes()
    assert 'advisor_triage' not in engine.configuration_descriptor()
    with pytest.raises(ValueError, match='Unknown workflow mode'):
        engine.run('r', TASK, VerifiableEnvironment, contract_version='v1', mode=ADVISOR_TRIAGE)


def test_config_rejects_out_of_pool_defaults_and_small_output_limit():
    with pytest.raises(ValueError, match='path2_default'):
        triage_config(path2_default='opus-xhigh')
    with pytest.raises(ValueError, match='32000'):
        triage_config(path2_max_tokens=12000)
    with pytest.raises(ValueError, match='open_source'):
        triage_config(triager=profile(TRIAGER))


def test_effective_plan_table():
    config = triage_config()
    assert effective_plan(config, 'path1', None, True)['executor'] == 'ladder'
    assert effective_plan(config, None, None, True)['route_source'] == 'triage_fallback'
    assert effective_plan(config, 'path2', {'route': 'path1'}, False)['route_source'] == 'no_verifier'
    assert effective_plan(config, 'path1', {'route': 'path2', 'risk': 'high'}, True)['setting'] == 'fable-high'
    assert normalize_overrides({'max_usd': '0.25'}, config) == {'max_usd': '0.25'}


# --- 預算預留（ADR 0038 第 6 點）---

NOW = '2026-10-18T00:00:00+00:00'


def budgeted(tmp_path, api, *, total='5'):
    from datetime import datetime
    engine = make_core(tmp_path / 'client', api)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime.fromisoformat(NOW))
    store.authorize(Authorization('approved', Decimal(total), '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'offline hard cap') for p in engine.profiles.values()}
    bounds.update({p['model_version']: EnforcedBound('.05', 'offline hard cap')
                   for p in engine.advisor_triage.pool.values()})
    bounds[engine.decision['version']] = EnforcedBound('.001', 'offline hard cap')
    bounds[TRIAGER] = EnforcedBound('.002', 'offline hard cap')
    bounds.update({m: EnforcedBound('.001', 'offline hard cap') for m in (ADVISOR, 'oss/judge-v1')})
    return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)


def test_reservation_covers_two_triage_calls_and_the_most_expensive_branch(tmp_path):
    runner = budgeted(tmp_path, API())
    turns = runner.core.limits.max_turns
    ladder = Decimal(runner.worst_case_usd('cloudyfan'))
    # 開放題：顧問一次＋三次作答的池內最高上界＋每次作答一次裁判。
    path2 = Decimal('.001') + Decimal('.05') * 3 * turns + Decimal('.001') * 3
    single = Decimal('.05') * turns
    assert Decimal(runner.worst_case_usd(ADVISOR_TRIAGE)) == Decimal('.004') + max(ladder, path2)
    # 指定模型只剩一條分支；關閉升級時路徑一只剩首答。
    assert Decimal(runner.worst_case_usd(ADVISOR_TRIAGE, {'setting': 'haiku-low'})) == Decimal('.004') + single
    off = Decimal(runner.worst_case_usd(ADVISOR_TRIAGE, {'route': 'path1', 'escalation': 'off'}))
    assert off == Decimal('.004') + max(Decimal('.03') * turns, path2)


def test_max_usd_below_worst_case_is_refused_before_any_call(tmp_path):
    api = API()
    runner = budgeted(tmp_path, api)
    with pytest.raises(BudgetError, match='max_usd_below_worst_case'):
        runner.run('r', TASK, VerifiableEnvironment, contract_version='v1', mode=ADVISOR_TRIAGE,
                   overrides={'max_usd': '0.01'})
    assert api.requests == []


def test_budgeted_triage_run_records_every_call_under_one_reservation(tmp_path):
    api = API(answers={'example/open_source': '3', 'example/low': '2'})
    runner = budgeted(tmp_path, api)
    result = runner.run('r', TASK, VerifiableEnvironment, contract_version='v1', mode=ADVISOR_TRIAGE,
                        overrides={'max_usd': '2'})
    assert result['status'] == 'passed'
    assert result['budget']['run_unknown_events'] == 0
    assert Decimal(result['budget']['run_total_usd']) == Decimal(result['costs']['api_usd'])
