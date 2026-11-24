"""盲測（2026-11-24）：只看公開驗收決定升級，事後才以隱藏測試計分；顧問只分派、評審在交卷前把關、
三種成本。改程式題環境以假模組替代，這裡只驗接線，全部離線。"""
import json
import sys
import types
from decimal import Decimal

import httpx
import pytest

from cloudyfan_jev import pilot
from cloudyfan_jev.advisor_flow import ADVISOR_SYSTEM_DELEGATE_ONLY, advisor_messages, parse_decision

TASKS = [{'id': 'X1-0', 'family': 'X', 'level': 1, 'prompt': 'Write solve().', 'kind': 'code'}]


class FakeEnv:
    """公開驗收：交出 SOLUTION 就放行；隱藏測試：只有 OK 版本正確。"""
    replayable_tools = ()

    def __init__(self, task):
        self.task = task

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        return {'ok': False, 'error': {'code': 'unknown_tool', 'category': 'content'}}

    def validate(self, final_text):
        passed = 'SOLUTION' in (final_text or '')
        return {'passed': passed, 'checks': [] if passed else ['code_block_missing']}


def fake_grade(task, final_text, *, require_sandbox=True):
    ok = 'SOLUTION OK' in (final_text or '')
    return {'passed': ok, 'hidden_passed': 50 if ok else 41, 'hidden_total': 50, 'status': 'ok'}


@pytest.fixture
def fake_code_tasks(monkeypatch):
    module = types.ModuleType('cloudyfan_jev.code_tasks')
    module.BlindCodeEnvironment = FakeEnv
    module.grade_hidden = fake_grade
    monkeypatch.setitem(sys.modules, 'cloudyfan_jev.code_tasks', module)
    sandbox = types.ModuleType('cloudyfan_jev.code_sandbox')
    sandbox.sandbox_available = lambda: True
    monkeypatch.setitem(sys.modules, 'cloudyfan_jev.code_sandbox', sandbox)
    return module


