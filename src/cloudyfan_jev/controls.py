"""使用者控制（ADR 0038 第 7 點）：對話 skill 與 mod 共用的控制定義、指令解析與每題回報。

兩種前端最後送出的是同一組 ``overrides``（見 ``triage.normalize_overrides``）；MRC 照樣逐項
檢查，這裡能組出來的覆寫不代表能繞過池內與預算規則。本模組不呼叫任何網路或模型。

- 單題標記：訊息開頭的 ``#高風險``、``#路徑二``、``#模型 sonnet-xhigh``、``#不升級``、``#上限 0.5``。
- 整段對話設定：單獨一行的 ``/風險 高``、``/路徑 自動``、``/模型 自動``、``/升級 關``、``/上限 無``。
- 單題標記優先於對話設定；「自動」「無」表示交回 MRC 的預設判斷。
"""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re
import sys

from .triage import OVERRIDE_FIELDS, _MONEY

VERSION = 'mrc-user-controls-v1'
STATE_VERSION = 1
ROUTE_LABELS = {'path1': '可驗收題', 'path2': '開放題'}
RISK_LABELS = {'normal': '一般風險', 'high': '高風險'}
SOURCE_NOTES = {
    'override': '你指定了路徑',
    'triage_fallback': '分流顧問兩次都沒回分類，照規則走開放題',
    'no_verifier': '這題建不出驗收器，改走開放題',
}
FIELD_LABELS = {'risk': '風險', 'route': '路徑', 'setting': '模型', 'escalation': '升級', 'max_usd': '上限'}

# 單題標記與對話指令的字詞 → 覆寫值；None 表示清除（交回預設）。
DEFINITION = {
    'version': VERSION,
    'fields': {
        'risk': {'label': '風險', 'command': '/風險', 'values': {'normal': '一般', 'high': '高'},
                 'words': {'一般': 'normal', '高': 'high'},
                 'tags': {'#高風險': 'high', '#一般風險': 'normal'}},
        'route': {'label': '路徑', 'command': '/路徑', 'values': {'path1': '路徑一（可驗收）', 'path2': '路徑二（開放題）'},
                  'words': {'一': 'path1', '二': 'path2', '自動': None},
                  'tags': {'#路徑一': 'path1', '#路徑二': 'path2'}},
        'setting': {'label': '模型', 'command': '/模型', 'values': {},
                    'words': {'自動': None}, 'tags': {}, 'tag_with_argument': '#模型'},
        'escalation': {'label': '升級', 'command': '/升級', 'values': {'auto': '自動升級', 'off': '不升級'},
                       'words': {'開': None, '關': 'off'}, 'tags': {'#不升級': 'off'}},
        'max_usd': {'label': '上限', 'command': '/上限', 'values': {},
                    'words': {'無': None}, 'tags': {}, 'tag_with_argument': '#上限'},
    },
    # 主動提醒的判斷清單（第 7 點）：命中就先問使用者，由使用者決定，不替使用者決定。
    'risk_checklist': [
        {'id': 'irreversible', 'label': '難以回頭',
         'keywords': ['刪除', '刪掉', '不可逆', '無法復原', '覆寫', '清空', '下架', 'drop table', 'rm -rf', 'force push']},
        {'id': 'costly', 'label': '花費大',
         'keywords': ['採購', '下單', '付款', '預算', '合約金額', '報價', '投資']},
        {'id': 'public', 'label': '對外發布',
         'keywords': ['發布', '公告', '新聞稿', '上線', '部署', '推送', '寄給', '對外']},
        {'id': 'customer', 'label': '牽涉客戶',
         'keywords': ['客戶', '住戶', '家屬', '使用者資料', '個資']},
        {'id': 'legal_safety', 'label': '法規或安全',
         'keywords': ['法規', '合規', '契約', '授權書', '醫療', '用藥', '跌倒', '安全', '資安', '漏洞']},
    ],
    # 面板上的模型按鈕；實際可用的設定以契約的池為準，池外的 MRC 會回 setting_not_in_pool。
    'suggested_settings': ['sonnet-xhigh', 'fable-high', 'haiku-low'],
    'max_usd_presets': ['0.10', '0.50', '1.00'],
    'high_risk_note': '改用高風險時，開放題改由 Fable 5.2 high 作答，每題花費約為一般（Sonnet 5.5 xhigh）的 2.5 倍'
                      '（2026-11-26 盲測的牌價計算值，未和帳單核對）。',
}


