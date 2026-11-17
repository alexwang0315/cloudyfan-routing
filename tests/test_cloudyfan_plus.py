"""#79：路徑一加強版（cloudyfan_plus，ADR 0034 第 8 點、ADR 0036 第 10 點）。

全部離線：只用 httpx.MockTransport，不呼叫任何真實模型。
"""
import copy
import json

import httpx
import pytest

from cloudyfan_jev.client import Client
from cloudyfan_jev.cloudyfan_plus import CLOUDYFAN_PLUS, CloudyfanPlusConfig
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.loop_guard import LoopGuardConfig

TASK = {'prompt': 'Return only the integer sum of 1 and 1.'}
JUDGE_MODEL = 'oss/judge-v1'
REDIRECT = 'Stop calling the tool; answer the integer directly.'
SYNTAX = {'ok': False, 'error': {'code': 'syntax_error', 'category': 'content', 'message': 'near SELEC'}}
TOOL = {'tool': 'query', 'arguments': {'sql': 'SELEC 1'}}


def judge_profile(open_source=True):
    return {'model': 'oss/judge', 'model_version': JUDGE_MODEL, 'accepted_models': [JUDGE_MODEL],
            'provider': 'example', 'provider_name': 'Example', 'effort': 'medium',
            'open_source': open_source}


def plus_config(mode='active', **overrides):
    fields = {'judge': judge_profile(), 'loop_guard': LoopGuardConfig(mode=mode)}
    fields.update(overrides)
    return CloudyfanPlusConfig(**fields)


class ToolEnvironment:
    replayable_tools = ('query',)

    def __init__(self, result=SYNTAX):
        self.result = result

    def tools_schema(self):
        return [{'type': 'function', 'function': {'name': 'query', 'parameters': {'type': 'object'}}}]

    def call(self, name, arguments):
        return copy.deepcopy(self.result)

    def validate(self, final_text):
        passed = final_text.strip() == '2'
        return {'passed': passed, 'checks': [] if passed else ['integer_sum_contract_failed']}


