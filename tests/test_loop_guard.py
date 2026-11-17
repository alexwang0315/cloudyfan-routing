"""#74：迴圈守衛裁判（ADR 0034 第 10 點）。

全部離線：只用 httpx.MockTransport，不呼叫任何真實模型。
池內模型依腳本回工具呼叫或最終答案；裁判（oss/judge-v1）回固定 JSON。
"""
import copy
import json

import httpx
import pytest

from cloudyfan_jev.advisor import ADVISOR_FIRST, AdvisorConfig
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.loop_guard import LoopGuardConfig

TASK = {'prompt': 'Return only the integer sum of 1 and 1.'}
ADVISOR_MODEL, JUDGE_MODEL = 'oss/advisor-v1', 'oss/judge-v1'
REDIRECT = 'Stop calling the tool; answer the integer directly.'
SYNTAX = {'ok': False, 'error': {'code': 'syntax_error', 'category': 'content', 'message': 'near SELEC'}}


def role_profile(name):
    return {'model': 'oss/' + name, 'model_version': 'oss/' + name + '-v1',
            'accepted_models': ['oss/' + name + '-v1'], 'provider': 'example',
            'provider_name': 'Example', 'effort': 'medium', 'open_source': True}


def config(mode='active', **overrides):
    fields = {'advisor': role_profile('advisor'), 'judge': role_profile('judge'),
              'score_snapshot': 'snapshot-20261117-a', 'loop_guard': LoopGuardConfig(mode=mode)}
    fields.update(overrides)
    return AdvisorConfig(**fields)


def tool(name='query', arguments=None):
    return {'tool': name, 'arguments': arguments or {'sql': 'SELEC 1'}}


class ToolEnvironment:
    """有一個工具的環境；每次呼叫依序回傳預設結果（用完就重複最後一個）。"""
    replayable_tools = ('query', 'other')

    def __init__(self, results=None):
        self.results = list(results or [SYNTAX])
        self.calls = []

    def tools_schema(self):
        return [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
                for name in ('query', 'other')]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return copy.deepcopy(self.results[min(len(self.calls), len(self.results)) - 1])

    def validate(self, final_text):
        passed = final_text.strip() == '2'
        return {'passed': passed, 'checks': [] if passed else ['integer_sum_contract_failed']}


def env_factory(results=None):
    def make():
        return ToolEnvironment(results)
    return make


class API:
    """顧問改派 mid、high；池內模型依腳本逐回合回應；裁判回固定判斷。"""
    def __init__(self, scripts, verdict=None, *, judge_status=200):
        self.scripts = {model: list(steps) for model, steps in scripts.items()}
        self.verdict = verdict
        self.judge_status = judge_status
        self.requests = []

    def by_model(self, model):
        return [p for p in self.requests if p['model'] == model]

    def __call__(self, request):
        p = json.loads(request.content)
        self.requests.append(copy.deepcopy(p))
        i = len(self.requests)
        message = {'role': 'assistant'}
        if p['model'] == ADVISOR_MODEL:
            message['content'] = json.dumps({'mode': 'delegate', 'recommendations': [
                {'setting': 'mid', 'tier': 'mid', 'reason': 'r'},
                {'setting': 'high', 'tier': 'high', 'reason': 'r'}]})
        elif p['model'] == JUDGE_MODEL:
            if self.judge_status != 200:
                return httpx.Response(self.judge_status, json={
                    'id': f'gen-{i}', 'provider': 'Example', 'usage': {'cost': 0},
                    'error': {'code': 429, 'message': 'rate limited'}})
            message['content'] = (self.verdict if isinstance(self.verdict, str)
                                  else json.dumps(self.verdict))
        else:
            steps = self.scripts.get(p['model'], ['2'])
            step = steps.pop(0) if len(steps) > 1 else steps[0]
            if isinstance(step, dict):
                message['content'] = None
                message['tool_calls'] = [{'id': f"tc-{len(self.by_model(p['model']))}", 'type': 'function', 'function': {
                    'name': step['tool'], 'arguments': json.dumps(step['arguments'])}}]
            else:
                message['content'] = step
        return httpx.Response(200, json={'id': f'gen-{i}', 'model': p['model'], 'provider': 'Example',
            'usage': {'cost': .01}, 'choices': [{'message': message, 'finish_reason': 'stop'}]})