class ControlError(ValueError):
    pass


def empty_state():
    return {'version': STATE_VERSION, 'session': {}, 'spent_usd': '0', 'tasks': 0}


def _value(field, word):
    spec = DEFINITION['fields'][field]
    if field == 'max_usd':
        if word in spec['words']:
            return spec['words'][word]
        if not _MONEY.fullmatch(word) or Decimal(word) <= 0:
            raise ControlError('上限要寫成正的金額，例如 0.5（美元）')
        return word
    if field == 'setting':
        if word in spec['words']:
            return spec['words'][word]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', word):
            raise ControlError('模型要寫池內設定的識別，例如 sonnet-xhigh')
        return word
    if word not in spec['words']:
        raise ControlError(f'{spec["command"]} 只接受：' + '、'.join(spec['words']))
    return spec['words'][word]


def _commands():
    return {spec['command']: field for field, spec in DEFINITION['fields'].items()}


def _tag(token, rest):
    """單題標記；回傳 (field, value, 用掉的後續字數) 或 None。"""
    for field, spec in DEFINITION['fields'].items():
        if token in spec['tags']:
            return field, spec['tags'][token], 0
        if token == spec.get('tag_with_argument'):
            if not rest:
                raise ControlError(token + ' 後面要接值')
            return field, _value(field, rest[0]), 1
    return None


def parse(text, state=None):
    """解析一則訊息：回傳要送的題目、這題的覆寫、更新後的對話設定與說明。"""
    state = dict(state or empty_state())
    session = dict(state.get('session') or {})
    commands, lines, applied = _commands(), [], []
    for line in text.splitlines():
        words = line.strip().split()
        if words and words[0] in commands:
            field = commands[words[0]]
            if len(words) != 2:
                raise ControlError(words[0] + ' 後面要接一個值')
            value = _value(field, words[1])
            if value is None:
                session.pop(field, None)
            else:
                session[field] = value
            applied.append({'field': field, 'value': value, 'scope': 'session'})
        else:
            lines.append(line)
    body = '\n'.join(lines).strip()
    task = {}
    tokens = body.split(' ') if body else []
    index = 0
    while index < len(tokens):
        if not tokens[index]:
            index += 1
            continue
        found = _tag(tokens[index], tokens[index + 1:])
        if found is None:
            break
        field, value, extra = found
        task[field] = value
        applied.append({'field': field, 'value': value, 'scope': 'task'})
        index += 1 + extra
    prompt = ' '.join(tokens[index:]).strip()
    merged = {k: v for k, v in {**session, **task}.items() if v is not None}
    # 指定模型就不會升級；MRC 不收「指定模型＋自動升級」，這裡也不送 escalation。
    overrides = {k: merged[k] for k in OVERRIDE_FIELDS if k in merged}
    signals = risk_signals(prompt) if prompt else []
    state['session'] = session
    return {'prompt': prompt, 'overrides': overrides or None, 'state': state, 'applied': applied,
            'risk_signals': signals,
            'needs_confirmation': bool(signals) and 'risk' not in merged,
            'notice': describe(session) if any(a['scope'] == 'session' for a in applied) else None}


def risk_signals(text):
    lowered = text.lower()
    return [{'id': item['id'], 'label': item['label'],
             'matched': [k for k in item['keywords'] if k.lower() in lowered]}
            for item in DEFINITION['risk_checklist'] if any(k.lower() in lowered for k in item['keywords'])]


def describe(session):
    """目前的整段對話設定，一行中文。"""
    route = session.get('route')
    parts = [RISK_LABELS[session.get('risk', 'normal')],
             '路徑 ' + (ROUTE_LABELS[route] if route else '自動分流'),
             '模型 ' + session.get('setting', '自動'),
             '升級 ' + ('關' if session.get('escalation') == 'off' else '自動'),
             '上限 ' + ('US$' + session['max_usd'] if 'max_usd' in session else '無')]
    return '目前設定：' + '／'.join(parts)


