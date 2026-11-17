"""#73：顧問先答流程（路徑二，ADR 0034 第 1、2、4、10 點）端到端測試。

全部離線：只用 httpx.MockTransport，不呼叫任何真實模型。
"""
import copy
import json

import httpx
import pytest

from cloudyfan_jev.advisor import ADVISOR_FIRST, AdvisorConfig
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core

TASK = {'prompt': 'Return only the integer sum of 1 and 1.'}
ADVISOR_MODEL = 'oss/advisor-v1'


def role_profile(name):
    return {'model': 'oss/' + name, 'model_version': 'oss/' + name + '-v1',
            'accepted_models': ['oss/' + name + '-v1'], 'provider': 'example',
            'provider_name': 'Example', 'effort': 'medium', 'open_source': True}


def config(**overrides):
    fields = {'advisor': role_profile('advisor'), 'judge': role_profile('judge'),
              'score_snapshot': 'snapshot-20261117-a'}
    fields.update(overrides)
    return AdvisorConfig(**fields)


def rec(setting, tier=None, reason='因為這類題目它常做對'):
    return {'setting': setting, 'tier': tier or setting, 'reason': reason}


def decision(mode='delegate', answer=None, recommendations=None):
    body = {'mode': mode, 'recommendations': recommendations if recommendations is not None
            else [rec('mid'), rec('high')]}
    if answer is not None:
        body['answer'] = answer
    return json.dumps(body, ensure_ascii=False)


class API:
    """顧問回固定內容；池內模型依模型名稱回答。"""
    def __init__(self, advisor_content, answers=None, *, advisor_status=200):
        self.advisor_content = advisor_content
        self.answers = answers or {}
        self.advisor_status = advisor_status
        self.requests = []

    def models(self):
        return [p['model'] for p in self.requests]

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(copy.deepcopy(p))
        i = len(self.requests)
        if p['model'] == ADVISOR_MODEL:
            if self.advisor_status != 200:
                return httpx.Response(self.advisor_status, json={
                    'id': f'gen-{i}', 'provider': 'Example', 'usage': {'cost': 0},
                    'error': {'code': 429, 'message': 'rate limited'}})
            content = self.advisor_content
        else:
            content = self.answers.get(p['model'], '3')
        return httpx.Response(200, json={'id': f'gen-{i}', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': .01}, 'choices': [{'message': {'role': 'assistant', 'content': content},
                                                  'finish_reason': 'stop'}]})


def make_core(tmp_path, api, advisor=None):
    http = httpx.Client(transport=httpx.MockTransport(api))
    profiles, decision_profile = example_config()
    return Core(Client(http, 'mock-only-key', tmp_path), profiles, decision_profile,
                advisor_first=advisor or config())


def run(engine, run_id='r', task=TASK, env=ArithmeticEnvironment):
    return engine.run(run_id, task, env, contract_version='v1', mode=ADVISOR_FIRST)


def test_advisor_direct_answer_passes_without_dispatching_pool_models(tmp_path):
    api = API(decision('answer', '2'))
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL]
    assert result['status'] == 'passed' and result['end_reason'] == 'passed'
    assert result['mode'] == ADVISOR_FIRST
    assert [a['source'] for a in result['attempts']] == ['advisor_answer']
    assert result['advisor']['decision']['outcome'] == 'answer'
    assert [r['setting'] for r in result['advisor']['decision']['recommendations']] == ['mid', 'high']
    assert result['costs']['api_usd'] == '0.01' and result['costs']['advisor_usd'] == '0.01'
    # 開源顧問明示 open_source，零模型費用情境不計入。
    assert result['costs']['hypothetical_open_source_zero_usd'] == '0'


def test_failed_direct_answer_delegates_first_recommendation_without_asking_advisor_again(tmp_path):
    api = API(decision('answer', '3'), {'example/mid': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL, 'example/mid']
    assert result['status'] == 'passed'
    assert [(a['source'], a.get('setting')) for a in result['attempts']] == [
        ('advisor_answer', None), ('delegated', 'mid')]
    assert result['first_public_pass'] is False
    delegated = result['attempts'][1]
    assert delegated['calls'][0]['call_id'] == 'run-r-a1-t0'
    assert delegated['calls'][0]['tier'] == 'pool:mid' and delegated['pool_tier'] == 'mid'
    # 研究全額：顧問 .01 ＋ 中池 .01；零模型費用情境只算池內作答。
    assert result['costs']['api_usd'] == '0.02'
    assert result['costs']['hypothetical_open_source_zero_usd'] == '0.01'


def test_first_recommendation_failing_delegates_second(tmp_path):
    api = API(decision('delegate'), {'example/mid': '3', 'example/high': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL, 'example/mid', 'example/high']
    assert result['status'] == 'passed'
    assert [a['setting'] for a in result['attempts']] == ['mid', 'high']
    assert [a['public']['passed'] for a in result['attempts']] == [False, True]


def test_recommendations_exhausted_ends_without_default_fallback_or_jev(tmp_path):
    api = API(decision('delegate'))
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL, 'example/mid', 'example/high']
    assert result['status'] == 'failed' and result['end_reason'] == 'recommendations_exhausted'
    assert result['selection'] is None
    assert not any('/decisions' in str(p) or 'questions' in p for p in api.requests)


def test_at_most_three_answering_attempts_including_direct_answer(tmp_path):
    recs = [rec('low'), rec('mid'), rec('high')]
    api = API(decision('answer', '3', recs))
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL, 'example/low', 'example/mid']
    assert len(result['attempts']) == 3
    assert result['status'] == 'failed' and result['end_reason'] == 'attempt_limit'


