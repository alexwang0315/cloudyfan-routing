"""開放題評估（決策點題目）：題目解析、防洩漏、作答／評審指令與盲評表。全部離線（假 Anthropic 端點）。"""
from decimal import Decimal
import json

import httpx
import pytest

from cloudyfan_jev import open_reasoning as orq
from cloudyfan_jev import pilot

BACKGROUND, FORMAT, ITEMS = orq.load()


def test_frozen_items_parse_into_ten_decision_points():
    assert [i['id'] for i in ITEMS] == [f'D{n}' for n in range(1, 11)]
    assert BACKGROUND.startswith('我們在開發一個模型路由系統 MRC') and FORMAT.count('\n') == 3
    for item in ITEMS:
        assert all(item[k] for k in ('title', 'context', 'question', 'truth', 'rubric'))
        assert '拆解' in item['rubric'] and '建議' in item['rubric']


def test_frozen_items_file_is_checked():
    text = orq.FROZEN_ITEMS.read_text()
    assert orq.parse(text) == (BACKGROUND, FORMAT, ITEMS)
    with pytest.raises(ValueError):
        orq.parse('# empty\n')


@pytest.mark.parametrize('item', ITEMS, ids=lambda i: i['id'])
def test_answer_prompt_keeps_hindsight_and_rubric_out(item):
    prompt = orq.answer_prompt(BACKGROUND, FORMAT, item)
    assert item['context'] in prompt and item['question'] in prompt and FORMAT in prompt
    assert item['truth'] not in prompt and item['rubric'] not in prompt and item['title'] not in prompt
    for word in ('事後', '評分重點', '加分', '扣分', '繞遠路', '太簡單'):
        assert word not in prompt, word


def test_judge_prompt_has_truth_rubric_scale_and_no_writer_identity():
    prompt = orq.judge_prompt(BACKGROUND, ITEMS[0], 'my answer')
    assert ITEMS[0]['truth'] in prompt and ITEMS[0]['rubric'] in prompt and orq.SCALE in prompt
    assert '<answer>\nmy answer\n</answer>' in prompt
    assert 'claude' not in prompt.lower() and 'haiku' not in prompt.lower()


def test_parse_judgment_accepts_only_integer_scores_in_range():
    good = '好的\n{"decomposition": 2, "evidence": 1, "recommendation": 2, "calibration": 0, "reasons": {"evidence": "x"}}'
    assert orq.parse_judgment(good) == {'decomposition': 2, 'evidence': 1, 'recommendation': 2, 'calibration': 0,
                                        'total': 5, 'reasons': {'decomposition': '', 'evidence': 'x',
                                                                'recommendation': '', 'calibration': ''}}
    for bad in ('no json', '{"decomposition": 3, "evidence": 1, "recommendation": 2, "calibration": 0}',
                '{"decomposition": 1.5, "evidence": 1, "recommendation": 2, "calibration": 0}',
                '{"decomposition": true, "evidence": 1, "recommendation": 2, "calibration": 0}', None):
        assert orq.parse_judgment(bad) is None


def _fake(texts, seen):
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={'id': f'msg_{len(seen)}', 'type': 'message', 'role': 'assistant',
            'model': body['model'], 'content': [{'type': 'text', 'text': texts(body)}], 'stop_reason': 'end_turn',
            'usage': {'input_tokens': 900, 'output_tokens': 2500}})
    return {'anthropic': httpx.MockTransport(handler)}


def _user_text(body):
    content = body['messages'][-1]['content']
    return content if isinstance(content, str) else ''.join(part.get('text', '') for part in content)


def test_open_answer_and_judge_end_to_end(tmp_path):
    seen = []
    rows = pilot.open_answer(tmp_path / 'a-haiku', ['D1', 'D7'], pilot.anthropic_target('claude-haiku-5-5', effort='low'),
                             anthropic_key='an-key', cap_usd='1',
                             transports=_fake(lambda b: 'ANSWER ' + _user_text(b)[-40:], seen))
    assert [r['id'] for r in rows] == ['D1', 'D7'] and all(r['status'] == 'passed' for r in rows)
    assert all(r['answer'].startswith('ANSWER') for r in rows)
    assert seen[0]['model'] == 'claude-haiku-5-5' and seen[0]['output_config'] == {'effort': 'low'}
    assert orq.ANSWER_SYSTEM in json.dumps(seen[0].get('system'), ensure_ascii=False)
    assert ITEMS[0]['context'] in _user_text(seen[0]) and ITEMS[0]['truth'] not in _user_text(seen[0])
    report = json.loads((tmp_path / 'a-haiku' / 'open-answer-report.json').read_text())
    assert report['items_sha256'] == orq.FROZEN_ITEMS_SHA256 and Decimal(report['summary']['known_usd']) > 0
    pilot.open_answer(tmp_path / 'a-opus', ['D1'], pilot.anthropic_target('claude-opus-5-5', effort='xhigh'),
                      anthropic_key='an-key', cap_usd='1', transports=_fake(lambda b: 'OPUS ANSWER', []))

    judged = []
    scores = '{"decomposition": 2, "evidence": 2, "recommendation": 1, "calibration": 2}'
    jrows = pilot.open_judge(tmp_path / 'j', {'haiku': tmp_path / 'a-haiku', 'opus': tmp_path / 'a-opus'},
                             pilot.anthropic_target('claude-haiku-5-5', effort='xhigh'), anthropic_key='an-key',
                             cap_usd='1', max_input_bytes=49152,
                             transports=_fake(lambda b: scores if 'OPUS' not in _user_text(b) else 'oops', judged))
    assert sorted((r['id'], r['writer'], r['outcome']) for r in jrows) == [
        ('D1', 'haiku', 'judged'), ('D1', 'opus', 'judge_invalid'), ('D7', 'haiku', 'judged')]
    assert all(r['total'] == 7 for r in jrows if r['outcome'] == 'judged')
    assert all(ITEMS[0]['truth'] in _user_text(b) or ITEMS[6]['truth'] in _user_text(b) for b in judged)
    # 評審收到的就是 judge_prompt 本身，沒有多帶作答者身分（題目情境本來就會提到模型名稱）
    expected = {orq.judge_prompt(BACKGROUND, ITEMS[0], 'OPUS ANSWER')} | {
        orq.judge_prompt(BACKGROUND, i, r['answer']) for i in (ITEMS[0], ITEMS[6]) for r in rows if r['id'] == i['id']}
    assert {_user_text(b) for b in judged} == expected


def test_blind_sheet_shuffles_writers_and_keeps_the_key_apart():
    answers = {w: {i['id']: f'answer by {w} for {i["id"]}' for i in ITEMS} for w in ('haiku', 'sonnet', 'opus', 'fable')}
    sheet, key = orq.blind_sheet(BACKGROUND, ITEMS, answers)
    assert set(key) == {i['id'] for i in ITEMS} and all(sorted(v) == ['A', 'B', 'C', 'D'] for v in key.values())
    assert len({tuple(v.values()) for v in key.values()}) > 1   # 每題順序不同
    for writer in answers:
        assert f'by {writer} for' in sheet
    assert 'haiku' not in sheet.replace('answer by haiku', '') and ITEMS[0]['truth'] not in sheet
    assert orq.blind_sheet(BACKGROUND, ITEMS, answers) == (sheet, key)


def test_open_commands_require_confirm_paid(tmp_path):
    for command in ('open-answer', 'open-judge'):
        with pytest.raises(SystemExit):
            pilot.main([command, '--root', str(tmp_path / 'r'), '--anthropic-model', 'claude-haiku-5-5',
                        '--effort', 'low'])