class API:
    """各層模型依腳本逐回合回應；Jev 依序選層；裁判回固定判斷。"""
    def __init__(self, scripts=None, verdict=None, choices=('low',)):
        self.scripts = {m: list(s) for m, s in (scripts or {}).items()}
        self.verdict = verdict
        self.choices = iter(choices)
        self.requests = []

    def by_model(self, model):
        return [p for p in self.requests if p['model'] == model]

    def decisions(self):
        return [p for p in self.requests if 'questions' in p]

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(copy.deepcopy(p))
        i = len(self.requests)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(p['questions']))
            choice = next(self.choices, 'high' if stage == 'mid_high' else 'low')
            return httpx.Response(200, json={'id': f'gen-{i}', 'model': p['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0001, 'input_tokens': 10, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': choice,
                    'probabilities': {k: .8 if k == choice else .2 for k in p['questions'][stage]['criteria']}}}})
        message = {'role': 'assistant'}
        if p['model'] == JUDGE_MODEL:
            message['content'] = json.dumps(self.verdict)
        else:
            steps = self.scripts.get(p['model'], ['3' if p['model'] == 'example/open_source' else '2'])
            step = steps.pop(0) if len(steps) > 1 else steps[0]
            if isinstance(step, dict):
                message['content'] = None
                message['tool_calls'] = [{'id': f"tc-{len(self.by_model(p['model']))}", 'type': 'function', 'function': {
                    'name': step['tool'], 'arguments': json.dumps(step['arguments'])}}]
            else:
                message['content'] = step
        return httpx.Response(200, json={'id': f'gen-{i}', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': .01}, 'choices': [{'message': message, 'finish_reason': 'stop'}]})


def make_core(root, api, plus=None):
    http = httpx.Client(transport=httpx.MockTransport(api))
    profiles, decision = example_config()
    return Core(Client(http, 'mock-only-key', root), profiles, decision, cloudyfan_plus=plus)


def stuck(confidence=0.9):
    return {'stuck': True, 'confidence': confidence, 'redirect': REDIRECT}


# ── 預設關閉 ───────────────────────────────────────────────

def test_mode_is_not_available_without_explicit_opt_in(tmp_path):
    engine = make_core(tmp_path, API())
    assert CLOUDYFAN_PLUS not in engine.workflow_modes()
    with pytest.raises(ValueError, match='Unknown workflow mode'):
        engine.run('r', TASK, ArithmeticEnvironment, contract_version='v1', mode=CLOUDYFAN_PLUS)
    assert CLOUDYFAN_PLUS in make_core(tmp_path / 'p', API(), plus_config()).workflow_modes()


def _stable(value):
    """去掉牆鐘計時欄位（每次執行必然不同），其餘逐欄比對。"""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k != 'wall_seconds' and not k.endswith('_ms')}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def test_plain_cloudyfan_is_byte_identical_when_plus_is_configured(tmp_path):
    outputs = []
    for name, plus in (('plain', None), ('plus', plus_config())):
        api = API({'example/open_source': [TOOL, TOOL, TOOL, '3']}, stuck())
        engine = make_core(tmp_path / name, api, plus)
        result = engine.run('r', TASK, ToolEnvironment, contract_version='v1')
        manifest = (tmp_path / name / 'runs' / 'r' / 'manifest.json').read_bytes()
        result_file = json.loads((tmp_path / name / 'runs' / 'r' / 'result.json').read_text())
        outputs.append((api.requests, manifest, _stable(result_file), _stable(result)))
        assert not api.by_model(JUDGE_MODEL)
    assert outputs[0] == outputs[1]


def run_plus(engine, env=ToolEnvironment, run_id='r', **kwargs):
    return engine.run(run_id, TASK, env, contract_version='v1', mode=CLOUDYFAN_PLUS, **kwargs)


# ── 迴圈守衛：插過仍卡住就結束本次作答，交回 Jev ─────────────

def test_still_stuck_after_redirect_ends_attempt_and_hands_back_to_jev(tmp_path):
    api = API({'example/open_source': [TOOL, TOOL, TOOL, '3'], 'example/low': ['2']}, stuck())
    result = run_plus(make_core(tmp_path, api, plus_config()))
    first, second = result['attempts']
    assert first['tier'] == 'open_source' and first['stop'] == 'loop_guard_stuck'
    assert first['public']['passed'] is False and first['public']['checks'] == ['LOOP_GUARD_STUCK']
    assert first['loop_guard']['action'] == 'redirected_still_stuck'
    # 開源首答只送了 3 回合：第 3 回合後守衛結束作答，沒有第 4 回合。
    assert len(api.by_model('example/open_source')) == 3
    assert len(api.by_model(JUDGE_MODEL)) == 1
    # 交回 Jev：照原規則在 low_mid 階段分類一次，選 low。
    assert len(api.decisions()) == 1 and 'LOOP_GUARD_STUCK' in json.dumps(api.decisions()[0])
    assert second['tier'] == 'low' and result['status'] == 'passed' and result['mode'] == CLOUDYFAN_PLUS
    assert result['selection']['classifier_calls'] == 1
    assert result['costs']['judge_usd'] == '0.01' and result['costs']['calls'] == 6
    # 開源裁判依明示標記進入零模型費用情境：開源首答與裁判都不計。
    assert result['costs']['hypothetical_open_source_zero_usd'] == '0.0101'


def test_redirect_that_works_keeps_the_attempt_and_skips_jev(tmp_path):
    api = API({'example/open_source': [TOOL, TOOL, '2']}, stuck())
    result = run_plus(make_core(tmp_path, api, plus_config()))
    assert [a['tier'] for a in result['attempts']] == ['open_source']
    assert result['attempts'][0]['loop_guard']['action'] == 'redirected'
    assert api.by_model('example/open_source')[2]['messages'][-1]['content'].endswith(REDIRECT)
    assert not api.decisions() and result['status'] == 'passed'


def test_shadow_mode_never_changes_the_jev_path(tmp_path):
    scripts = {'example/open_source': [TOOL, TOOL, TOOL, '3'], 'example/low': ['2']}
    plain_api, shadow_api = API(copy.deepcopy(scripts)), API(copy.deepcopy(scripts), stuck())
    plain = make_core(tmp_path / 'plain', plain_api).run('r', TASK, ToolEnvironment, contract_version='v1')
    shadow = run_plus(make_core(tmp_path / 'shadow', shadow_api, plus_config(mode='shadow')))
    generation = [p for p in shadow_api.requests if p['model'] != JUDGE_MODEL]
    assert generation == plain_api.requests
    assert shadow['attempts'][0]['loop_guard']['action'] == 'shadow_recorded'
    assert [a['tier'] for a in shadow['attempts']] == [a['tier'] for a in plain['attempts']]


def test_judge_disabled_runs_plain_attempts(tmp_path):
    api = API({'example/open_source': [TOOL, TOOL, TOOL, '3'], 'example/low': ['2']}, stuck())
    result = run_plus(make_core(tmp_path, api, plus_config(judge_calls_per_attempt=0)))
    assert not api.by_model(JUDGE_MODEL)
    assert all('loop_guard' not in a for a in result['attempts'])


# ── 解題事實與成績 ─────────────────────────────────────────

from cloudyfan_jev.experience import ExperienceStore  # noqa: E402
from cloudyfan_jev.experience_card import ExperienceLink  # noqa: E402

SCOPE = 'arith_v1'
CONTEXT = {'client_id': 'c1', 'scope': SCOPE, 'created_at': '2026-11-17'}
FACT = {'kind': 'check_code', 'code': 'integer_sum_contract_failed'}
STATS_WORDS = ('pass_rate', 'dispatches', 'mean_cost_usd', 'verified', 'advisor_direct', 'sample',
               'cloudyfan-plus-v1', 'experience-ec-', '參考區塊')


def seeded_store(stats=0):
    store = ExperienceStore(rewriter=lambda fact: '此類題目常見錯誤代碼 ' + fact['code'] + '。')
    for i in range(3):
        store.record({'task_id': f'old{i}', 'client_id': 'c1', 'scope': SCOPE,
                      'settings': store.freeze_settings('c1'), 'created_at': '2026-11-01',
                      'supports': [FACT], 'outcomes': []})
    for i in range(stats):
        store.record({'task_id': f'stat{i}', 'client_id': 'c1', 'scope': SCOPE,
                      'settings': store.freeze_settings('c1'), 'created_at': '2026-11-02', 'supports': [],
                      'outcomes': [{'role': 'solver', 'model_version': 'example/open_source', 'effort': 'low',
                                    'provider': 'Example', 'verdict': 'failed', 'cost_usd': '0.01',
                                    'pool_snapshot': 'cloudyfan-plus-v1'}]})
    return store


def test_solving_facts_are_appended_as_a_versioned_reference_block(tmp_path):
    api = API()
    store = seeded_store()
    result = run_plus(make_core(tmp_path, api, plus_config(experience=ExperienceLink(store))),
                      env=ArithmeticEnvironment, experience=CONTEXT)
    card = json.loads((tmp_path / 'runs' / 'r' / 'experience-facts.json').read_text())
    block = 'experience-' + card['view']['card']
    user = api.by_model('example/open_source')[0]['messages'][1]['content']
    assert user.startswith(TASK['prompt'] + '\n\n[參考區塊 ' + block + ']\n')
    assert 'check_code: integer_sum_contract_failed' in user
    assert result['experience'] == {'card': card['view']['card'], 'store_version': card['store_version'],
                                    'facts_block': block}
    assert card['version'] == 'cloudyfan-plus-facts-v1' and set(card['view']) == {
        'card', 'scope', 'facts', 'facts_omitted'}
    manifest = json.loads((tmp_path / 'runs' / 'r' / 'manifest.json').read_text())
    assert manifest['cloudyfan_plus']['version'] == 'cloudyfan-plus-v1'
    assert manifest['cloudyfan_plus']['stats_policy'] == 'record_only_not_routed'
    assert manifest['experience'] == CONTEXT


def test_jev_inputs_never_contain_experience_stats(tmp_path):
    # 經驗庫已有 ≥ 5 題成績；Jev 請求仍只有原題與公開診斷。
    api = API({'example/open_source': ['3'], 'example/low': ['3'], 'example/mid': ['2']},
              choices=('low', 'mid'))
    store = seeded_store(stats=6)
    result = run_plus(make_core(tmp_path, api, plus_config(experience=ExperienceLink(store))),
                      env=ArithmeticEnvironment, experience=CONTEXT)
    assert len(api.decisions()) == 2 and result['status'] == 'passed'
    for decision in api.decisions():
        text = json.dumps(decision, ensure_ascii=False)
        assert decision['state']['task'] == TASK['prompt']
        for word in STATS_WORDS:
            assert word not in text


def test_model_stats_are_recorded_but_routing_is_identical_to_plain_jev(tmp_path):
    scripts = {'example/open_source': ['3'], 'example/low': ['2']}
    plain_api = API(copy.deepcopy(scripts))
    make_core(tmp_path / 'plain', plain_api).run('r', TASK, ArithmeticEnvironment, contract_version='v1')
    for stats in (0, 6):
        api, store = API(copy.deepcopy(scripts)), seeded_store(stats=stats)
        run_plus(make_core(tmp_path / f'plus{stats}', api, plus_config(experience=ExperienceLink(store))),
                 env=ArithmeticEnvironment, experience=CONTEXT)
        assert api.decisions() == plain_api.decisions()
        rows = {(r['model_version'], r['verified'], r['passed'])
                for r in store.model_stats(SCOPE, pool_snapshot='cloudyfan-plus-v1')}
        assert ('example/low', 1, 1) in rows
        assert ('example/open_source', stats + 1, 0) in rows
    # 路徑一加強版的成績不混進路徑二的模型池快照。
    assert store.model_stats(SCOPE, pool_snapshot='snapshot-20261117-a') == []


def test_frozen_facts_are_reused_on_resume_and_tampering_is_refused(tmp_path):
    store = seeded_store()
    engine = make_core(tmp_path, API(), plus_config(experience=ExperienceLink(store)))
    first = run_plus(engine, env=ArithmeticEnvironment, experience=CONTEXT)
    # 經驗庫之後變了，續跑仍用凍結的同一版，且回寫只算一次。
    store.record({'task_id': 'later', 'client_id': 'c1', 'scope': SCOPE, 'settings': store.freeze_settings('c1'),
                  'created_at': '2026-11-17', 'supports': [{'kind': 'check_code', 'code': 'other'}],
                  'outcomes': []})
    version = store.export()['body']['version']
    again = run_plus(engine, env=ArithmeticEnvironment, experience=CONTEXT)
    assert again['experience'] == first['experience'] and store.export()['body']['version'] == version
    path = tmp_path / 'runs' / 'r' / 'experience-facts.json'
    card = json.loads(path.read_text())
    card['view']['facts'] = []
    path.chmod(0o600)
    path.write_text(json.dumps(card))
    with pytest.raises(ValueError, match='unverifiable'):
        run_plus(engine, env=ArithmeticEnvironment, experience=CONTEXT)


def test_changed_plus_configuration_refuses_resume(tmp_path):
    run_plus(make_core(tmp_path, API(), plus_config()), env=ArithmeticEnvironment)
    with pytest.raises(ValueError, match='Resume evidence changed'):
        run_plus(make_core(tmp_path, API(), plus_config(mode='shadow')), env=ArithmeticEnvironment)
    with pytest.raises(ValueError, match='Completed result configuration changed'):
        make_core(tmp_path, API(), plus_config(mode='shadow')).read_completed('r')
    assert make_core(tmp_path, API(), plus_config()).read_completed('r')['mode'] == CLOUDYFAN_PLUS


# ── 路徑一不選用時仍拒收經驗 ───────────────────────────────

def test_plain_path_one_still_rejects_experience(tmp_path):
    engine = make_core(tmp_path, API(), plus_config(experience=ExperienceLink(seeded_store())))
    for mode in ('cloudyfan', 'fixed_high', 'low_first', 'open_source_first'):
        with pytest.raises(ValueError, match='only available'):
            engine.run('r', TASK, ArithmeticEnvironment, contract_version='v1', mode=mode, experience=CONTEXT)
    assert not (tmp_path / 'runs').exists()


def test_plus_without_experience_link_rejects_experience(tmp_path):
    with pytest.raises(ValueError, match='ExperienceLink'):
        run_plus(make_core(tmp_path, API(), plus_config()), env=ArithmeticEnvironment, experience=CONTEXT)
    with pytest.raises(ValueError, match='experience requires'):
        run_plus(make_core(tmp_path, API(), plus_config(experience=ExperienceLink(seeded_store()))),
                 env=ArithmeticEnvironment, experience={'client_id': 'c 1', 'scope': SCOPE,
                                                        'created_at': '2026-11-17'})


def test_plus_cannot_combine_with_qcs_selection_policy(tmp_path):
    from types import SimpleNamespace
    engine = make_core(tmp_path, API(), plus_config())
    engine.selection_policy = SimpleNamespace(plan={'policy': {'workload_id': 'w'}})
    with pytest.raises(ValueError, match='QCS requires'):
        engine.run('r', {**TASK, 'workload_id': 'w'}, ArithmeticEnvironment, contract_version='v1',
                   mode=CLOUDYFAN_PLUS)


def test_repair_attempt_repeating_previous_validation_checks_triggers_judge(tmp_path):
    same = {'passed': False, 'checks': ['integer_sum_contract_failed']}
    api = API({'example/open_source': ['3'], 'example/low': [TOOL, '2']}, stuck())
    result = run_plus(make_core(tmp_path, api, plus_config()), env=lambda: ToolEnvironment(same))
    assert 'loop_guard' in result['attempts'][0] and not result['attempts'][0]['loop_guard']['triggered']
    guard = result['attempts'][1]['loop_guard']
    assert guard['trigger'] == {'kind': 'validation', 'fingerprint': {'checks': ['integer_sum_contract_failed']},
                                'count': 1}
    assert guard['judge']['call_id'] == 'run-r-a1-judge' and result['status'] == 'passed'


# ── 預算預留計入裁判 ───────────────────────────────────────

def budgeted(tmp_path, plus, bounds):
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import BudgetedCore, BudgetStore
    from cloudyfan_jev.engine import Limits
    http = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No request expected')))
    profiles, decision = example_config()
    engine = Core(Client(http, 'mock-only-key', tmp_path / 'client'), profiles, decision,
                  limits=Limits(max_turns=2), cloudyfan_plus=plus)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
    return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={},
                        verify_bound=lambda *args: True)