def report_line(outcome):
    """每題一行：路徑、模型、風險、花費；推翻分流或轉送時附上顧問原判斷。"""
    routing = outcome.get('routing')
    if not routing:
        raise ControlError('這題不是顧問先分流模式，沒有路徑紀錄')
    model = routing['model'] or '未作答'
    if routing.get('effort') and routing['model']:
        model += ' ' + routing['effort']
    cost = (outcome.get('costs') or {}).get('complete_api_usd')
    spent = '費用待核' if cost is None else '約 US$' + _rounded(cost)
    line = '本題：' + '／'.join([ROUTE_LABELS[routing['route']], model, RISK_LABELS[routing['risk']], spent])
    notes = []
    if routing['route_source'] in SOURCE_NOTES:
        notes.append(SOURCE_NOTES[routing['route_source']])
    if routing['overridden']:
        notes.append('你改了' + '、'.join(FIELD_LABELS[f] for f in routing['overridden']))
    if routing['triage_route'] and routing['triage_route'] != routing['route']:
        notes.append('分流顧問原判：' + ROUTE_LABELS[routing['triage_route']])
    return line + ('（' + '；'.join(notes) + '）' if notes else '')


def _rounded(cost):
    value = Decimal(cost)
    return format(value.quantize(Decimal('0.01')) if value >= Decimal('0.01') else value.normalize(), 'f')


def record(state, outcome):
    """把一題的花費加進對話累計（狀態列顯示用）。"""
    state = dict(state)
    cost = (outcome.get('costs') or {}).get('complete_api_usd')
    state['tasks'] = state.get('tasks', 0) + 1
    if cost is not None:
        state['spent_usd'] = str(Decimal(state.get('spent_usd', '0')) + Decimal(cost))
    return state


def mod_definition_source():
    """mod 用的定義檔（TypeScript）；由這裡產生，測試核對 mod 內的副本沒有漂移。"""
    return ('// 由 `python -m cloudyfan_jev.controls definition --typescript` 產生；不要手改。\n'
            'export const DEFINITION = ' + json.dumps(DEFINITION, ensure_ascii=False, indent=2, sort_keys=True)
            + ' as const\n')


def _load(path):
    if path is None or not Path(path).exists():
        return empty_state()
    state = json.loads(Path(path).read_text())
    if state.get('version') != STATE_VERSION:
        raise ControlError('對話設定檔版本不符')
    return state


def _save(path, state):
    if path is not None:
        Path(path).write_text(json.dumps(state, ensure_ascii=False, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m cloudyfan_jev.controls',
                                     description='MRC 使用者控制：解析指令、組出 overrides、產生每題回報（不連網）')
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='解析一則訊息（從標準輸入讀），輸出要送的題目與 overrides')
    prep.add_argument('--state', help='整段對話設定檔（JSON）；省略就不保存')
    prep.add_argument('--risk', choices=('normal', 'high'), help='使用者回答高風險提醒後，這題的風險')
    rep = sub.add_parser('report', help='讀一題的結果（標準輸入的 JSON），輸出一行回報')
    rep.add_argument('--state', help='同上；會累計這段對話的花費')
    sub.add_parser('show', help='顯示目前的整段對話設定').add_argument('--state')
    definition = sub.add_parser('definition', help='輸出共用的控制定義')
    definition.add_argument('--typescript', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'definition':
            sys.stdout.write(mod_definition_source() if args.typescript
                             else json.dumps(DEFINITION, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
            return 0
        state = _load(args.state)
        if args.command == 'show':
            print(describe(state['session']))
            return 0
        if args.command == 'report':
            outcome = json.loads(sys.stdin.read())
            print(report_line(outcome))
            if args.state is not None:
                state = record(state, outcome)
                _save(args.state, state)
                print(f'累計：約 US${_rounded(state["spent_usd"])}（{state["tasks"]} 題）')
            return 0
        result = parse(sys.stdin.read(), state)
        if args.risk is not None:
            overrides = dict(result['overrides'] or {})
            overrides['risk'] = args.risk
            result.update(overrides=overrides, needs_confirmation=False)
        _save(args.state, result['state'])
        print(json.dumps({k: v for k, v in result.items() if k != 'state'}, ensure_ascii=False, sort_keys=True))
        return 0
    except ControlError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