def make_core(tmp_path, api, advisor=None):
    http = httpx.Client(transport=httpx.MockTransport(api))
    profiles, decision_profile = example_config()
    return Core(Client(http, 'mock-only-key', tmp_path), profiles, decision_profile,
                advisor_first=advisor or config())


def run(engine, env=None, run_id='r'):
    return engine.run(run_id, TASK, env or env_factory(), contract_version='v1', mode=ADVISOR_FIRST)


def stuck(confidence=0.9, redirect=REDIRECT):
    return {'stuck': True, 'confidence': confidence, 'redirect': redirect}


def user_texts(payload):
    return [m['content'] for m in payload['messages'] if m['role'] == 'user']


# ── 偵測與插入提示 ─────────────────────────────────────────

def test_repeated_tool_error_triggers_judge_and_inserts_redirect(tmp_path):
    api = API({'example/mid': [tool(), tool(), '2']}, stuck())
    result = run(make_core(tmp_path, api), env_factory())
    assert len(api.by_model(JUDGE_MODEL)) == 1
    # 第三回合的作答請求帶著換方向提示。
    third = api.by_model('example/mid')[2]
    assert user_texts(third)[-1].endswith(REDIRECT)
    assert result['status'] == 'passed'
    guard = result['attempts'][0]['loop_guard']
    assert guard['action'] == 'redirected'
    assert guard['trigger']['kind'] == 'tool_error'
    assert guard['trigger']['fingerprint'] == {'tool': 'query', 'code': 'syntax_error', 'category': 'content'}
    assert guard['judge']['call_id'] == 'run-r-a1-judge'
    assert result['costs']['judge_usd'] == '0.01'


def test_single_error_or_distinct_errors_do_not_trigger(tmp_path):
    other = {'ok': False, 'error': {'code': 'unknown_table', 'category': 'content', 'message': 'x'}}
    api = API({'example/mid': [tool(), tool('other'), '2']}, stuck())
    result = run(make_core(tmp_path, api), env_factory([SYNTAX, other]))
    assert api.by_model(JUDGE_MODEL) == []
    assert result['attempts'][0]['loop_guard'] == {'triggered': False, 'calls': []}
    assert result['costs']['judge_usd'] == '0'


@pytest.mark.parametrize('error', [
    {'code': 'rate_limited', 'category': 'content'},
    {'code': 'x', 'category': 'operational'},
    {'code': 'x', 'category': 'timeout'},
    {'code': 'provider_unavailable', 'category': 'provider'},
])
def test_operational_failures_are_not_loop_signals(tmp_path, error):
    failure = {'ok': False, 'error': {**error, 'message': 'try later'}}
    api = API({'example/mid': [tool(), tool(), tool(), '2']}, stuck())
    run(make_core(tmp_path, api), env_factory([failure]))
    assert api.by_model(JUDGE_MODEL) == []


def test_repeated_validation_check_codes_trigger(tmp_path):
    fail = {'passed': False, 'checks': ['SQL_RESULT_COLUMNS', 'SQL_ORDER_BY_REQUIRED']}
    same = {'passed': False, 'checks': ['SQL_ORDER_BY_REQUIRED', 'SQL_RESULT_COLUMNS']}
    api = API({'example/mid': [tool(), tool(), '2']}, stuck())
    result = run(make_core(tmp_path, api), env_factory([fail, same]))
    guard = result['attempts'][0]['loop_guard']
    assert guard['trigger']['kind'] == 'validation'
    assert guard['trigger']['fingerprint'] == {'checks': ['SQL_ORDER_BY_REQUIRED', 'SQL_RESULT_COLUMNS']}