def bounds(judge='.0005'):
    from cloudyfan_jev.budget import EnforcedBound
    usd = {'example/open_source': '.03', 'example/low': '.01', 'example/mid': '.02',
           'example/high': '.05', 'typesafe/jev-1.13': '.001', JUDGE_MODEL: judge}
    return {k: EnforcedBound(v, 'offline hard-cap fixture') for k, v in usd.items()}


def test_reservation_adds_one_judge_call_per_reachable_attempt(tmp_path):
    from decimal import Decimal
    app = budgeted(tmp_path, plus_config(), bounds())
    # 純 cloudyfan：(.03 + .02 + .05) × 2 回合 ＋ Jev 2 次 × .001 = .202；加 3 次作答 × 裁判 .0005。
    assert app._admit('cloudyfan') == Decimal('0.202')
    assert app._admit(CLOUDYFAN_PLUS) == Decimal('0.2035')


def test_reservation_refuses_without_judge_bound_unless_judge_disabled(tmp_path):
    from decimal import Decimal
    from cloudyfan_jev.budget import BudgetError
    missing = bounds()
    del missing[JUDGE_MODEL]
    with pytest.raises(BudgetError, match='upper bound'):
        budgeted(tmp_path / 'a', plus_config(), missing)._admit(CLOUDYFAN_PLUS)
    assert budgeted(tmp_path / 'b', plus_config(judge_calls_per_attempt=0),
                    missing)._admit(CLOUDYFAN_PLUS) == Decimal('0.202')
    with pytest.raises(ValueError, match='Unknown workflow mode'):
        budgeted(tmp_path / 'c', None, bounds())._admit(CLOUDYFAN_PLUS)


