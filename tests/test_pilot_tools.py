"""#81 工具題：check_answer 工具環境，以及路徑一加強版的迴圈守衛在多回合比較中提早交給 Jev。全部離線。"""
import json

import httpx

from cloudyfan_jev import hard_tasks, pilot
from test_pilot_compare import Anthropic, setup

TASKS = [hard_tasks.make('A', 1, 0), hard_tasks.make('E', 2, 0)]


def test_check_tool_reports_correctness_and_limits_uses_per_attempt():
    task = TASKS[0]
    env = hard_tasks.HardToolTaskEnvironment(task)
    assert [t['function']['name'] for t in env.tools_schema()] == ['check_answer']
    assert env.call('check_answer', {'answer': 'not a number'}) == {'passed': False, 'checks': ['answer_format_invalid']}
    assert env.call('check_answer', {'answer': '-1'}) == {'passed': False, 'checks': ['answer_incorrect']}
    assert env.call('check_answer', {'answer': str(task['answer'])}) == {'passed': True}
    assert env.call('check_answer', {'answer': str(task['answer'])})['error']['code'] == 'CHECK_LIMIT'
    assert env.validate('ANSWER: ' + str(task['answer'])) == {'passed': True, 'checks': []}
    assert env.call('check_answer', {'answer': '-1'}) == {'passed': False, 'checks': ['answer_incorrect']}


def test_check_tool_rejects_other_tools_and_extra_arguments():
    env = hard_tasks.HardToolTaskEnvironment(TASKS[0])
    assert env.call('python', {'answer': '1'})['error']['code'] == 'unknown_tool'
    assert env.call('check_answer', {'answer': '1', 'hint': 'x'})['error']['code'] == 'invalid_arguments'
    assert env.call('check_answer', {'answer': 1})['error']['code'] == 'invalid_arguments'
    assert env.checks_used == 0


def test_tool_prompt_keeps_the_original_task_first():
    prompt = hard_tasks.tool_prompt(TASKS[0])
    assert prompt.startswith(TASKS[0]['prompt']) and 'check_answer' in prompt


class StuckOpenRouter:
    """開源模型每回合都用同一個錯誤答案呼叫 check_answer；裁判判定卡住；Jev 選中池。"""

    def __init__(self):
        self.models = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.models.append(body['model'])
        n = len(self.models)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(body['questions']))
            criteria = body['questions'][stage]['criteria']
            return httpx.Response(200, json={'id': f'gen-jev-{n}', 'model': body['model'], 'provider': 'TypeSafe',
                'usage': {'cost': .0002}, 'answers': {stage: {'type': 'choice', 'choice': 'mid',
                'probabilities': {k: .8 if k == 'mid' else .2 for k in criteria}}}})
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'call-{n}', 'type': 'function',
                   'function': {'name': 'check_answer', 'arguments': json.dumps({'answer': '-1'})}}]}
        provider, finish = 'Crusoe', 'tool_calls'
        if body['model'] == 'openai/gpt-oss-20b':
            message = {'role': 'assistant', 'content': json.dumps(
                {'stuck': True, 'confidence': 0.9, 'redirect': 'Stop guessing; recompute from scratch.'})}
            provider, finish = 'Parasail', 'stop'
        return httpx.Response(200, json={'id': f'gen-or-{n}', 'model': body['model'], 'provider': provider,
            'usage': {'cost': .0003, 'prompt_tokens': 100, 'completion_tokens': 50},
            'choices': [{'message': message, 'finish_reason': finish}]})


def run_tools(tmp_path, arms, mode):
    orouter, anth = StuckOpenRouter(), Anthropic()
    rows = pilot.compare(tmp_path / 'tools', TASKS, openrouter_key='or-key', anthropic_key='an-key', cap_usd='50',
                         arms=arms, tools=True, max_turns=6, max_input_bytes=131072, loop_guard_mode=mode,
                         transports={'openrouter': httpx.MockTransport(orouter),
                                     'anthropic': httpx.MockTransport(anth)}, **setup())
    return rows, orouter


def test_active_loop_guard_hands_a_stuck_open_model_to_jev_early(tmp_path):
    rows, orouter = run_tools(tmp_path, ('cloudyfan', 'cloudyfan_plus'), 'active')
    by = {(r['task'], r['arm']): r for r in rows}
    for t in TASKS:
        plain, plus = by[(t['id'], 'cloudyfan')], by[(t['id'], 'cloudyfan_plus')]
        assert plain['final_pass'] is True and plus['final_pass'] is True
        assert plain['tiers'] == plus['tiers'] == ['open_source', 'mid']
        # 純路徑一：開源模型用完 3 次檢查後撞 CHECK_LIMIT，直到 6 回合上限。
        assert plain['stops'][0] == 'turn_limit' and plain['tool_calls'] == 6
        # 加強版：兩次相同檢查碼 → 裁判插提示 → 再撞一次 → 結束本次作答交給 Jev。
        assert plus['stops'][0] == 'loop_guard_stuck' and plus['tool_calls'] == 3
    open_calls = [m for m in orouter.models if m == 'openai/gpt-oss-120b']
    assert len(open_calls) == len(TASKS) * (6 + 3)
    assert orouter.models.count('openai/gpt-oss-20b') == len(TASKS)


def test_shadow_loop_guard_records_but_does_not_change_the_attempt(tmp_path):
    rows, orouter = run_tools(tmp_path, ('cloudyfan_plus',), 'shadow')
    assert all(r['stops'][0] == 'turn_limit' and r['tool_calls'] == 6 and r['final_pass'] is True for r in rows)
    assert orouter.models.count('openai/gpt-oss-20b') == len(TASKS)