def test_delegate_mode_can_use_all_three_attempts(tmp_path):
    recs = [rec('low'), rec('mid'), rec('high')]
    api = API(decision('delegate', recommendations=recs), {'example/high': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL, 'example/low', 'example/mid', 'example/high']
    assert result['status'] == 'passed'


def test_configured_attempt_limit_is_respected(tmp_path):
    api = API(decision('delegate'))
    result = run(make_core(tmp_path, api, config(max_attempts=1)))
    assert api.models() == [ADVISOR_MODEL, 'example/mid']
    assert result['end_reason'] == 'attempt_limit'


def test_out_of_pool_recommendations_are_voided_with_reasons(tmp_path):
    recs = [rec('frontier-opus', 'high'), rec('mid', 'high'), rec('high'), rec('high'),
            {'setting': 'low'}, rec('low')]
    api = API(decision('delegate', recommendations=recs), {'example/high': '3', 'example/low': '2'})
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL, 'example/high', 'example/low']
    advice = result['advisor']['decision']
    assert [(v['rank'], v['reason']) for v in advice['voided']] == [
        (0, 'not_in_pool'), (1, 'tier_mismatch'), (3, 'duplicate'), (4, 'malformed')]
    assert [r['setting'] for r in advice['recommendations']] == ['high', 'low']
    assert result['status'] == 'passed'


@pytest.mark.parametrize('content,reason', [
    ('not json at all', 'invalid_json'),
    ('```json\n{"mode": "delegate"}\n```', 'invalid_json'),
    ('[]', 'invalid_json'),
    (json.dumps({'mode': 'maybe', 'recommendations': [rec('mid'), rec('high')]}), 'invalid_schema'),
    (json.dumps({'mode': 'answer', 'recommendations': [rec('mid'), rec('high')]}), 'invalid_schema'),
    (json.dumps({'mode': 'delegate', 'answer': '2', 'recommendations': [rec('mid'), rec('high')]}),
     'invalid_schema'),
    (json.dumps({'mode': 'delegate', 'note': 'x', 'recommendations': [rec('mid'), rec('high')]}),
     'invalid_schema'),
    (json.dumps({'mode': 'answer', 'answer': '2', 'recommendations': [rec('mid')]}),
     'too_few_recommendations'),
    (json.dumps({'mode': 'answer', 'answer': '2',
                 'recommendations': [rec('gpt-x', 'high'), rec('claude-y', 'mid')]}),
     'no_valid_recommendations'),
])
def test_unparseable_or_unusable_advice_ends_as_advisor_incomplete(tmp_path, content, reason):
    api = API(content)
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL]
    assert result['status'] == 'advisor_incomplete' and result['end_reason'] == 'advisor_incomplete'
    assert result['advisor']['decision']['incomplete_reason'] == reason
    assert result['attempts'] == [] and result['final_public_pass'] is None
    assert result['costs']['advisor_usd'] == '0.01'


def test_advisor_provider_rejection_is_recorded_as_incomplete(tmp_path):
    api = API(decision('answer', '2'), advisor_status=429)
    result = run(make_core(tmp_path, api))
    assert api.models() == [ADVISOR_MODEL]
    assert result['advisor']['decision']['incomplete_reason'] == 'provider_rejected'
    assert result['status'] == 'advisor_incomplete'


class NoVerifier(ArithmeticEnvironment):
    def validate(self, final_text):
        return {'passed': None, 'checks': ['no_public_verifier']}


def test_direct_answer_without_verifier_stays_unverifiable(tmp_path):
    api = API(decision('answer', '2'))
    result = run(make_core(tmp_path, api, config(max_attempts=1)), env=NoVerifier)
    assert result['status'] == 'unverifiable'
    assert result['attempts'][0]['public']['passed'] is None
    assert result['final_public_pass'] is None


def test_delegated_model_gets_only_the_original_task(tmp_path):
    reason = 'SECRET-ADVISOR-REASONING'
    recs = [rec('mid', reason=reason), rec('high', reason=reason)]
    api = API(decision('answer', 'advisor-draft-answer', recs), {'example/high': '2'})
    run(make_core(tmp_path, api))
    for payload in api.requests[1:]:
        assert payload['messages'][1:] == [{'role': 'user', 'content': TASK['prompt']}]
        assert reason not in json.dumps(payload) and 'advisor-draft-answer' not in json.dumps(payload)
    # 顧問輸入只含公開題目與池內設定識別。
    advisor_input = json.loads(api.requests[0]['messages'][1]['content'])
    assert advisor_input['task'] == TASK
    assert {p['setting'] for p in advisor_input['pool']} == {'low', 'mid', 'high'}
    assert 'capability_card' not in json.dumps(advisor_input)