class Anthropic:
    """Sonnet 交出過得了公開範例的錯誤版；Opus／Fable 交出正確版；Haiku 依系統提示當顧問或評審。"""

    def __init__(self, advisor_mode='delegate'):
        self.calls = []
        self.advisor_mode = advisor_mode

    def __call__(self, request):
        body = json.loads(request.content)
        system = json.dumps(body.get('system'))
        model = body['model']
        self.calls.append((model, body.get('output_config', {}).get('effort'), system[:40]))
        if 'advisor of a model-routing service' in system:
            decision = {'mode': self.advisor_mode,
                        'recommendations': [{'setting': 'sonnet-xhigh', 'tier': 'low', 'reason': 'cheap'},
                                            {'setting': 'fable-xhigh', 'tier': 'high', 'reason': 'strong'}]}
            if self.advisor_mode == 'answer':
                decision['answer'] = 'SOLUTION OK from the advisor'
            text = json.dumps(decision)
        elif 'You review a candidate answer' in system:
            candidate = json.loads(body['messages'][-1]['content'])['candidate']
            text = json.dumps({'ok': 'BUG' not in candidate,
                               'issue': '' if 'BUG' not in candidate else 'Misses the empty-input rule.'})
        elif 'claude-sonnet-5-5' in model:
            text = 'SOLUTION BUG'
        else:
            text = 'SOLUTION OK'
        return httpx.Response(200, json={'id': f'msg_{len(self.calls):04d}', 'type': 'message', 'role': 'assistant',
            'model': model, 'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 1000, 'output_tokens': 200}})


class Jev:
    def __init__(self):
        self.n = 0

    def __call__(self, request):
        body = json.loads(request.content)
        self.n += 1
        stage = next(iter(body['questions']))
        criteria = body['questions'][stage]['criteria']
        return httpx.Response(200, json={'id': f'gen-jev-{self.n}', 'model': body['model'], 'provider': 'TypeSafe',
            'usage': {'cost': .01}, 'answers': {stage: {'type': 'choice', 'choice': 'mid',
            'probabilities': {k: .8 if k == 'mid' else .2 for k in criteria}}}})


def run_blind(tmp_path, advisor_mode='delegate'):
    anth, jev = Anthropic(advisor_mode), Jev()
    rows = pilot.compare(tmp_path / 'blind', TASKS, openrouter_key='or-key', anthropic_key='an-key', cap_usd='50',
                         arms=pilot.BLIND_ARMS, blind=True,
                         transports={'openrouter': httpx.MockTransport(jev), 'anthropic': httpx.MockTransport(anth)},
                         **pilot.blind_setup())
    return {r['arm']: r for r in rows}, anth, jev


def test_blind_roles_use_the_requested_models_and_efforts():
    setup = pilot.blind_setup()
    assert setup['open_target']['profile']['effort'] == 'xhigh' and setup['open_target']['vendor_model'] == 'claude-sonnet-5-5'
    pools = {tier: (t['vendor_model'], t['profile']['effort']) for tier, t in setup['pool_targets'].items()}
    assert pools == {'low': ('claude-opus-5-5', 'low'), 'mid': ('claude-opus-5-5', 'xhigh'),
                     'high': ('claude-fable-5-2', 'xhigh')}
    for role in ('advisor_target', 'judge_target'):
        assert (setup[role]['vendor_model'], setup[role]['profile']['effort']) == ('claude-haiku-5-5', 'xhigh')
    assert setup['decision'] == pilot.JEV
    assert set(setup['advisor_pool']) == {'sonnet-xhigh', 'opus-low', 'opus-xhigh', 'fable-xhigh'}
    assert all('per million tokens' in s['price'] for s in setup['advisor_pool'].values())


def test_blind_arms_escalate_only_on_public_signals_and_score_hidden_afterwards(tmp_path, fake_code_tasks):
    rows, anth, jev = run_blind(tmp_path)
    # A：一律 Fable xhigh，正確。
    assert rows['fixed_high']['tiers'] == ['high'] and rows['fixed_high']['hidden_final_pass'] is True
    # B：Sonnet 的錯誤版過了公開範例就停 → 誤放行。
    b = rows['cloudyfan']
    assert b['tiers'] == ['open_source'] and b['final_pass'] is True
    assert b['hidden_final_pass'] is False and b['false_accept'] is True
    # C：評審抓到問題 → Jev 升級到中池 Opus xhigh → 正確，評審放行。
    c = rows['cloudyfan_plus']
    assert c['tiers'] == ['open_source', 'mid'] and c['review_flags'] == 1
    assert c['hidden_by_attempt'] == [False, True] and c['false_accept'] is False
    # D：顧問只分派；先派 Sonnet → 誤放行。
    d = rows['advisor_first']
    assert d['advisor_outcome'] == 'delegate' and d['advisor_settings'] == ['sonnet-xhigh']
    assert d['false_accept'] is True
    assert ('claude-sonnet-5-5', 'xhigh') in {(m, e) for m, e, _ in anth.calls}
    assert ('claude-fable-5-2', 'xhigh') in {(m, e) for m, e, _ in anth.calls}
    assert ('claude-haiku-5-5', 'xhigh') in {(m, e) for m, e, _ in anth.calls}
    assert jev.n == 1
    report = json.loads((tmp_path / 'blind' / 'compare-report.json').read_text())
    assert report['stage'] == 'blind-compare'
    assert report['arms']['cloudyfan']['false_accept'] == 1 and report['arms']['cloudyfan_plus']['hidden_final_pass'] == 1


def test_three_cost_views_zero_roles_then_sonnet(tmp_path, fake_code_tasks):
    rows, _, _ = run_blind(tmp_path)
    usd = lambda r, k: Decimal(r[k])
    a, b, c, d = (rows[k] for k in ('fixed_high', 'cloudyfan', 'cloudyfan_plus', 'advisor_first'))
    # A 沒有顧問、評審或 Sonnet：三種相同。
    assert usd(a, 'real_usd') == usd(a, 'roles_zero_usd') == usd(a, 'roles_sonnet_zero_usd') > 0
    # B 只有 Sonnet：假設一不變，假設二為 0。
    assert usd(b, 'real_usd') == usd(b, 'roles_zero_usd') > 0 and usd(b, 'roles_sonnet_zero_usd') == 0
    # C：評審兩次（Haiku），Sonnet 一次，Jev 與 Opus 照計。
    haiku = Decimal('1000') * Decimal('0.10') / 10**6 + Decimal('200') * Decimal('0.50') / 10**6
    sonnet = Decimal('1000') * 2 / 10**6 + Decimal('200') * 10 / 10**6
    assert usd(c, 'real_usd') - usd(c, 'roles_zero_usd') == 2 * haiku
    assert usd(c, 'roles_zero_usd') - usd(c, 'roles_sonnet_zero_usd') == sonnet
    # D：顧問一次（Haiku）＋被分派的 Sonnet 一次。
    assert usd(d, 'real_usd') - usd(d, 'roles_zero_usd') == haiku
    assert usd(d, 'roles_sonnet_zero_usd') == 0
    assert usd(c, 'real_usd') == Decimal(c['research_usd'])


def test_delegate_only_advisor_never_answers(tmp_path, fake_code_tasks):
    rows, _, _ = run_blind(tmp_path, advisor_mode='answer')
    d = rows['advisor_first']
    assert d['advisor_outcome'] == 'delegate' and d['advisor_settings'] == ['sonnet-xhigh']
    decision = json.loads((tmp_path / 'blind/receipts/runs/p2-X1-0/advisor-decision.json').read_text())
    assert decision['answer_ignored'] is True and decision['answer'] is None


def test_delegate_only_listing_shows_prices_and_frozen_system():
    pool = pilot.blind_setup()['advisor_pool']
    messages = advisor_messages({'prompt': 'p'}, pool, delegate_only=True)
    assert messages[0]['content'] == ADVISOR_SYSTEM_DELEGATE_ONLY
    listing = json.loads(messages[1]['content'])['pool']
    assert all('price' in item for item in listing)
    # 原本的路徑二不帶價格、提示不變。
    assert all('price' not in item for item in json.loads(advisor_messages({'prompt': 'p'}, pool)[1]['content'])['pool'])
    content = json.dumps({'mode': 'answer', 'answer': 'x', 'recommendations': [
        {'setting': 'opus-low', 'tier': 'low', 'reason': 'r'}, {'setting': 'fable-xhigh', 'tier': 'high', 'reason': 'r'}]})
    assert parse_decision(content, pool)['outcome'] == 'answer'
    assert parse_decision(content, pool, delegate_only=True)['outcome'] == 'delegate'


def test_rejection_policy_validity_is_written_once_per_root(tmp_path):
    first = pilot._policy_valid_until(tmp_path)
    assert pilot._policy_valid_until(tmp_path) == first
    assert (tmp_path / 'rejection-policy-valid-until.txt').read_text().strip() == first


def test_completed_blind_rows_read_back_after_restart(tmp_path, fake_code_tasks):
    first, _, _ = run_blind(tmp_path)

    def refuse(request):
        raise AssertionError('completed tasks must not dispatch again')
    rows = pilot.compare(tmp_path / 'blind', TASKS, openrouter_key='or-key', anthropic_key='an-key', cap_usd='50',
                         arms=pilot.BLIND_ARMS, blind=True,
                         transports={'openrouter': httpx.MockTransport(refuse), 'anthropic': httpx.MockTransport(refuse)},
                         **pilot.blind_setup())
    again = {r['arm']: r for r in rows}
    assert {k: v['real_usd'] for k, v in again.items()} == {k: v['real_usd'] for k, v in first.items()}


def test_blind_worst_case_reserves_reviewer_and_advisor():
    worst = pilot.compare_worst_case_usd(**pilot.blind_setup())
    assert worst['cloudyfan_plus'] > worst['cloudyfan'] > 0
    assert worst['advisor_first'] > 0 and worst['fixed_high'] > 0


def test_blind_compare_refuses_without_sandbox_before_any_request(tmp_path, monkeypatch):
    sandbox = types.ModuleType('cloudyfan_jev.code_sandbox')
    sandbox.sandbox_available = lambda: False
    monkeypatch.setitem(sys.modules, 'cloudyfan_jev.code_sandbox', sandbox)

    def refuse(request):
        raise AssertionError('no request may be sent')
    with pytest.raises(RuntimeError, match='sandbox-exec'):
        pilot.compare(tmp_path / 'blind', TASKS, openrouter_key='or-key', anthropic_key='an-key', cap_usd='50',
                      arms=pilot.BLIND_ARMS, blind=True,
                      transports={'openrouter': httpx.MockTransport(refuse), 'anthropic': httpx.MockTransport(refuse)},
                      **pilot.blind_setup())


def _memorized(task):
    """只背下公開範例的程式：過得了公開驗收、過不了隱藏測試（誤放行的典型）。"""
    cases = json.dumps([[c['args'], c['expected']] for c in task['public_tests']])
    return ('```python\nimport json\nCASES = json.loads(' + repr(cases) + ')\n'
            'def ' + task['entry_point'] + '(*args):\n'
            '    for a, e in CASES:\n        if a == json.loads(json.dumps(list(args))):\n            return e\n'
            '    return None\n```')


@pytest.mark.skipif(not __import__('cloudyfan_jev.code_sandbox', fromlist=['x']).sandbox_available(),
                    reason='needs macOS sandbox-exec')
def test_real_code_tasks_flag_memorized_answers_as_false_accepts(tmp_path):
    from cloudyfan_jev import code_tasks
    task = code_tasks.frozen_code_tasks()[0]

    class Memorizer(Anthropic):
        def __call__(self, request):
            body = json.loads(request.content)
            system = json.dumps(body.get('system'))
            if 'advisor of a model-routing service' in system or 'You review a candidate answer' in system:
                return super().__call__(request)
            self.calls.append((body['model'], None, ''))
            return httpx.Response(200, json={'id': f'msg_{len(self.calls):04d}', 'type': 'message',
                'role': 'assistant', 'model': body['model'], 'content': [{'type': 'text', 'text': _memorized(task)}],
                'stop_reason': 'end_turn', 'usage': {'input_tokens': 1000, 'output_tokens': 200}})
    anth = Memorizer()
    rows = pilot.compare(tmp_path / 'blind', [task], openrouter_key='or-key', anthropic_key='an-key', cap_usd='50',
                         arms=('cloudyfan', 'advisor_first'), blind=True,
                         transports={'openrouter': httpx.MockTransport(Jev()), 'anthropic': httpx.MockTransport(anth)},
                         **pilot.blind_setup())
    for row in rows:
        assert row['final_pass'] is True and row['hidden_final_pass'] is False and row['false_accept'] is True
        passed, total = row['hidden_counts'][-1]
        assert total == len(task['hidden_tests']) and passed < total


@pytest.mark.skipif(not __import__('cloudyfan_jev.code_sandbox', fromlist=['x']).sandbox_available(),
                    reason='needs macOS sandbox-exec')
def test_blind_calibration_measures_first_attempt_false_accepts(tmp_path):
    from cloudyfan_jev import code_tasks
    task = code_tasks.frozen_code_tasks()[1]

    def answer(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': 'msg_cal', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': [{'type': 'text', 'text': _memorized(task)}],
            'stop_reason': 'end_turn', 'usage': {'input_tokens': 1500, 'output_tokens': 900}})
    rows = pilot.calibrate(tmp_path / 'cal', [task], pilot.anthropic_target('claude-sonnet-5-5', effort='xhigh'),
                           anthropic_key='an-key', cap_usd='1', blind=True, max_input_bytes=24576,
                           transports={'anthropic': httpx.MockTransport(answer)})
    row = rows[0]
    assert row['first_pass'] is True and row['first_false_accept'] is True and row['attempts'] == 1
    report = json.loads((tmp_path / 'cal' / 'calibration-report.json').read_text())
    assert report['summary']['first_false_accept'] == 1 and report['summary']['hidden_first_pass'] == 0