def test_budgeted_run_freezes_plus_configuration_in_reservation(tmp_path):
    api = API({'example/open_source': [TOOL, TOOL, TOOL, '3'], 'example/low': ['2']}, stuck())
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import (Authorization, BudgetedCore, BudgetError, BudgetStore,
                                      EnforcedBound, ToolBound)
    tools = {'query': ToolBound(EnforcedBound('0', 'offline local tool'), external=False)}
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 11, 17, tzinfo=timezone.utc))
    store.authorize(Authorization('offline', '10', '2026-12-31T00:00:00+00:00'))

    def app(plus):
        return BudgetedCore(make_core(tmp_path / 'client', api, plus), store, model_bounds=bounds(judge='.01'),
                            tool_bounds=tools, verify_bound=lambda *args: True)
    result = app(plus_config()).run('r', TASK, ToolEnvironment, contract_version='v1', mode=CLOUDYFAN_PLUS)
    assert result['status'] == 'passed' and result['budget']['run_total_usd'] == '0.0501'
    with pytest.raises(BudgetError, match='Frozen reservation changed'):
        app(plus_config(mode='shadow')).run('r', TASK, ToolEnvironment, contract_version='v1',
                                            mode=CLOUDYFAN_PLUS)


# ── 契約註冊（客戶端層級設定在 Monitor，#80）──────────────────