@pytest.mark.parametrize('private', ['hidden_tests', 'expected_answer', 'goldAnswer', 'private_notes'])
def test_private_task_fields_are_blocked_before_any_call(tmp_path, private):
    api = API(decision('answer', '2'))
    with pytest.raises(ValueError, match='Private'):
        run(make_core(tmp_path, api), task={**TASK, private: '2'})
    assert api.requests == []
    assert not (tmp_path / 'runs' / 'r' / 'manifest.json').exists()


def test_interrupted_run_resumes_without_paying_advisor_or_attempts_again(tmp_path):
    instances = []

    class Flaky(ArithmeticEnvironment):
        def __init__(self):
            instances.append(self)

        def validate(self, final_text):
            if len(instances) == 1 and final_text == '2':
                raise RuntimeError('validator interrupted')
            return super().validate(final_text)

    api = API(decision('answer', '3'), {'example/mid': '2'})
    engine = make_core(tmp_path, api)
    with pytest.raises(RuntimeError, match='validator interrupted'):
        run(engine, env=Flaky)
    assert api.models() == [ADVISOR_MODEL, 'example/mid']
    result = run(engine, env=Flaky)
    assert api.models() == [ADVISOR_MODEL, 'example/mid']
    assert result['status'] == 'passed' and result['resumed'] and result['wall_seconds'] is None
    again = run(engine, env=Flaky)
    assert again == result and len(api.requests) == 2
    assert engine.read_completed('r') == result


def test_changed_advisor_config_refuses_resume(tmp_path):
    api = API(decision('answer', '2'))
    run(make_core(tmp_path, api))
    changed = make_core(tmp_path, api, config(score_snapshot='snapshot-20261118-b'))
    with pytest.raises(ValueError, match='Resume evidence changed'):
        run(changed)
    assert len(api.requests) == 1


def test_custom_pool_setting_dispatches_its_frozen_profile(tmp_path):
    profiles, _ = example_config()
    pool = {'mid-fast': {**profiles['mid'], 'effort': 'low', 'tier': 'mid'},
            'high': {**profiles['high'], 'tier': 'high'}}
    api = API(decision('delegate', recommendations=[rec('mid-fast', 'mid'), rec('high')]),
              {'example/mid': '2'})
    engine = make_core(tmp_path, api, config(pool=pool))
    result = run(engine)
    assert result['status'] == 'passed' and result['attempts'][0]['setting'] == 'mid-fast'
    assert result['attempts'][0]['calls'][0]['tier'] == 'pool:mid-fast'
    assert engine.read_completed('r')['costs']['api_usd'] == '0.02'


def test_result_records_traceable_advice_and_snapshot(tmp_path):
    api = API(decision('delegate', recommendations=[rec('frontier', 'high'), rec('mid'), rec('high')]),
              {'example/mid': '2'})
    result = run(make_core(tmp_path, api))
    advice = result['advisor']
    assert advice['score_snapshot'] == 'snapshot-20261117-a'
    assert advice['calls'][0]['call_id'] == 'run-r-advisor' and advice['calls'][0]['tier'] == 'advisor'
    assert advice['decision']['outcome'] == 'delegate'
    assert advice['decision']['recommendations'][0]['reason'] == '因為這類題目它常做對'
    assert advice['decision']['voided'][0]['reason'] == 'not_in_pool'
    assert result['attempts'][0]['recommendation_rank'] == 1
    assert result['costs']['calls'] == 2


def test_flow_version_and_advisor_prompt_are_frozen(tmp_path):
    from cloudyfan_jev.advisor_flow import ADVISOR_SYSTEM, FLOW_VERSION
    frozen = make_core(tmp_path, API('')).configuration_descriptor()['advisor_first']['flow']
    assert frozen == {'version': FLOW_VERSION, 'advisor_system': ADVISOR_SYSTEM}


def test_budgeted_core_runs_advisor_first_within_reservation(tmp_path):
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore, EnforcedBound
    api = API(decision('answer', '3'), {'example/mid': '2'})
    engine = make_core(tmp_path / 'client', api)
    usd = {'example/open_source': '.03', 'example/low': '.01', 'example/mid': '.02',
           'example/high': '.05', 'typesafe/jev-1.13': '.001',
           'oss/advisor-v1': '.02', 'oss/judge-v1': '.0005'}
    store = BudgetStore(tmp_path / 'budget.sqlite',
                        clock=lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
    store.authorize(Authorization('approved', '2', '2026-11-18T00:00:00+00:00'))
    app = BudgetedCore(engine, store, model_bounds={k: EnforcedBound(v, 'offline fixture') for k, v in usd.items()},
                       tool_bounds={}, verify_bound=lambda *a: True)
    result = app.run('b', TASK, ArithmeticEnvironment, contract_version='v1', mode=ADVISOR_FIRST)
    assert result['status'] == 'passed'
    assert api.models() == [ADVISOR_MODEL, 'example/mid']
    assert result['costs']['api_usd'] == '0.02'
