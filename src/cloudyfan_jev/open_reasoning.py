"""開放題評估：沒有標準答案的決策點題目，評「推理能力」與「建議能力」。設計見
plans/mrc-blind-validation-experiment-2026-11-24.md §11。

題目凍結在 ``docs/evidence/open-reasoning-items-20261125.md``：每題的「給模型看的情境」與「問題」送給作答者；
「事後真相」與「評分重點」只給評審。共同背景與作答格式也從同一份檔案讀出，只有一個來源。
"""
import hashlib
import json
from pathlib import Path
import random
import re

FROZEN_ITEMS = Path(__file__).resolve().parents[2] / 'docs/evidence/open-reasoning-items-20261125.md'
FROZEN_ITEMS_SHA256 = '3b4fd3f49cdc00c9ee064311697b421bdab24f792dfc2cde0c030cf2d0744c2e'
DIMENSIONS = ('decomposition', 'evidence', 'recommendation', 'calibration')

ANSWER_SYSTEM = ('你是協助設計與判讀實驗的研究夥伴。請用繁體中文回答，依使用者指定的四段格式。'
                 '只根據題目給的資訊推理；題目沒說的結果不要當成已知，需要的話寫成假設或要查證的事。')

# 評分表（實驗計畫 §11.2）。評審拿這份表、事後真相與評分重點來評。
SCALE = '''- 拆解（decomposition）：2＝點出後來證明關鍵的子問題；1＝有拆，但漏了關鍵的那一塊；0＝沒拆，或拆錯方向。
- 證據（evidence）：2＝提出能分辨不同假設、成本合理的檢查，並把不知道的事標成不知道；1＝提了檢查，但分辨不出關鍵差異；0＝沒提證據，或拿推測當事實。
- 建議（recommendation）：2＝下一步和事後證明有效的做法一樣好或更好，並顧到成本與風險；1＝方向大致對，但繞遠路或忽略成本；0＝方向錯，或事後證明無效。
- 校準（calibration）：2＝結論的把握程度和證據相稱，有說樣本或前提的限制；1＝有些過度肯定；0＝沒證據卻下定論。'''

JUDGE_SYSTEM = ('你是嚴格、公正的評審。你要評一份針對實驗決策問題的回答。你會拿到當時的情境、問題、事後實際發生的事，'
                '以及這題的評分重點。依評分表給四個面向各 0～2 分。歷史做法不是唯一正解：回答比事後實際的做法更好，'
                '也要給高分。不要因為回答寫得長或流暢就加分。只回一個 JSON 物件，不要其他文字。')


def items_text(path=FROZEN_ITEMS):
    text = Path(path).read_text()
    if Path(path) == FROZEN_ITEMS and hashlib.sha256(text.encode()).hexdigest() != FROZEN_ITEMS_SHA256:
        raise ValueError('Frozen open-reasoning items changed')
    return text


def _between(block, start, end):
    match = re.search(re.escape(start) + r'\s*(.*?)\s*' + (re.escape(end) if end else r'\Z'), block, re.S)
    if match is None:
        raise ValueError('Missing section ' + start)
    return match.group(1).strip()


def parse(text):
    """回傳 (共同背景, 作答格式, 題目清單)。題目欄位：id、title、context、question、truth、rubric。"""
    head, *sections = re.split(r'\n## (?=D\d+：)', text)
    if '作答格式' not in head or not sections:
        raise ValueError('Open-reasoning items file is malformed')
    background = '\n'.join(re.sub(r'^\s*> ?', '', line) for line in head.splitlines()
                           if line.lstrip().startswith('>')).strip()
    fmt_block = head.split('作答格式', 1)[1].split('\n- ', 1)[0]
    answer_format = '\n'.join(line.strip() for line in fmt_block.splitlines() if re.match(r'\s+\d\.', line))
    items = []
    for section in sections:
        section = section.split('\n---', 1)[0]
        first, _, body = section.partition('\n')
        number, title = first.split('：', 1)
        items.append({'id': number, 'title': title.strip(),
                      'context': _between(body, '**給模型看的情境：**', '**問題：**'),
                      'question': _between(body, '**問題：**', '**事後真相：**'),
                      'truth': _between(body, '**事後真相：**', '**評分重點：**'),
                      'rubric': _between(body, '**評分重點：**', None)})
    if not background or answer_format.count('\n') != 3 or not items:
        raise ValueError('Open-reasoning items file is malformed')
    return background, answer_format, items


def load(path=FROZEN_ITEMS):
    return parse(items_text(path))


def answer_prompt(background, answer_format, item):
    """作答者只看共同背景、這題的情境與問題、作答格式；不含事後真相、評分重點與標題。"""
    return '\n\n'.join(['背景：\n' + background, '目前的情況：\n' + item['context'], '問題：' + item['question'],
                        '請分四段回答：\n' + answer_format])


def judge_prompt(background, item, answer):
    return '\n\n'.join([
        '背景：\n' + background, '當時的情境（作答者看到的）：\n' + item['context'], '問題：' + item['question'],
        '事後實際發生的事（作答者看不到）：\n' + item['truth'], '這題的評分重點：\n' + item['rubric'],
        '評分表：\n' + SCALE, '要評的回答：\n<answer>\n' + answer.strip() + '\n</answer>',
        '請只回 JSON：{"decomposition": 0-2, "evidence": 0-2, "recommendation": 0-2, "calibration": 0-2, '
        '"reasons": {"decomposition": "一句話", "evidence": "一句話", "recommendation": "一句話", '
        '"calibration": "一句話"}}'])


def parse_judgment(text):
    """取回覆裡的 JSON 物件；四個分數都要是 0、1、2 的整數，否則回 None。"""
    if not isinstance(text, str):
        return None
    match = re.search(r'\{.*\}', text, re.S)
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    scores = {d: value.get(d) for d in DIMENSIONS}
    if not all(type(s) is int and 0 <= s <= 2 for s in scores.values()):
        return None
    reasons = value.get('reasons') if isinstance(value.get('reasons'), dict) else {}
    return {**scores, 'total': sum(scores.values()), 'reasons': {d: str(reasons.get(d, '')) for d in DIMENSIONS}}


def blind_sheet(background, items, answers, seed='open-reasoning-sheet-v1'):
    """使用者盲評表。``answers`` 是 {作答者: {題號: 回答}}；每題的作答者順序用固定種子打亂，
    標成 A、B、C…；回傳 (評分表 Markdown, 代號對照)。對照另外存檔，評完才看。"""
    lines = ['# 開放題盲評表', '', '每題的回答順序是隨機的，看不到是哪個模型寫的。每份回答依四個面向各給 0～2 分：',
             '', SCALE, '', '共同背景：', '', *['> ' + line if line else '>' for line in background.splitlines()], '']
    key = {}
    for item in items:
        writers = sorted(w for w in answers if item['id'] in answers[w])
        random.Random(f'{seed}:{item["id"]}').shuffle(writers)
        labels = {chr(ord('A') + i): w for i, w in enumerate(writers)}
        key[item['id']] = labels
        lines += ['---', '', f'## {item["id"]}：{item["title"]}', '', '**情境：**', '', item['context'], '',
                  '**問題：** ' + item['question'], '']
        for label, writer in labels.items():
            lines += [f'### 回答 {label}', '', answers[writer][item['id']].strip(), '']
        lines += ['**你的評分：**', '', '| 回答 | 拆解 | 證據 | 建議 | 校準 | 備註 |', '|---|---|---|---|---|---|',
                  *[f'| {label} |  |  |  |  |  |' for label in labels], '']
    return '\n'.join(lines), key