def test_changed_validation_check_codes_do_not_trigger(tmp_path):
    api = API({'example/mid': [tool(), tool(), '2']}, stuck())
    run(make_core(tmp_path, api), env_factory([{'passed': False, 'checks': ['A']},
                                               {'passed': False, 'checks': ['B']}]))
    assert api.by_model(JUDGE_MODEL) == []


@pytest.mark.parametrize('verdict,action', [
    (stuck(confidence=0.69), 'below_threshold'),
    ({'stuck': False, 'confidence': 0.95, 'redirect': ''}, 'not_stuck'),
])
def test_no_redirect_without_confident_stuck_verdict(tmp_path, verdict, action):
    api = API({'example/mid': [tool(), tool(), '2']}, verdict)
    result = run(make_core(tmp_path, api))
    assert not any(REDIRECT in t for p in api.by_model('example/mid') for t in user_texts(p))
    assert result['attempts'][0]['loop_guard']['action'] == action
    assert result['attempts'][0]['loop_guard']['would_redirect'] is False
    assert result['status'] == 'passed'


def test_confidence_exactly_at_threshold_redirects(tmp_path):
    api = API({'example/mid': [tool(), tool(), '2']}, stuck(confidence=0.7))
    result = run(make_core(tmp_path, api))
    assert result['attempts'][0]['loop_guard']['action'] == 'redirected'


@pytest.mark.parametrize('content', [
    'not json', '```json\n{"stuck": true, "confidence": 0.9, "redirect": "x"}\n```',
    json.dumps({'stuck': 'yes', 'confidence': 0.9, 'redirect': 'x'}),
    json.dumps({'stuck': True, 'confidence': 1.5, 'redirect': 'x'}),
    json.dumps({'stuck': True, 'confidence': True, 'redirect': 'x'}),
    json.dumps({'stuck': True, 'confidence': 0.9, 'redirect': ''}),
    json.dumps({'stuck': True, 'confidence': 0.9, 'redirect': 'x', 'extra': 1}),
    json.dumps({'stuck': True, 'confidence': 0.9, 'redirect': 'x' * 501}),
])
def test_invalid_judge_json_never_redirects(tmp_path, content):
    api = API({'example/mid': [tool(), tool(), '2']}, content)
    result = run(make_core(tmp_path, api))
    guard = result['attempts'][0]['loop_guard']
    assert guard['action'] == 'judge_invalid' and guard['judge']['outcome'] == 'judge_invalid'
    assert len(api.by_model('example/mid')[2]['messages']) == 6  # 沒有多插提示


def test_judge_rate_limit_is_recorded_without_redirect(tmp_path):
    api = API({'example/mid': [tool(), tool(), '2']}, stuck(), judge_status=429)
    result = run(make_core(tmp_path, api))
    assert result['attempts'][0]['loop_guard']['action'] == 'judge_rejected'
    assert result['status'] == 'passed'


def test_at_most_one_judge_call_per_attempt(tmp_path):
    api = API({'example/mid': [tool(), tool(), tool(), tool(), '2']},
              {'stuck': False, 'confidence': 0.9, 'redirect': ''})
    run(make_core(tmp_path, api))
    assert len(api.by_model(JUDGE_MODEL)) == 1


# ── 仍卡住就結束本次作答，改派下一組建議 ─────────────────────

def test_still_stuck_after_redirect_ends_attempt_and_dispatches_next(tmp_path):
    api = API({'example/mid': [tool(), tool(), tool(), '2'], 'example/high': ['2']}, stuck())
    result = run(make_core(tmp_path, api))
    first, second = result['attempts']
    assert first['stop'] == 'loop_guard_stuck' and first['public']['passed'] is False
    assert first['public']['checks'] == ['LOOP_GUARD_STUCK']
    assert first['loop_guard']['action'] == 'redirected_still_stuck'
    assert first['loop_guard']['ended_attempt'] is True
    assert len(api.by_model('example/mid')) == 3  # 第 4 回合沒有發出
    assert second['setting'] == 'high' and result['status'] == 'passed'
    # 第二次作答有自己的守衛額度；這次沒觸發。
    assert second['loop_guard']['triggered'] is False
    assert len(api.by_model(JUDGE_MODEL)) == 1


