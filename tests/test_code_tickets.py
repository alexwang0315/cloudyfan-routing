"""第三版盲測程式題（Q～X，工單式）：公平性、錯誤解、獨立核對與凍結。全部離線，不發任何請求。

核對方式（全部寫在這個檔案，和 code_tickets 的參考解不同）：
- CHECKERS：每類用另一種寫法重算全部期望值（分鐘陣列、逐月走、Decimal、比較函式、csv 模組、逐年加……）。
- ALTERNATIVES：在工單沒定的點上做了別的選擇、但合理的寫法，必須過全部公開與隱藏測試
  （這是「合理工程師可能有不同做法的點一律不測」的直接證據）。
- WITHIN_TICKET：每個測試輸入都在工單或既有程式寫明的前提內（例如 on_date 不早於 birth_date、沒有 2/29 生日）。
錯誤解：在行程內確認「公開範例全對、隱藏測試至少錯 TICKET_COVERAGE 題，且每個失敗都能歸到它違反的要求」；
另在隔離環境抽查參考解全過、錯誤解過公開驗收卻沒過隱藏計分。
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, timedelta
from decimal import Decimal
from functools import cmp_to_key, lru_cache
import io
import json
import re

import pytest

from cloudyfan_jev import code_sandbox as sandbox
from cloudyfan_jev import code_tasks as ct
from cloudyfan_jev import code_tickets as tk

TRUSTED_SANDBOX = sandbox.sandbox_available()
needs_sandbox = pytest.mark.skipif(not TRUSTED_SANDBOX, reason='sandbox-exec is only available on macOS')
FAMILIES = tk.TICKET_FAMILIES
KEYS = [(f, level, 0) for f in FAMILIES for level in ct.LEVELS]
BASES = {'ticket', 'convention', 'existing_code', 'examples'}


def ids(key):
    return f'{key[0]}{key[1]}-{key[2]}'


@lru_cache(maxsize=None)
def built(family, level, seed):
    return tk.build_ticket(family, level, seed)


def args_of(test):
    return json.loads(json.dumps(test['args']))


def agrees(function, test):
    try:
        return sandbox.strict_equal(json.loads(json.dumps(function(*args_of(test)))), test['expected'])
    except Exception:
        return False


def compiled(source, entry):
    namespace = {}
    exec(compile(source, '<alternative>', 'exec'), namespace)
    return namespace[entry]


# ---- 獨立核對 ----

def _hhmm(total):
    return '%02d:%02d' % divmod(total, 60)


def q_check(busy, day_start, day_end, min_minutes):
    """逐分鐘標記忙碌，再找連續空閒的區段。"""
    def minutes(t):
        return int(t[:2]) * 60 + int(t[3:])
    start, end = minutes(day_start), minutes(day_end)
    free = [True] * (end - start)
    for s, e in busy:
        for x in range(max(minutes(s), start), min(minutes(e), end)):
            free[x - start] = False
    out, x = [], 0
    while x < len(free):
        if not free[x]:
            x += 1
            continue
        y = x
        while y < len(free) and free[y]:
            y += 1
        if y - x >= min_minutes:
            out.append([_hhmm(start + x), _hhmm(start + y)])
        x = y
    return out


def r_check(day, amount, unit):
    """一個月一個月走，月底用「下個月 1 日減一天」算。"""
    y, m, d = map(int, day.split('-'))
    if unit in ('days', 'weeks'):
        return (date(y, m, d) + timedelta(days=amount * (7 if unit == 'weeks' else 1))).isoformat()
    steps = amount * (12 if unit == 'years' else 1)
    for _ in range(abs(steps)):
        m += 1 if steps > 0 else -1
        if m == 13:
            y, m = y + 1, 1
        if m == 0:
            y, m = y - 1, 12
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d, last)).isoformat()


def s_check(text):
    t = text.strip()
    sign = -1 if t.startswith('-') else 1
    t = t.lstrip('-').lstrip('$').replace(',', '')
    return sign * int(Decimal(t) * 100)


def _t_cmp(a, b):
    """逐字比較，遇到兩邊都是數字就整段取數值比。"""
    a, b = a.lower(), b.lower()
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i].isdigit() and b[j].isdigit():
            i2, j2 = i, j
            while i2 < len(a) and a[i2].isdigit():
                i2 += 1
            while j2 < len(b) and b[j2].isdigit():
                j2 += 1
            x, y = int(a[i:i2]), int(b[j:j2])
            if x != y:
                return -1 if x < y else 1
            i, j = i2, j2
        elif a[i] != b[j]:
            return -1 if a[i] < b[j] else 1
        else:
            i, j = i + 1, j + 1
    return (len(a) - i) - (len(b) - j)


def t_check(items):
    pinned = [it['title'] for it in items if it.get('pinned')]
    others = [it['title'] for it in items if not it.get('pinned')]
    return sorted(pinned, key=cmp_to_key(_t_cmp)) + sorted(others, key=cmp_to_key(_t_cmp))


def _u_cmp(a, b):
    """照 SemVer 2.0 第 11 條逐條比較。"""
    core_a, _, pre_a = a.partition('-')
    core_b, _, pre_b = b.partition('-')
    na, nb = [int(x) for x in core_a.split('.')], [int(x) for x in core_b.split('.')]
    if na != nb:
        return -1 if na < nb else 1
    if not pre_a or not pre_b:
        return (not pre_a) - (not pre_b)
    xs, ys = pre_a.split('.'), pre_b.split('.')
    for x, y in zip(xs, ys):
        if x == y:
            continue
        if x.isdigit() and y.isdigit():
            return -1 if int(x) < int(y) else 1
        if x.isdigit() or y.isdigit():
            return -1 if x.isdigit() else 1
        return -1 if x < y else 1
    return len(xs) - len(ys)


def u_check(versions):
    return max(versions, key=cmp_to_key(_u_cmp)) if versions else None


def v_check(fields, delimiter=','):
    """Python 標準庫的 csv 模組（QUOTE_MINIMAL）。"""
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator='\r\n').writerow(fields)
    return buffer.getvalue()[:-2]


def w_check(birth_date, on_date):
    born, on = date.fromisoformat(birth_date), date.fromisoformat(on_date)
    age = 0
    while born.replace(year=born.year + age + 1) <= on:
        age += 1
    return age


def x_check(text, n, stopwords=()):
    """逐字元收集英文字母當字。"""
    words, current = [], ''
    for ch in text.lower() + ' ':
        if 'a' <= ch <= 'z':
            current += ch
        elif current:
            words.append(current)
            current = ''
    counts = Counter(w for w in words if w not in set(stopwords))
    return [[w, c] for w, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


CHECKERS = {'Q': q_check, 'R': r_check, 'S': s_check, 'T': t_check, 'U': u_check, 'V': v_check, 'W': w_check,
            'X': x_check}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_expected_values_match_an_independent_implementation(key):
    task, _ = built(*key)
    check = CHECKERS[key[0]]
    for test in task['public_tests'] + task['hidden_tests']:
        assert sandbox.strict_equal(json.loads(json.dumps(check(*args_of(test)))), test['expected']), \
            (task['id'], test['args'])


# ---- 合理但做法不同的寫法：工單沒定的點不可被測到 ----

ALTERNATIVES = {
    # 先合併區間再找空檔，回傳 tuple（JSON 化後一樣是清單）。
    'Q': ['''
def free_slots(busy, day_start, day_end, min_minutes):
    to = lambda t: int(t[:2]) * 60 + int(t[3:])
    lo, hi = to(day_start), to(day_end)
    merged = []
    for s, e in sorted((max(to(s), lo), min(to(e), hi)) for s, e in busy):
        if s >= e:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    edges = [lo] + [x for block in merged for x in block] + [hi]
    fmt = lambda x: f"{x // 60:02d}:{x % 60:02d}"
    return [(fmt(a), fmt(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= min_minutes]
'''],
    # 目標日期不存在就一天一天往回退；年用 12 個月處理。
    'R': ['''
from datetime import date, timedelta


def add_period(day, amount, unit):
    start = date.fromisoformat(day)
    if unit == "days":
        return (start + timedelta(days=amount)).isoformat()
    if unit == "weeks":
        return (start + timedelta(weeks=amount)).isoformat()
    months = amount * 12 if unit == "years" else amount
    year, month0 = divmod(start.year * 12 + start.month - 1 + months, 12)
    d = start.day
    while True:
        try:
            return date(year, month0 + 1, d).isoformat()
        except ValueError:
            d -= 1
'''],
    # 正規表示式加 Decimal；也接受工單沒提到的寫法（"+5"、".5"、"$ 5"），不影響測試。
    'S': ['''
import re
from decimal import Decimal


def parse_amount(text):
    m = re.fullmatch(r"\\s*([+-]?)\\s*\\$?\\s*([\\d,]*\\.?\\d*)\\s*", text)
    value = Decimal(m.group(2).replace(",", "") or "0")
    cents = int((value * 100).to_integral_value())
    return -cents if m.group(1) == "-" else cents
'''],
    # casefold、re.findall 切段、混型別清單當鍵。
    'T': ['''
import re


def sidebar_order(items):
    def key(item):
        return [int(p) if p.isdigit() else p for p in re.findall(r"\\d+|\\D+", item["title"].casefold())]

    pinned = sorted((i for i in items if i.get("pinned")), key=key)
    rest = sorted((i for i in items if not i.get("pinned")), key=key)
    return [i["title"] for i in pinned + rest]
'''],
    # 完整的 SemVer 排序鍵（含工單沒提到的英數識別碼與 build metadata）。
    'U': ['''
def latest_release(versions):
    def key(v):
        core, _, pre = v.split("+")[0].partition("-")
        ids = [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split(".")] if pre else []
        return ([int(x) for x in core.split(".")], not pre, ids)

    return max(versions, key=key, default=None)
'''],
    # 改用標準庫 csv 模組（另外也把 \\r 和前後空白的格子加引號，工單沒定，測試不碰）。
    'V': ['''
import csv
import io


def to_csv_line(fields, delimiter=","):
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator="\\r\\n").writerow(fields)
    return buffer.getvalue()[:-2]
''', '''
def to_csv_line(fields, delimiter=","):
    cells = []
    for value in fields:
        text = "" if value is None else str(value)
        if any(c in text for c in (delimiter, '"', "\\n", "\\r")) or text != text.strip():
            text = '"' + text.replace('"', '""') + '"'
        cells.append(text)
    return delimiter.join(cells)
'''],
    # 2 月 29 日生日用 3 月 1 日滿歲（工單說不會出現）；用逐年加。
    'W': ['''
from datetime import date


def age_on(birth_date, on_date):
    born, on = date.fromisoformat(birth_date), date.fromisoformat(on_date)
    if (born.month, born.day) == (2, 29):
        born = date(born.year, 3, 1)
    years = on.year - born.year
    return years - ((on.month, on.day) < (born.month, born.day))
'''],
    # \\w+ 斷字、casefold、Counter；同次數照字母排。
    'X': ['''
import re
from collections import Counter


def top_words(text, n, stopwords=()):
    stop = {w.casefold() for w in stopwords}
    words = [w for w in re.findall(r"\\w+", text.casefold()) if w not in stop]
    ranked = sorted(Counter(words).items(), key=lambda kv: (-kv[1], kv[0]))
    return [[w, c] for w, c in ranked[:n]]
'''],
}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_reasonable_alternatives_pass_every_test(key):
    task, _ = built(*key)
    for source in ALTERNATIVES[key[0]]:
        function = compiled(source, task['entry_point'])
        for test in task['public_tests'] + task['hidden_tests']:
            assert agrees(function, test), (task['id'], test['args'])


# ---- 每個輸入都在工單或既有程式寫明的前提內 ----

HHMM = re.compile(r'([01][0-9]|2[0-3]):[0-5][05]')


def q_ok(busy, day_start, day_end, min_minutes):
    return all(HHMM.fullmatch(t) for pair in busy for t in pair) and all(s < e for s, e in busy) \
        and HHMM.fullmatch(day_start) and HHMM.fullmatch(day_end) and day_start < day_end and min_minutes >= 5


def r_ok(day, amount, unit):
    return unit in ('days', 'weeks', 'months', 'years') and type(amount) is int and 2000 <= int(day[:4]) <= 2040 \
        and date.fromisoformat(day).isoformat() == day


def s_ok(text):
    return re.fullmatch(r' *-?\$?(\d{1,3}(,\d{3})+|\d+)(\.\d{1,2})? *', text) is not None


def t_ok(items):
    titles = [it['title'] for it in items]
    return all(set(it) <= {'title', 'pinned'} for it in items) and len({t.lower() for t in titles}) == len(titles) \
        and all(t[0].isalpha() and not re.search(r'(?<!\d)0\d', t) for t in titles)


def u_ok(versions):
    return all(re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-(alpha|beta|rc)\.[1-9]\d*)?', v)
               for v in versions)


def v_ok(fields, delimiter=','):
    others = set(',;|') - {delimiter}
    texts = [f for f in fields if isinstance(f, str)]
    return len(fields) >= 2 and all(f is None or type(f) in (int, float, str) for f in fields) \
        and all(t and t == t.strip() and '\r' not in t and not set(t) & others for t in texts)


def w_ok(birth_date, on_date):
    return birth_date[5:] != '02-29' and date.fromisoformat(on_date) >= date.fromisoformat(birth_date)


def x_ok(text, n, stopwords=()):
    # 字只由英文字母組成，標點只在字的前後或單獨出現；沒有撇號、連字號或數字這類有不同斷字做法的情況。
    token = re.compile(r'[^\w\s]*[A-Za-z]+[^\w\s]*|[^\w\s]+')
    return n >= 1 and all(re.fullmatch(r'[a-z]+', w) for w in stopwords) \
        and all(token.fullmatch(t) for t in text.split()) and "'" not in text


WITHIN_TICKET = {'Q': q_ok, 'R': r_ok, 'S': s_ok, 'T': t_ok, 'U': u_ok, 'V': v_ok, 'W': w_ok, 'X': x_ok}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_every_input_stays_within_what_the_ticket_or_code_states(key):
    task, _ = built(*key)
    for test in task['public_tests'] + task['hidden_tests']:
        assert WITHIN_TICKET[key[0]](*args_of(test)), (task['id'], test['args'])


# ---- 題目形狀與 prompt ----

CJK = re.compile(r'[　-鿿＀-￯]')


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_task_shape_prompt_and_determinism(key):
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    assert ct.make(*key) == task and ct.build(*key) == built(*key)
    assert set(task) == {'id', 'family', 'level', 'seed', 'prompt', 'entry_point', 'public_tests', 'hidden_tests',
                         'kind', 'implicit_requirements'}
    assert task['id'] == ids(key) and task['entry_point'] == fam.entry and task['kind'] == 'code'
    assert len(task['public_tests']) == ct.PUBLIC_COUNT
    assert tk.TICKET_HIDDEN_MIN <= len(task['hidden_tests']) <= tk.TICKET_HIDDEN_MAX
    prompt = task['prompt']
    assert prompt.rstrip().endswith('Do not include tests or example calls in the block.')
    assert f'def {fam.entry}(...)' in prompt and not CJK.search(prompt)
    for test in task['public_tests']:
        assert f'- {ct._call(task["entry_point"], test["args"])} returns {test["expected"]!r}' in prompt
        assert len(json.dumps(test['args'])) < 400
    config = fam.levels[key[1]]
    if fam.kind == 'feature':
        title, body = fam.tickets[config['variant']]
        assert prompt.startswith(tk.FEATURE_INTRO) and f'Ticket: {title}' in prompt and body in prompt
        assert '```python\n' not in prompt and f'Implement `{fam.signatures[config["variant"]]}`' in prompt
    else:
        assert prompt.startswith(tk.BUG_INTRO) and tk.BUG_EXAMPLES in prompt
        assert '```python\n' + fam.originals[config['variant']].strip('\n') + '\n```' in prompt
        assert ct._call(task['entry_point'], config['repro']) in prompt.split('Current code:')[0]
        assert task['public_tests'][0]['args'] == config['repro']
        assert 20 <= len(fam.originals[config['variant']].strip().splitlines()) <= 80
    keys = [json.dumps(t['args'], sort_keys=True) for t in task['public_tests'] + task['hidden_tests']]
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_implicit_requirements_are_recorded_for_review_and_kept_out_of_the_prompt(key):
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    records = task['implicit_requirements']
    expected_ids = list(fam.levels[key[1]]['reqs']) + ([tk.REGRESSION] if fam.kind == 'bugfix' else [])
    assert [r['id'] for r in records] == expected_ids
    for record in records:
        assert set(record) == {'id', 'requirement', 'why', 'basis'} and record['basis'] in BASES
        assert CJK.search(record['requirement']) and CJK.search(record['why'])
        assert record['id'] not in task['prompt'] and record['requirement'] not in task['prompt']
    assert 'implicit' not in task['prompt'].lower() and 'hidden' not in task['prompt'].lower()


# ---- 公平性：隱藏測試只碰列出的要求或題目已寫明的情況 ----

@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_hidden_tests_only_touch_listed_requirements_and_cover_each_one(key):
    task, meta = built(*key)
    fam = FAMILIES[key[0]]
    config = fam.levels[key[1]]
    reqs, explicit = set(config['reqs']), set(config.get('explicit', ()))
    counts = Counter()
    for test, row in zip(task['hidden_tests'], meta['hidden']):
        touched = fam.touches(test['args'], test['expected'])
        assert touched == set(row['touched']) and touched <= reqs | explicit, (task['id'], test['args'], touched)
        counts.update(touched & reqs)
    assert all(counts[c] >= tk.TICKET_COVERAGE for c in reqs), (task['id'], counts)
    publics = task['public_tests'][1:] if fam.kind == 'bugfix' else task['public_tests']
    for test in publics:
        assert fam.touches(test['args'], test['expected']) == set(), (task['id'], test['args'])
    if fam.kind == 'bugfix':
        assert fam.touches(config['repro'], task['public_tests'][0]['expected']) <= explicit


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_every_trap_passes_the_public_examples_and_fails_hidden_tests_for_a_listed_reason(key):
    task, meta = built(*key)
    fam = FAMILIES[key[0]]
    config = fam.levels[key[1]]
    assert len(config['traps']) >= 2
    for trap in config['traps']:
        assert fam.trap_breaks[trap] & (set(config['reqs']) | {tk.REGRESSION}), (task['id'], trap)
        function = fam.trap(trap)
        for test in task['public_tests']:
            assert agrees(function, test), (task['id'], trap, test['args'])
        credited = 0
        for test, row in zip(task['hidden_tests'], meta['hidden']):
            if agrees(function, test):
                continue
            touched = set(row['touched'])
            # 每個失敗都要歸得到它違反的某條要求；改壞既有行為的錯誤解另外允許在其他測試失敗。
            assert touched & fam.trap_breaks[trap] or tk.REGRESSION in fam.trap_breaks[trap], \
                (task['id'], trap, test['args'])
            credited += tk._attributable(fam, trap, touched, row['original_ok'])
        assert credited >= tk.TICKET_COVERAGE, (task['id'], trap, credited)


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_traps_that_only_break_unlisted_requirements_pass_every_test(key):
    # 「這級沒列的要求就不測」的反向證據：只違反沒列要求的錯誤解，在這一級必須全對。
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    config = fam.levels[key[1]]
    scope = set(config['reqs']) | set(config.get('explicit', ())) | {tk.REGRESSION}
    outside = [t for t in fam.trap_sources if not fam.trap_breaks[t] & scope and fam.fits(t, key[1])]
    for trap in outside:
        function = fam.trap(trap)
        for test in task['public_tests'] + task['hidden_tests']:
            assert agrees(function, test), (task['id'], trap, test['args'])


def test_some_levels_exercise_the_unlisted_trap_check():
    exercised = 0
    for family, level, _ in KEYS:
        fam = FAMILIES[family]
        config = fam.levels[level]
        scope = set(config['reqs']) | set(config.get('explicit', ())) | {tk.REGRESSION}
        exercised += sum(not fam.trap_breaks[t] & scope and fam.fits(t, level) for t in fam.trap_sources)
    assert exercised >= 20


@pytest.mark.parametrize('key', [k for k in KEYS if FAMILIES[k[0]].kind == 'bugfix'], ids=ids)
def test_bug_report_reproduces_and_hidden_tests_include_regression_tests(key):
    task, meta = built(*key)
    fam = FAMILIES[key[0]]
    original = fam.original(key[1])
    repro, *others = task['public_tests']
    assert not agrees(original, repro)
    assert all(agrees(original, test) for test in others)
    regression = [t for t in task['hidden_tests'] if agrees(original, t)]
    assert len(regression) >= tk.REGRESSION_MIN and len(regression) == meta['regression']
    # 修 bug 題的每一級都有一個錯誤解是回報症狀修好、但同根本原因的其他輸入仍錯。
    assert any(fam.trap_breaks[t] - {tk.REGRESSION} for t in fam.levels[key[1]]['traps'])


def test_family_inventory():
    kinds = [FAMILIES[f].kind for f in 'QRSTUVWX']
    assert kinds == ['feature', 'bugfix'] * 4 and list(FAMILIES) == list('QRSTUVWX')
    assert set(FAMILIES) <= set(ct.FAMILY_REVISION) and not set(FAMILIES) & (set(ct.FAMILIES) | set(ct.TRAP_FAMILIES))
    for fam in FAMILIES.values():
        assert set(fam.trap_breaks) == set(fam.trap_sources)
        used_traps = {t for c in fam.levels.values() for t in c['traps']}
        used_reqs = {r for c in fam.levels.values() for r in c['reqs']}
        assert used_traps == set(fam.trap_sources), fam.letter
        assert used_reqs <= set(fam.categories()) and set(fam.levels) == set(ct.LEVELS)
        assert (tk.REGRESSION in fam.requirements) == (fam.kind == 'bugfix')
        for level, config in fam.levels.items():
            assert all(fam.fits(t, level) for t in config['traps'])
            assert not set(config['reqs']) & set(config.get('explicit', ()))
            assert ('repro' in config) == (fam.kind == 'bugfix')


def test_prompt_examples_for_review():
    # 給人工審查用的兩個範例（新功能與修 bug 各一）；內容由凍結檔的雜湊守住。
    feature = ct.make('W', 5, 0)['prompt']
    bug = ct.make('V', 1, 0)['prompt']
    assert 'Customers born on February 29 are verified manually' in feature
    assert "to_csv_line(['Smith, Jane', 'Boston', 3]) returns '\"Smith, Jane\",Boston,3'" in bug


# ---- 隔離環境抽查 ----

def reply(code):
    return 'Here is the fix.\n\n```python\n' + code + '\n```\n'


@needs_sandbox
def test_reference_passes_and_traps_are_false_accepts_in_the_sandbox():
    jobs = []
    for key in KEYS:
        task, _ = built(*key)
        fam = FAMILIES[key[0]]
        jobs.append((task, None, fam.reference_source))
        jobs.extend((task, trap, fam.trap_sources[trap]) for trap in fam.levels[key[1]]['traps'])

    def run(job):
        task, trap, source = job
        return task['id'], trap, ct.BlindCodeEnvironment(task).validate(reply(source)), \
            ct.grade_hidden(task, reply(source))
    with ThreadPoolExecutor(4) as pool:
        for task_id, trap, public, hidden in pool.map(run, jobs):
            assert public == {'passed': True, 'checks': []}, (task_id, trap)
            assert hidden['status'] == 'ok' and hidden['passed'] is (trap is None), (task_id, trap, hidden)
            if trap is not None:
                assert hidden['hidden_total'] - hidden['hidden_passed'] >= tk.TICKET_COVERAGE, (task_id, trap)


@needs_sandbox
def test_original_buggy_code_fails_the_public_check_in_the_sandbox():
    task = ct.make('R', 1, 0)
    checks = ct.BlindCodeEnvironment(task).validate(reply(tk.R_ORIGINAL))
    assert checks['passed'] is False and 'public_test_failed' in checks['checks']
    assert any('ValueError' in c for c in checks['checks'])


# ---- 凍結與相容 ----

def test_committed_v3_frozen_set_is_the_default_and_interleaved():
    assert ct.FROZEN_CODE_TASKS == ct.FROZEN_CODE_TASKS_V3 == ct.FROZEN_CODE_TASK_SETS['v3']
    tasks = ct.frozen_code_tasks()
    assert len(tasks) == 40
    assert {(t['family'], t['level']) for t in tasks} == {(f, level) for f in FAMILIES for level in ct.LEVELS}
    assert [t['id'] for t in tasks[:8]] == [f'{f}1-0' for f in 'QRSTUVWX']
    assert all(a['family'] != b['family'] for a, b in zip(tasks, tasks[1:]))
    assert [t['id'] for t in ct.frozen_code_tasks(limit=2)] == ['Q1-0', 'R1-0']
    frozen = json.loads(ct.FROZEN_CODE_TASKS_V3.read_text())
    assert frozen['version'] == tk.TICKET_VERSION and set(frozen['family_revision']) == set(FAMILIES)
    assert all(len(r['requirements_sha256']) == 64 for r in frozen['tasks'])
    assert all(t == built(t['family'], t['level'], t['seed'])[0] for t in tasks)


def test_frozen_v3_detects_changed_requirements_and_refuses_mixed_versions(tmp_path):
    tasks = ct.generate(families=['Q', 'X'], levels=(1,), seeds=(0,))
    path = tmp_path / 'v3.json'
    assert ct.freeze(path, tasks)['version'] == tk.TICKET_VERSION
    assert [t['id'] for t in ct.frozen_code_tasks(path)] == ['Q1-0', 'X1-0']
    data = json.loads(path.read_text())
    data['tasks'][1]['requirements_sha256'] = '0' * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='changed: X1-0'):
        ct.frozen_code_tasks(path)
    data['version'] = ct.TRAP_VERSION
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='version'):
        ct.frozen_code_tasks(path)
    with pytest.raises(ValueError, match='exactly one version'):
        ct.freeze(tmp_path / 'mixed.json', tasks + ct.generate(families=['I'], levels=(1,)))


def test_cached_builds_return_independent_copies():
    first = ct.make('S', 1, 0)
    first['hidden_tests'].clear()
    first['implicit_requirements'].clear()
    again = ct.make('S', 1, 0)
    assert len(again['hidden_tests']) >= tk.TICKET_HIDDEN_MIN and again['implicit_requirements']
