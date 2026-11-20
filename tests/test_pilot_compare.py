"""#81 三組比較執行器：純路徑一、路徑一加強版、路徑二。全部離線，兩家廠商都只接 httpx.MockTransport。"""
import json

import httpx

from cloudyfan_jev import hard_tasks, pilot

TASKS = [hard_tasks.make('A', 1, 0), hard_tasks.make('E', 2, 0)]
ANSWERS = {t['prompt']: t['answer'] for t in TASKS}


def answer_for(messages):
    prompt = next(m['content'] for m in messages if m['role'] == 'user')
    for text, answer in ANSWERS.items():
        if prompt.startswith(text):
            return answer
    raise AssertionError('unknown task prompt')


class OpenRouter:
    """開源首答一律答錯；顧問一律改派中、高池；Jev 一律選中池。"""
    def __init__(self):
        self.models = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.models.append(body['model'])
        n = len(self.models)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(body['questions']))
            choice = 'mid'
            criteria = body['questions'][stage]['criteria']
            return httpx.Response(200, json={'id': f'gen-jev-{n}', 'model': body['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0002}, 'answers': {stage: {'type': 'choice', 'choice': choice,
                'probabilities': {k: .8 if k == choice else .2 for k in criteria}}}})
        if body['model'] == 'qwen/qwen3.5-397b-a17b':
            content = json.dumps({'mode': 'delegate', 'recommendations': [
                {'setting': 'mid', 'tier': 'mid', 'reason': 'needs care'},
                {'setting': 'high', 'tier': 'high', 'reason': 'fallback'}]})
        else:
            content = 'ANSWER: 1'
        return httpx.Response(200, json={'id': f'gen-or-{n}', 'model': body['model'], 'provider': 'Crusoe'
            if body['model'] == 'openai/gpt-oss-120b' else 'Alibaba' if 'qwen' in body['model'] else 'Parasail',
            'usage': {'cost': .0003, 'prompt_tokens': 100, 'completion_tokens': 50},
            'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})


class Anthropic:
    def __init__(self):
        self.models = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.models.append(body['model'])
        content = [{'type': 'text', 'text': 'ANSWER: ' + answer_for(body['messages'])}]
        if body.get('system'):
            content[0]['text'] = 'work\n' + content[0]['text']
        return httpx.Response(200, json={'id': f'msg_{len(self.models):04d}', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': content, 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 1000, 'output_tokens': 200}})


def setup():
    return {'open_target': pilot.openrouter_target('openai/gpt-oss-120b', provider='crusoe/bf16',
                                                   provider_name='Crusoe', input_per_mtok='0.05',
                                                   output_per_mtok='0.25'),
            'advisor_target': pilot.openrouter_target('qwen/qwen3.5-397b-a17b', provider='alibaba',
                                                      provider_name='Alibaba', input_per_mtok='0.39',
                                                      output_per_mtok='2.34', effort='low'),
            'judge_target': pilot.openrouter_target('openai/gpt-oss-20b', provider='parasail/fp4',
                                                    provider_name='Parasail', input_per_mtok='0.03',
                                                    output_per_mtok='0.15'),
            'pool_targets': {'low': pilot.anthropic_target('claude-haiku-5-5', effort='medium'),
                             'mid': pilot.anthropic_target('claude-sonnet-5-5', effort='medium'),
                             'high': pilot.anthropic_target('claude-fable-5-2', effort='high')},
            'decision': pilot.JEV, 'decision_per_call_usd': '0.01'}


def run(tmp_path, **kw):
    orouter, anth = OpenRouter(), Anthropic()
    rows = pilot.compare(tmp_path / 'cmp', TASKS, openrouter_key='or-key', anthropic_key='an-key', cap_usd='5',
                         transports={'openrouter': httpx.MockTransport(orouter),
                                     'anthropic': httpx.MockTransport(anth)}, **setup(), **kw)
    return rows, orouter, anth


def test_each_task_runs_on_every_arm_and_escalations_reach_anthropic(tmp_path):
    rows, orouter, anth = run(tmp_path)
    assert sorted((r['task'], r['arm']) for r in rows) == sorted(
        (t['id'], arm) for t in TASKS for arm in pilot.ARMS)
    by = {(r['task'], r['arm']): r for r in rows}
    for t in TASKS:
        p1 = by[(t['id'], 'cloudyfan')]
        # 開源首答錯 → Jev 選中池 → Sonnet 答對。
        assert p1['first_pass'] is False and p1['final_pass'] is True
        assert p1['tiers'] == ['open_source', 'mid'] and p1['classifier_calls'] == 1
        p2 = by[(t['id'], 'advisor_first')]
        assert p2['advisor_outcome'] == 'delegate' and p2['final_pass'] is True and p2['tiers'] == ['pool:mid']
        assert by[(t['id'], 'cloudyfan_plus')]['tiers'] == ['open_source', 'mid']
    assert set(anth.models) == {'claude-sonnet-5-5'}
    assert 'typesafe/jev-1.13-20260917' in orouter.models


def test_report_splits_research_and_formal_costs_without_prompts_or_answers(tmp_path):
    rows, _, _ = run(tmp_path)
    report = json.loads((tmp_path / 'cmp' / 'compare-report.json').read_text())
    arm = report['arms']['advisor_first']
    assert arm['tasks'] == 2 and arm['final_pass'] == 2 and arm['advisor_delegate'] == 2
    # Sonnet 每題 1000 × $2/M + 200 × $10/M = 0.004；開源顧問每次 0.0003 只計入研究全額。
    assert arm['formal_usd'] == '0.008' and arm['research_usd'] == '0.0086'
    assert report['arms']['cloudyfan']['tier_counts'] == {'open_source': 2, 'mid': 2}
    text = json.dumps(report)
    assert '"prompt"' not in text and all(t['prompt'][:40] not in text for t in TASKS)


def test_arm_order_rotates_and_rerun_does_not_pay_again(tmp_path):
    rows, orouter, anth = run(tmp_path)
    orders = [[r['arm'] for r in rows if r['task'] == t['id']] for t in TASKS]
    assert orders[0] != orders[1]
    before = len(orouter.models) + len(anth.models)
    again, orouter2, anth2 = run(tmp_path)
    assert len(orouter2.models) + len(anth2.models) == 0
    assert [r['final_pass'] for r in again] == [r['final_pass'] for r in rows] and before > 0


def test_only_selected_arms_run(tmp_path):
    rows, _, _ = run(tmp_path, arms=('advisor_first',))
    assert {r['arm'] for r in rows} == {'advisor_first'}


def test_default_pilot_setup_uses_the_agreed_models_and_frozen_tasks():
    setup_ = pilot.pilot_setup()
    assert setup_['open_target'] == setup_['advisor_target']
    assert setup_['open_target']['vendor_model'] == 'deepseek/deepseek-v4-pro'
    assert setup_['open_target']['profile']['provider'] == 'siliconflow/fp8'
    # 2026-11-19：qwen3.5-397b 在 Alibaba 被上游 429 拒絕，使用者決定換顧問模型。
    assert setup_['advisor_target']['vendor_model'] == 'deepseek/deepseek-v4-pro'
    assert setup_['advisor_target']['profile']['provider'] == 'siliconflow/fp8'
    assert setup_['judge_target']['profile']['provider'] == 'parasail/fp4'
    assert {t: p['vendor_model'] for t, p in setup_['pool_targets'].items()} == {
        'low': 'claude-haiku-5-5', 'mid': 'claude-sonnet-5-5', 'high': 'claude-fable-5-2'}
    tasks = pilot.frozen_tasks()
    assert len(tasks) == 50 and pilot.frozen_tasks(limit=10, interleave=True)[0]['family'] != \
        pilot.frozen_tasks(limit=10, interleave=True)[1]['family']


def test_spend_cap_is_separate_from_the_ledger_exposure(tmp_path):
    """帳本授權只容納保守暫扣；實際花費上限另外檢查：已花＋這題最壞花費超過上限就不跑。"""
    orouter, anth = OpenRouter(), Anthropic()
    rows = pilot.compare(tmp_path / 'cmp', TASKS, openrouter_key='or-key', anthropic_key='an-key',
                         cap_usd='0.5', ledger_exposure_usd='100',
                         transports={'openrouter': httpx.MockTransport(orouter),
                                     'anthropic': httpx.MockTransport(anth)}, **setup())
    # 每題最壞花費（含 Fable 的牌價上界）超過 0.5：一題都不送出。
    assert {r['status'] for r in rows} == {'not_run'} and all(r['reason'] == 'spend_cap' for r in rows)
    assert orouter.models == [] and anth.models == []
    report = json.loads((tmp_path / 'cmp' / 'compare-report.json').read_text())
    assert report['spend_cap_usd'] == '0.5' and report['ledger_exposure_usd'] == '100'


def test_spend_cap_counts_what_was_already_spent_and_completed_runs_are_still_read(tmp_path):
    orouter, anth = OpenRouter(), Anthropic()
    kw = dict(openrouter_key='or-key', anthropic_key='an-key', ledger_exposure_usd='100',
              arms=('cloudyfan', 'advisor_first'), **setup())
    first = pilot.compare(tmp_path / 'cmp', TASKS[:1], cap_usd='100',
                          transports={'openrouter': httpx.MockTransport(orouter),
                                      'anthropic': httpx.MockTransport(anth)}, **kw)
    spent = sum(float(r['research_usd']) for r in first)
    worst = pilot.compare_worst_case_usd(**setup())
    cap = str(round(spent + float(min(worst['cloudyfan'], worst['advisor_first'])) - 0.001, 6))
    rows = pilot.compare(tmp_path / 'cmp', TASKS, cap_usd=cap,
                         transports={'openrouter': httpx.MockTransport(orouter),
                                     'anthropic': httpx.MockTransport(anth)}, **kw)
    assert [r['task'] for r in rows if 'final_pass' in r] == [TASKS[0]['id'], TASKS[0]['id']]
    assert [r.get('reason') for r in rows if r['task'] == TASKS[1]['id']] == ['spend_cap', 'spend_cap']


def test_task_filter_runs_only_named_tasks(tmp_path):
    tasks = pilot.frozen_tasks(only=['H1-1', 'I1-1'])
    assert [t['id'] for t in tasks] == ['H1-1', 'I1-1']