def test_redirect_followed_by_new_different_error_keeps_going(tmp_path):
    other = {'ok': False, 'error': {'code': 'unknown_table', 'category': 'content', 'message': 'x'}}
    api = API({'example/mid': [tool(), tool(), tool('other'), '2']}, stuck())
    result = run(make_core(tmp_path, api), env_factory([SYNTAX, SYNTAX, other]))
    assert result['attempts'][0]['loop_guard']['action'] == 'redirected'
    assert result['status'] == 'passed' and len(result['attempts']) == 1


# ── 影子模式 ─────────────────────────────────────────────────

def test_shadow_mode_records_but_does_not_change_outcome(tmp_path):
    script = {'example/mid': [tool(), tool(), tool(), '3'], 'example/high': ['2']}
    shadow_api = API(copy.deepcopy(script), stuck())
    shadow = run(make_core(tmp_path / 's', shadow_api, config('shadow')))
    off_api = API(copy.deepcopy(script), stuck())
    off = run(make_core(tmp_path / 'o', off_api, config('shadow', judge_calls_per_attempt=0)))
    worker = lambda api: [p['messages'] for p in api.requests if p['model'] not in (JUDGE_MODEL,)]
    assert worker(shadow_api) == worker(off_api)
    strip = lambda r: [(a['setting'], a['stop'], a['final'], a['public']) for a in r['attempts']]
    assert strip(shadow) == strip(off) and shadow['status'] == off['status'] == 'passed'
    guard = shadow['attempts'][0]['loop_guard']
    assert guard['mode'] == 'shadow' and guard['action'] == 'shadow_recorded'
    assert guard['would_redirect'] is True and guard['ended_attempt'] is False
    assert 'loop_guard' not in off['attempts'][0]


def test_default_loop_guard_mode_is_shadow():
    assert AdvisorConfig(advisor=role_profile('advisor'), judge=role_profile('judge'),
                         score_snapshot='s').loop_guard.mode == 'shadow'


# ── 裁判輸入：公開、≤ 8 KB ──────────────────────────────────

def test_judge_digest_is_public_bounded_and_has_recent_two_calls(tmp_path):
    huge = {'ok': False, 'error': {'code': 'syntax_error', 'category': 'content', 'message': 'x' * 20000}}
    api = API({'example/mid': [tool(arguments={'sql': 'A'}), tool(arguments={'sql': 'B' * 9000}),
                               tool(arguments={'sql': 'C'}), '2']}, stuck())
    run(make_core(tmp_path, api), env_factory([{'ok': True, 'rows': []}, huge, huge]))
    (request,) = api.by_model(JUDGE_MODEL)
    system, user = request['messages']
    assert 'loop-guard judge' in system['content']
    assert len(user['content'].encode()) <= 8192
    digest = json.loads(user['content'])
    assert digest['task'] == TASK['prompt']
    observed = digest['observations']
    assert [c['name'] for c in observed['tool_results']] == ['query', 'query']
    assert observed['public_diagnostics']['errors'][0]['count'] == 2
    assert 'tools' not in request


def test_digest_rejects_private_fields():
    from cloudyfan_jev.loop_guard import build_digest
    with pytest.raises(ValueError, match='Private'):
        build_digest('p', {'kind': 'tool_error'}, [],
                     [{'name': 'q', 'arguments': {'expected_answer': '2'}, 'result': {}}])


# ── 紀錄、經驗、凍結、續跑 ─────────────────────────────────