def registered(tmp_path, plus, mode=CLOUDYFAN_PLUS):
    from cloudyfan_jev.http_api import RegisteredContract
    from cloudyfan_jev.privacy import ExecutionPolicy
    from test_budget import controlled
    from test_engine import API as EngineAPI
    runner = controlled(tmp_path / 'runner', EngineAPI(first='2'))
    runner.core.execution_policy = ExecutionPolicy(mode='unrestricted', origin='home:fixture',
                                                   classifier='cloud:typesafe')
    runner.core.cloudyfan_plus = plus
    return RegisteredContract('public-v1', runner, lambda workspace: ArithmeticEnvironment(), mode=mode)


def test_contract_selecting_plus_requires_and_freezes_its_configuration(tmp_path):
    with pytest.raises(ValueError, match='cloudyfan_plus'):
        registered(tmp_path / 'a', None).descriptor()
    descriptor = registered(tmp_path / 'b', plus_config()).descriptor()
    assert descriptor['mode'] == CLOUDYFAN_PLUS
    assert descriptor['cloudyfan_plus']['version'] == 'cloudyfan-plus-v1'
    plain = registered(tmp_path / 'c', None, mode='cloudyfan').descriptor()
    with_plus = registered(tmp_path / 'c', plus_config(), mode='cloudyfan').descriptor()
    assert plain == with_plus


def test_repair_attempt_does_not_count_earlier_attempt_tool_errors(tmp_path):
    # 開源首答與修復作答各只撞一次同樣的工具錯誤；修復作答的守衛不得把首答的錯誤算進來。
    api = API({'example/open_source': [TOOL, '3'], 'example/low': [TOOL, '2']}, stuck())
    result = run_plus(make_core(tmp_path, api, plus_config()))
    first, second = result['attempts']
    assert second['tier'] == 'low' and result['status'] == 'passed'
    assert first['loop_guard']['triggered'] is False
    assert second['loop_guard']['triggered'] is False
    assert not api.by_model(JUDGE_MODEL)
