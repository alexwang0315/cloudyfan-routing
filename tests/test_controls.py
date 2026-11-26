"""使用者控制（ADR 0038 第 7 點）：skill 與 mod 共用的指令解析、高風險提醒與每題回報。"""
import json
from pathlib import Path

import pytest

from cloudyfan_jev import controls
from cloudyfan_jev.controls import ControlError, parse, report_line
from cloudyfan_jev.triage import normalize_overrides
from test_advisor_triage import triage_config

MOD = Path(__file__).resolve().parents[1] / 'clients' / 'mrc-controls-mod'


def test_plain_message_sends_no_overrides():
    result = parse('請把這段 SQL 改成用 JOIN')
    assert result['prompt'] == '請把這段 SQL 改成用 JOIN'
    assert result['overrides'] is None and result['needs_confirmation'] is False


def test_task_tags_apply_to_this_task_only():
    state = controls.empty_state()
    result = parse('#高風險 #路徑二 #上限 0.5 評估要不要換資料庫', state)
    assert result['prompt'] == '評估要不要換資料庫'
    assert result['overrides'] == {'risk': 'high', 'route': 'path2', 'max_usd': '0.5'}
    assert parse('下一題', result['state'])['overrides'] is None


def test_session_commands_persist_until_reset_and_tags_win():
    state = parse('/風險 高\n/升級 關', controls.empty_state())
    assert state['prompt'] == '' and state['notice'].startswith('目前設定：高風險')
    following = parse('#一般風險 #模型 sonnet-xhigh 寫一段摘要', state['state'])
    assert following['overrides'] == {'risk': 'normal', 'setting': 'sonnet-xhigh', 'escalation': 'off'}
    cleared = parse('/風險 一般\n/升級 開\n第三題', following['state'])
    assert cleared['overrides'] == {'risk': 'normal'} and cleared['prompt'] == '第三題'
    assert parse('/路徑 二', cleared['state'])['state']['session'] == {'risk': 'normal', 'route': 'path2'}
    assert parse('/路徑 自動', {**cleared['state'], 'session': {'route': 'path2'}})['state']['session'] == {}


@pytest.mark.parametrize('text', ['/風險 超高', '/上限 -1', '/上限', '#上限 abc 題目', '#模型', '/模型 一 二'])
def test_bad_commands_are_rejected_with_a_reason(text):
    with pytest.raises(ControlError):
        parse(text)


def test_risk_checklist_asks_the_user_only_when_risk_is_not_set():
    result = parse('幫我寫好新聞稿，明天對外發布並寄給客戶')
    assert result['needs_confirmation'] is True
    assert {s['id'] for s in result['risk_signals']} == {'public', 'customer'}
    assert parse('#一般風險 幫我寫好新聞稿，明天對外發布')['needs_confirmation'] is False
    assert parse('#高風險 幫我寫好新聞稿，明天對外發布')['needs_confirmation'] is False


def test_controls_always_produce_overrides_mrc_accepts():
    config = triage_config()
    messages = ['#高風險 #路徑一 #不升級 #上限 0.25 題目', '#模型 haiku-low #不升級 題目', '/升級 關\n#路徑二 題目']
    state = controls.empty_state()
    for text in messages:
        result = parse(text, state)
        state = result['state']
        assert normalize_overrides(result['overrides'], config) == result['overrides']


def outcome(**routing):
    base = {'version': 'advisor-triage-v1', 'route': 'path2', 'route_source': 'triage', 'triage_route': 'path2',
            'triage_reason': 'r', 'triage_retried': False, 'triage_fallback': False, 'risk': 'normal',
            'escalation': 'auto', 'setting': 'sonnet-xhigh', 'final_tier': 'triage:sonnet-xhigh',
            'model': 'anthropic:claude-sonnet-5-5', 'effort': 'xhigh', 'overridden': []}
    base.update(routing)
    return {'routing': base, 'costs': {'complete_api_usd': '0.1134'}}


def test_report_line_shows_route_model_risk_and_cost():
    assert report_line(outcome()) == '本題：開放題／anthropic:claude-sonnet-5-5 xhigh／一般風險／約 US$0.11'


def test_report_line_names_overrides_and_the_triage_original_judgment():
    line = report_line(outcome(route_source='override', triage_route='path1', overridden=['route'], risk='high'))
    assert line == ('本題：開放題／anthropic:claude-sonnet-5-5 xhigh／高風險／約 US$0.11'
                    '（你指定了路徑；你改了路徑；分流顧問原判：可驗收題）')
    assert '建不出驗收器' in report_line(outcome(route_source='no_verifier', triage_route='path1'))
    fallback = outcome()
    fallback['routing']['advisor_incomplete_dispatch'] = {'setting': 'sonnet-xhigh', 'reason': 'invalid_json'}
    assert report_line(fallback).endswith('（顧問未完成、改派 sonnet-xhigh）')
    cheap = outcome()
    cheap['costs']['complete_api_usd'] = '0.0008'
    assert report_line(cheap).endswith('約 US$0.0008')


def test_cli_round_trip_keeps_session_state_and_totals(tmp_path, capsys, monkeypatch):
    import io
    state = tmp_path / 'state.json'
    monkeypatch.setattr('sys.stdin', io.StringIO('/風險 高'))
    assert controls.main(['prepare', '--state', str(state)]) == 0
    monkeypatch.setattr('sys.stdin', io.StringIO('發布公告'))
    assert controls.main(['prepare', '--state', str(state)]) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed['overrides'] == {'risk': 'high'} and printed['needs_confirmation'] is False
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(outcome())))
    assert controls.main(['report', '--state', str(state)]) == 0
    assert capsys.readouterr().out.startswith('本題：開放題')
    assert json.loads(state.read_text())['spent_usd'] == '0.1134'
    monkeypatch.setattr('sys.stdin', io.StringIO('發布公告'))
    assert controls.main(['prepare', '--risk', 'normal']) == 0
    assert json.loads(capsys.readouterr().out)['overrides'] == {'risk': 'normal'}


def test_mod_definition_copy_matches_the_shared_definition():
    assert (MOD / 'hooks' / 'definition.ts').read_text() == controls.mod_definition_source()