def test_findings_are_structured_for_experience_store(tmp_path):
    from cloudyfan_jev.loop_guard import experience_findings
    api = API({'example/mid': [tool(), tool(), tool(), '2'], 'example/high': ['2']}, stuck())
    result = run(make_core(tmp_path, api))
    (finding,) = experience_findings(result)
    assert finding['kind'] == 'loop_guard' and finding['model'] == 'example/mid'
    assert finding['setting'] == 'mid' and finding['action'] == 'redirected_still_stuck'
    assert finding['trigger']['fingerprint']['code'] == 'syntax_error'
    assert finding['errors'] == [{'kind': 'tool_error', 'count': 3, 'fingerprint': {
        'tool': 'query', 'code': 'syntax_error', 'category': 'content'}}]
    assert finding['stuck'] is True and finding['confidence'] == 0.9
    assert TASK['prompt'] not in json.dumps(finding)
    saved = json.loads((tmp_path / 'runs' / 'r' / 'loop-guard-a1.json').read_text())
    assert saved['action'] == 'redirected_still_stuck'


def test_loop_guard_settings_are_frozen_and_change_rejects_resume(tmp_path):
    api = API({'example/mid': ['2']})
    engine = make_core(tmp_path, api, config('shadow'))
    frozen = engine.configuration_descriptor()['advisor_first']['loop_guard']
    assert frozen['mode'] == 'shadow' and frozen['repeat_threshold'] == 2
    assert frozen['confidence_threshold'] == 0.7 and frozen['digest_limit_bytes'] == 8192
    run(engine)
    with pytest.raises(ValueError):
        run(make_core(tmp_path, API({'example/mid': ['2']}), config('active')))


def test_resume_rereads_judge_receipt_without_paying_again(tmp_path):
    script = {'example/mid': [tool(), tool(), tool(), '2'], 'example/high': ['2']}
    api = API(copy.deepcopy(script), stuck())
    first = run(make_core(tmp_path, api))
    (tmp_path / 'runs' / 'r' / 'result.json').unlink()
    again_api = API(copy.deepcopy(script), stuck())
    again = run(make_core(tmp_path, again_api))
    assert again_api.requests == []
    assert again['attempts'][0]['loop_guard'] == first['attempts'][0]['loop_guard']
    assert again['costs'] == first['costs']


@pytest.mark.parametrize('kwargs', [
    {'mode': 'loud'}, {'repeat_threshold': 1}, {'confidence_threshold': 0},
    {'confidence_threshold': 1.2}, {'digest_limit_bytes': 9000},
])
def test_invalid_loop_guard_config_rejected(kwargs):
    with pytest.raises(ValueError):
        LoopGuardConfig(**kwargs)


def test_detector_accepts_previous_validation_checks_for_legacy_plus_reuse():
    """#79 路徑一加強版可帶入上次驗收檢查碼；本單只確認偵測器支援。"""
    from cloudyfan_jev.loop_guard import Detector
    detector = Detector(seed_checks=['B', 'A'])
    hit = detector.observe('validate', {'passed': False, 'checks': ['A', 'B']})
    assert hit == {'kind': 'validation', 'fingerprint': {'checks': ['A', 'B']}, 'count': 1}
    assert Detector().observe('validate', {'passed': False, 'checks': ['A']}) is None


def test_second_recommendation_does_not_count_earlier_attempt_tool_errors(tmp_path):
    # 第一次作答與第二次作答各只撞一次同樣的工具錯誤；偵測以單次作答為範圍，不跨作答累計。
    api = API({'example/mid': [tool(), 'x'], 'example/high': [tool(), '2']}, stuck())
    result = run(make_core(tmp_path, api))
    first, second = result['attempts']
    assert second['setting'] == 'high' and result['status'] == 'passed'
    assert first['loop_guard']['triggered'] is False
    assert second['loop_guard']['triggered'] is False
    assert not api.by_model(JUDGE_MODEL)
