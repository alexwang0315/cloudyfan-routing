"""盲測改程式題：題目產生器、獨立核對、隔離執行與盲測環境。全部離線，不呼叫任何模型。

每個題型另有一份獨立寫法的解（下面的 *_SRC，演算法與 code_tasks 的參考解不同），
用來核對全部公開與隱藏測試的期望值；同一份程式碼也拿去隔離執行，確認參考解全過。
"""
from functools import lru_cache
import json
import re
import sys

import pytest

from cloudyfan_jev import code_sandbox as sandbox
from cloudyfan_jev import code_tasks as ct
from cloudyfan_jev.diagnostics import DiagnosticContract

SEEDS = (0, 1)
TRUSTED_SANDBOX = sandbox.sandbox_available()
needs_sandbox = pytest.mark.skipif(not TRUSTED_SANDBOX, reason='sandbox-exec is only available on macOS')


@lru_cache(maxsize=None)
def built(family, level, seed):
    return ct.build(family, level, seed)


def all_tasks(seeds=SEEDS):
    return [(f, level, seed) for f in ct.FAMILIES for level in ct.LEVELS for seed in seeds]


# ---- 第二種獨立寫法（可單獨執行，只用標準庫；開頭會加上 LEVEL 等常數）----

A_SRC = r'''
INF = float('inf')


def _empty(iv):
    s, sc, e, ec = iv
    return s > e or (s == e and not (sc and ec))


def _norm(item):
    if LEVEL == 1:
        a, b = item
        kind = '[]'
    else:
        a, b, kind = item
    if a > b:
        a, b = b, a
    iv = (a, kind[0] == '[', b, kind[1] == ']')
    return None if _empty(iv) else iv


def _meet(x, y):
    s, sc = max((x[0], x[1]), (y[0], y[1]), key=lambda p: (p[0], not p[1]))
    e, ec = min((x[2], x[3]), (y[2], y[3]), key=lambda p: (p[0], p[1]))
    iv = (s, sc, e, ec)
    return None if _empty(iv) else iv


def _union(ivs):
    out = []
    for v in sorted(ivs, key=lambda v: (v[0], not v[1])):
        if out:
            s, sc, e, ec = out[-1]
            if v[0] < e or (v[0] == e and (ec or v[1])):
                if (v[2], v[3]) > (e, ec):
                    out[-1] = (s, sc, v[2], v[3])
                continue
        out.append(v)
    return out


def _bridge(parts, gap):
    out = []
    for v in parts:
        if out and v[0] - out[-1][2] <= gap:
            out[-1] = (out[-1][0], out[-1][1], v[2], v[3])
        else:
            out.append(v)
    return out


def _minus(part, b):
    left = _meet(part, (-INF, False, b[0], not b[1]))
    right = _meet(part, (b[2], not b[3], INF, False))
    return [x for x in (left, right) if x]


def _kind(p):
    return ('[' if p[1] else '(') + (']' if p[3] else ')')


def merge_intervals(intervals, *rest):
    items = [v for v in map(_norm, intervals) if v]
    parts = _union(items)
    if LEVEL == 1:
        return [[p[0], p[2]] for p in parts]
    if LEVEL == 2:
        return [[p[0], p[2], _kind(p)] for p in parts]
    blocked = [] if LEVEL == 3 else [v for v in map(_norm, rest[0]) if v]
    min_len = rest[0] if LEVEL == 3 else rest[1]
    if LEVEL == 5:
        parts = _bridge(parts, rest[2])
    for b in blocked:
        parts = [x for p in parts for x in _minus(p, b)]
    out = []
    for p in parts:
        if p[2] - p[0] >= min_len:
            out.append([p[0], p[2], _kind(p), sum(1 for v in items if _meet(v, p))])
    return out
'''

B_SRC = r'''
import bisect
from datetime import date, timedelta


def _leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def add_workdays(start, n, holidays, weekend=(5, 6), roll='following'):
    if LEVEL < 4:
        weekend = (5, 6)
    if LEVEL < 5:
        roll = 'following'
    s = date.fromisoformat(start)
    week = set(weekend)
    first, last = date(s.year - 2, 1, 1), date(s.year + 2, 12, 31)
    hol = set()
    for h in holidays:
        parts = [int(x) for x in h.split('-')]
        if len(parts) == 3:
            hol.add(date(*parts))
        elif LEVEL >= 3:
            for y in range(first.year, last.year + 1):
                if parts == [2, 29] and not _leap(y):
                    continue
                hol.add(date(y, *parts))
    off = set(hol)
    if LEVEL in (2, 3):
        d = first
        while d <= last:
            if (d + timedelta(1) in hol and d.weekday() == 4) or (d - timedelta(1) in hol and d.weekday() == 0):
                off.add(d)
            d += timedelta(1)
    elif LEVEL >= 4:
        pending, d = 0, min([first] + sorted(hol))
        while d <= last:
            if d.weekday() in week:
                pending += d in hol
            elif d not in hol and pending:
                off.add(d)
                pending -= 1
            d += timedelta(1)
    days, d = [], first
    while d <= last:
        if d.weekday() not in week and d not in off:
            days.append(d)
        d += timedelta(1)
    if n > 0:
        return days[bisect.bisect_right(days, s) + n - 1].isoformat()
    if n < 0:
        return days[bisect.bisect_left(days, s) + n].isoformat()
    i = bisect.bisect_left(days, s)
    if days[i] == s:
        return start
    after, before = days[i], days[i - 1]
    same = lambda x: (x.year, x.month) == (s.year, s.month)
    pick = {'following': after, 'preceding': before,
            'modified_following': after if same(after) else before,
            'modified_preceding': before if same(before) else after}[roll]
    return pick.isoformat()
'''

C_SRC = r'''
import re


def _breaks():
    return re.compile(r'\r\n|\r|\n') if LEVEL >= 3 else re.compile(r'\n')


def _quoted_body():
    if LEVEL >= 4:
        return re.compile(r'[ \t]*"((?:\\[\s\S]|\\\Z|[^"\\]|"")*)("?)[ \t]*')
    return re.compile(r'[ \t]*"((?:[^"]|"")*)("?)[ \t]*')


def _unquoted():
    d = re.escape(DELIM)
    stop = '\\r\\n' if LEVEL >= 3 else '\\n'
    if LEVEL >= 4:
        return re.compile(r'(?:\\[\s\S]|\\\Z|[^\\' + d + stop + r'])*')
    return re.compile('[^' + d + stop + ']*')


def _field(text, pos):
    if LEVEL >= 2 and re.compile(r'[ \t]*"').match(text, pos):
        m = _quoted_body().match(text, pos)
        body = m.group(1)
        if LEVEL >= 4:
            body = re.sub(r'\\([\s\S])|\\\Z|""', lambda x: x.group(1) if x.group(1) is not None
                          else ('"' if x.group(0) == '""' else '\\'), body)
        else:
            body = body.replace('""', '"')
        return (body, True, False), m.end()
    m = _unquoted().match(text, pos)
    raw = m.group(0)
    if LEVEL >= 4:
        items = [(x.group(1), True) if x.group(1) is not None else ('\\', True) if x.group(0) == '\\'
                 else (x.group(0), False) for x in re.finditer(r'\\([\s\S])|\\|[\s\S]', raw)]
    else:
        items = [(c, False) for c in raw]
    while items and not items[0][1] and items[0][0] in ' \t':
        items.pop(0)
    while items and not items[-1][1] and items[-1][0] in ' \t':
        items.pop()
    return (''.join(c for c, _ in items), False, '\\' in raw and LEVEL >= 4), m.end()


def _value(field):
    text, quoted, escaped = field
    if quoted or escaped:
        return text
    if text == '':
        return None
    if re.fullmatch(r'-?(0|[1-9][0-9]*)', text):
        return int(text)
    if text in ('true', 'false'):
        return text == 'true'
    return text


def parse_records(text):
    breaks, pos, records = _breaks(), 0, []
    while pos < len(text):
        m = breaks.search(text, pos)
        end, after = (m.start(), m.end()) if m else (len(text), len(text))
        line = text[pos:end]
        if line.strip(' \t') == '' or (LEVEL >= 3 and line.startswith('#')):
            pos = after
            continue
        fields = []
        while True:
            field, pos = _field(text, pos)
            fields.append(field)
            if text[pos:pos + 1] == DELIM:
                pos += 1
                continue
            m = breaks.match(text, pos)
            pos = m.end() if m else pos
            break
        records.append(fields)
    if LEVEL <= 4:
        return [[f[0] for f in r] for r in records]
    if not records:
        return []
    header = [f[0] for f in records[0]]
    rows = []
    for record in records[1:]:
        values = [_value(f) for f in record]
        row = dict(zip(header, values + [None] * (len(header) - len(values))))
        if len(values) > len(header):
            row['_extra'] = values[len(header):]
        rows.append(row)
    return rows
'''

D_SRC = r'''
from fractions import Fraction


def _lrm(amount, weights):
    total = sum(weights)
    if total == 0:
        return [0] * len(weights)
    ideal = [Fraction(amount * w, total) for w in weights]
    shares = [int(x) for x in ideal]
    rank = sorted(range(len(weights)), key=lambda i: (shares[i] - ideal[i], -weights[i] if LEVEL >= 2 else 0, i))
    for i in rank[:amount - sum(shares)]:
        shares[i] += 1
    return shares


def _capped(amount, weights, caps):
    open_, fixed = list(range(len(weights))), {}
    while True:
        tentative = dict(zip(open_, _lrm(amount - sum(fixed.values()), [weights[i] for i in open_])))
        over = [i for i in open_ if caps[i] is not None and tentative[i] > caps[i]]
        if not over:
            fixed.update(tentative)
            return [fixed.get(i, 0) for i in range(len(weights))]
        fixed.update({i: caps[i] for i in over})
        open_ = [i for i in open_ if i not in over]


def allocate(total, weights, caps=None, unit=1):
    sign, amount = (-1 if total < 0 else 1), abs(total)
    if LEVEL <= 3:
        return [sign * s for s in _lrm(amount, weights)]
    if LEVEL == 4:
        shares = _capped(amount, weights, caps)
    else:
        q, r = divmod(amount, unit)
        shares = [s * unit for s in _capped(q, weights, [None if c is None else c // unit for c in caps])]
        fits = [i for i in range(len(weights)) if weights[i] > 0 and (caps[i] is None or shares[i] + r <= caps[i])]
        if r and fits:
            shares[sorted(fits, key=lambda i: (-weights[i], i))[0]] += r
    return {'shares': [sign * s for s in shares], 'unallocated': sign * (amount - sum(shares))}
'''

E_SRC = r'''
import re

CORE = re.compile(r'[0-9]+(?:\.[0-9]+){0,3}')
IDENT = re.compile(r'[0-9A-Za-z-]+' if LEVEL >= 3 else r'[0-9A-Za-z]+')


def _key(text):
    s = text
    if LEVEL >= 3:
        s = s.strip(' ')
        if s[:1] in ('v', 'V'):
            s = s[1:]
    build = pre = None
    if LEVEL >= 3 and '+' in s:
        s, build = s.split('+', 1)
    if LEVEL >= 2 and '-' in s:
        s, pre = s.split('-', 1)
    if not CORE.fullmatch(s):
        return None
    for part in (pre, build):
        if part is not None and not all(IDENT.fullmatch(x) for x in part.split('.')):
            return None
    core = [int(x) for x in s.split('.')]
    while core and core[-1] == 0:
        core.pop()
    if pre is None:
        return (tuple(core), (1,))
    return (tuple(core), (0, tuple((0, int(x)) if x.isdigit() else (1, x) for x in pre.split('.'))))


def sort_versions(versions, descending=False):
    keyed = [(_key(v), v) for v in versions]
    valid = sorted([kv for kv in keyed if kv[0] is not None], key=lambda kv: kv[0],
                   reverse=LEVEL == 5 and bool(descending))
    return [v for _, v in valid] + [v for k, v in keyed if k is None]
'''

F_SRC = r'''
from collections import deque


def rate_limit(events, limit, window, global_limit=None, cooldown=None):
    if LEVEL == 1:
        evs = [(t, 'k', 1) for t in events]
    elif LEVEL == 2:
        evs = [(e[0], e[1], 1) for e in events]
    else:
        evs = [tuple(e) for e in events]
    queues, sums, block = {}, {}, {}
    everything, total = deque(), 0
    result = [False] * len(evs)
    for i in sorted(range(len(evs)), key=lambda i: (evs[i][0], i)):
        t, key, cost = evs[i]
        while everything and everything[0][0] <= t - window:
            total -= everything.popleft()[1]
        q = queues.setdefault(key, deque())
        while q and q[0][0] <= t - window:
            sums[key] -= q.popleft()[1]
        if LEVEL == 5 and t < block.get(key, t):
            continue
        key_ok = sums.get(key, 0) + cost <= limit
        global_ok = LEVEL < 4 or total + cost <= global_limit
        if key_ok and global_ok:
            result[i] = True
            q.append((t, cost))
            everything.append((t, cost))
            sums[key] = sums.get(key, 0) + cost
            total += cost
        elif LEVEL == 5 and not key_ok:
            block[key] = t + cooldown
    return result
'''

G_SRC = r'''
def _show(lots):
    out = []
    for qty, cost, _ in lots:
        if out and LEVEL >= 2 and out[-1][1] == cost:
            out[-1][0] += qty
        else:
            out.append([qty, cost])
    return out


def process(ops):
    stock, sold, seen = {}, {}, []
    cogs = shrinkage = 0
    rejected, shortfalls, cancelled = [], [], set()
    for i, op in enumerate(ops):
        if LEVEL <= 2:
            op = [op[0], '_'] + list(op[1:])
        if op[0] == 'cancel':
            j = op[1]
            target = ops[j]
            if target[0] != 'buy' or j in cancelled or \
                    sum(l[0] for l in stock.get(target[1], []) if l[2] == j) != target[2]:
                rejected.append(i)
                continue
            cancelled.add(j)
            stock[target[1]] = [l for l in stock[target[1]] if l[2] != j]
            continue
        sku = op[1]
        if sku not in stock:
            stock[sku], sold[sku] = [], []
            seen.append(sku)
        lots = stock[sku]
        have = sum(l[0] for l in lots)
        if op[0] == 'buy':
            if op[2]:
                lots.append([op[2], op[3], i])
        elif op[0] == 'sell':
            qty, partial = op[2], len(op) > 3 and op[3]
            if qty > have and not partial:
                rejected.append(i)
                continue
            if qty > have:
                shortfalls.append([i, qty - have])
            take = min(qty, have)
            while take:
                n = min(take, lots[0][0])
                cogs += n * lots[0][1]
                sold[sku].append([n, lots[0][1]])
                lots[0][0] -= n
                take -= n
                if not lots[0][0]:
                    lots.pop(0)
        elif op[0] == 'return':
            qty = op[2]
            if qty > sum(s[0] for s in sold[sku]):
                rejected.append(i)
                continue
            back = []
            while qty:
                last = sold[sku][-1]
                n = min(qty, last[0])
                back.insert(0, [n, last[1], None])
                cogs -= n * last[1]
                last[0] -= n
                qty -= n
                if not last[0]:
                    sold[sku].pop()
            stock[sku] = back + lots
        elif op[0] == 'adjust':
            new = op[2]
            drop = have - new
            while drop > 0:
                n = min(drop, lots[-1][0])
                shrinkage += n * lots[-1][1]
                lots[-1][0] -= n
                drop -= n
                if not lots[-1][0]:
                    lots.pop()
            if new > have:
                lots.append([new - have, lots[-1][1] if lots else 0, None])
    out = {'cogs': cogs, 'rejected': rejected}
    if LEVEL <= 2:
        out['stock'] = _show(stock.get('_', []))
    else:
        out['stock'] = {k: _show(stock[k]) for k in seen if stock[k]}
    if LEVEL >= 2:
        out['shortfalls'] = shortfalls
    if LEVEL >= 4:
        out['shrinkage'] = shrinkage
    return out
'''

H_SRC = r'''
import re


def _special(c):
    return LEVEL >= 2 and (c in '0123456789\\' or (LEVEL == 5 and c in '_\n'))


def _token(c, counted):
    if LEVEL == 5 and c == ' ' and counted:
        return '_'
    if LEVEL == 5 and c == '\n':
        return '\\n'
    return '\\' + c if _special(c) else c


def _piece(c, n):
    threshold = 2 if LEVEL >= 4 and _special(c) else 3
    return str(n) + _token(c, True) if n >= threshold else _token(c, False) * n


def encode(s):
    out = []
    for m in re.finditer(r'(.)\1*', s, re.S):
        c, n = m.group(1), len(m.group(0))
        if LEVEL >= 3:
            full, rest = divmod(n, 9)
            out.append(('9' + _token(c, True)) * full)
            if rest:
                out.append(_piece(c, rest))
        else:
            out.append(_piece(c, n))
    return ''.join(out)
'''

SOURCES = {'A': A_SRC, 'B': B_SRC, 'C': C_SRC, 'D': D_SRC, 'E': E_SRC, 'F': F_SRC, 'G': G_SRC, 'H': H_SRC}


def source(family, level, params):
    header = f'LEVEL = {level}\n'
    if family == 'C':
        header += f'DELIM = {params["delimiter"]!r}\n'
    return header + SOURCES[family]


@lru_cache(maxsize=None)
def independent(family, level, delimiter=None):
    namespace = {}
    exec(source(family, level, {'delimiter': delimiter}), namespace)
    return namespace[ct.FAMILIES[family].entry]


def h_decode(level, encoded):
    """編碼的反運算，用來確認壓縮結果可唯一還原。"""
    out, i = [], 0
    while i < len(encoded):
        m = re.match(r'[0-9]+', encoded[i:])
        count = int(m.group(0)) if m else 1
        i += len(m.group(0)) if m else 0
        c = encoded[i]
        if c == '\\':
            c = encoded[i + 1]
            c = '\n' if level == 5 and c == 'n' else c
            i += 2
        else:
            c = ' ' if level == 5 and c == '_' else c
            i += 1
        out.append(c * count)
    return ''.join(out)


# ---- 題目形狀與獨立核對 ----

@pytest.mark.parametrize('key', all_tasks(), ids=lambda k: f'{k[0]}{k[1]}-{k[2]}')
def test_every_expected_value_matches_an_independent_implementation(key):
    task, meta = built(*key)
    solve = independent(key[0], key[1], meta['params'].get('delimiter'))
    for test in task['public_tests'] + task['hidden_tests']:
        args = json.loads(json.dumps(test['args']))
        assert sandbox.strict_equal(solve(*args), test['expected']), (task['id'], test)


@pytest.mark.parametrize('key', all_tasks(), ids=lambda k: f'{k[0]}{k[1]}-{k[2]}')
def test_public_examples_avoid_every_edge_clause_and_hidden_tests_cover_each(key):
    task, meta = built(*key)
    fam, level, params = ct.FAMILIES[key[0]], key[1], meta['params']
    assert len(task['public_tests']) == 3 and len(task['hidden_tests']) >= 50
    for test in task['public_tests']:
        assert fam.edges(level, params, test['args']) == set()
    names = fam.edge_names(level)
    assert names
    counts = {e: sum(e in fam.edges(level, params, t['args']) for t in task['hidden_tests']) for e in names}
    assert min(counts.values()) >= ct.EDGE_COVERAGE, counts
    # 至少一半的隱藏測試碰到邊界條款。
    assert sum(1 for t in task['hidden_tests'] if fam.edges(level, params, t['args'])) * 2 >= len(task['hidden_tests'])
    # 難度越高，邊界條款越多（第 4 級起有的題型換了規則，只要求不少於第 1 級、不多於第 5 級）。
    assert len(fam.edge_names(1)) <= len(names) <= len(fam.edge_names(5))


@pytest.mark.parametrize('key', all_tasks(seeds=(0,)), ids=lambda k: f'{k[0]}{k[1]}-{k[2]}')
def test_dropping_an_edge_clause_passes_the_public_examples_but_fails_hidden_tests(key):
    """每條邊界條款都對應一個「公開範例全過、隱藏測試沒過」的錯誤寫法：正是盲測要量的誤放行。"""
    task, meta = built(*key)
    fam, level, params = ct.FAMILIES[key[0]], key[1], meta['params']
    for edge in fam.toggles(level):
        if key[0] == 'C':
            continue
        skip = frozenset({edge})
        assert all(fam.solve(level, params, t['args'], skip) == t['expected'] for t in task['public_tests'])
        assert any(fam.solve(level, params, t['args'], skip) != t['expected'] for t in task['hidden_tests'])


@pytest.mark.parametrize('key', all_tasks(seeds=(0,)), ids=lambda k: f'{k[0]}{k[1]}-{k[2]}')
def test_task_shape_prompt_and_determinism(key):
    task = ct.make(*key)
    assert task == built(*key)[0]
    assert set(task) == {'id', 'family', 'level', 'seed', 'prompt', 'entry_point', 'public_tests', 'hidden_tests',
                         'kind'}
    assert task['id'] == f'{key[0]}{key[1]}-{key[2]}' and task['kind'] == 'code'
    assert json.loads(json.dumps(task)) == task
    prompt = task['prompt']
    assert f'def {task["entry_point"]}(...)' in prompt and '```python' in prompt
    assert prompt.rstrip().endswith('Do not include tests or example calls in the block.')
    for test in task['public_tests']:
        assert ct._call(task['entry_point'], test['args']) in prompt and repr(test['expected']) in prompt
    public = {json.dumps(t['args'], sort_keys=True) for t in task['public_tests']}
    assert not public & {json.dumps(t['args'], sort_keys=True) for t in task['hidden_tests']}


def test_csv_header_names_are_distinct_and_never_extra():
    for seed in SEEDS:
        task, meta = built('C', 5, seed)
        for test in task['public_tests'] + task['hidden_tests']:
            records = ct.FAMILIES['C'].parse(4, meta['params']['delimiter'], test['args'][0], set())
            if records:
                assert len(set(records[0])) == len(records[0]) and '_extra' not in records[0]


@pytest.mark.parametrize('key', [(5, s) for s in SEEDS] + [(l, 0) for l in (1, 2, 3, 4)])
def test_run_length_encoding_round_trips(key):
    level, seed = key
    task, _ = built('H', level, seed)
    for test in task['public_tests'] + task['hidden_tests']:
        assert h_decode(level, test['expected']) == test['args'][0]


def test_generate_interleave_and_freeze_round_trip(tmp_path):
    tasks = ct.generate(families=['A', 'H'], levels=(1, 2), seeds=(0,))
    assert [t['id'] for t in tasks] == ['A1-0', 'A2-0', 'H1-0', 'H2-0']
    assert [t['id'] for t in ct.interleave(tasks)] == ['A1-0', 'H1-0', 'A2-0', 'H2-0']
    path = tmp_path / 'frozen.json'
    frozen = ct.freeze(path, tasks)
    assert frozen['version'] == ct.VERSION and len(frozen['tasks']) == 4
    assert [t['id'] for t in ct.frozen_code_tasks(path)] == ['A1-0', 'H1-0', 'A2-0', 'H2-0']
    assert [t['id'] for t in ct.frozen_code_tasks(path, limit=1)] == ['A1-0']
    data = json.loads(path.read_text())
    data['tasks'][0]['tests_sha256'] = '0' * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='changed'):
        ct.frozen_code_tasks(path)
    data['version'] = 'other'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='version'):
        ct.frozen_code_tasks(path)


def test_committed_frozen_set_rebuilds_and_is_interleaved():
    tasks = ct.frozen_code_tasks()
    assert len(tasks) == 40
    assert {(t['family'], t['level']) for t in tasks} == {(f, l) for f in ct.FAMILIES for l in ct.LEVELS}
    assert [t['family'] for t in tasks[:8]] == list(ct.FAMILIES)
    assert all(a['family'] != b['family'] for a, b in zip(tasks, tasks[1:]))


# ---- 取程式碼 ----

def test_extract_code_takes_the_last_python_block():
    text = 'Plan:\n```python\ndef f():\n    return 1\n```\nFixed:\n```python\ndef f():\n    return 2\n```\nDone.'
    assert ct.extract_code(text) == 'def f():\n    return 2\n'
    assert ct.extract_code('```py\nx = 1\n```') == 'x = 1\n'
    assert ct.extract_code('```Python3\nx = 2\n```\n```text\nnot code\n```') == 'x = 2\n'
    assert ct.extract_code('no code here') is None
    assert ct.extract_code('```js\nlet x\n```') is None
    assert ct.extract_code('```python\n   \n```') is None
    assert ct.extract_code('```python\ndef f(): pass') is None
    assert ct.extract_code(None) is None


# ---- 隔離執行 ----

def reply(code):
    return 'Here is the function.\n\n```python\n' + code + '\n```\n'


@pytest.mark.parametrize('key', all_tasks(seeds=(0,)), ids=lambda k: f'{k[0]}{k[1]}-{k[2]}')
def test_reference_solutions_pass_public_and_hidden_tests_in_the_runner(key):
    task, meta = built(*key)
    text = reply(source(key[0], key[1], meta['params']))
    assert BlindCodeEnv(task).validate(text) == {'passed': True, 'checks': []}
    assert ct.grade_hidden(task, text, require_sandbox=TRUSTED_SANDBOX) == {
        'passed': True, 'hidden_passed': len(task['hidden_tests']), 'hidden_total': len(task['hidden_tests']),
        'status': 'ok'}


def BlindCodeEnv(task):
    return ct.BlindCodeEnvironment(task, require_sandbox=TRUSTED_SANDBOX)


SIMPLE = [{'args': [1, 2], 'expected': 3}, {'args': [2, 2], 'expected': 4}, {'args': [0, 5], 'expected': 5}]


def run(code, tests=SIMPLE, **kwargs):
    return sandbox.run_tests(code, 'f', tests, require_sandbox=TRUSTED_SANDBOX, **kwargs)


def test_runner_statuses():
    assert run(None)['status'] == 'no_code'
    assert run('   ')['status'] == 'no_code'
    assert run('def f(a, b:\n')['status'] == 'syntax_error'
    ok = run('def f(a, b):\n    print("noise")\n    return a + b\n')
    assert ok == {'status': 'ok', 'passed': 3, 'total': 3, 'failures': []}
    wrong = run('def f(a, b):\n    return a - b if a == 2 else a + b\n')
    assert wrong == {'status': 'ok', 'passed': 2, 'total': 3,
                     'failures': [{'index': 1, 'kind': 'wrong', 'detail': 'expected 4, got 0'}]}
    raised = run('def f(a, b):\n    return a / 0\n')
    assert raised['status'] == 'ok' and raised['passed'] == 0
    assert {f['detail'] for f in raised['failures']} == {'ZeroDivisionError'}
    typed = run('def f(a, b):\n    return True if a == 0 else a + b\n')
    assert typed['passed'] == 2 and typed['failures'][0]['detail'] == 'expected 5, got true'
    missing = run('def g(a, b):\n    return a + b\n')
    assert {f['detail'] for f in missing['failures']} == {'entry_point_missing'}
    imported = run('import numpy\ndef f(a, b):\n    return a + b\n')
    assert {f['detail'] for f in imported['failures']} == {'load_error: ModuleNotFoundError'}
    assert run('import os\ndef f(a, b):\n    os._exit(0)\n')['status'] == 'crashed'
    assert run('def f(a, b):\n    raise SystemExit(3)\n')['failures'][0]['detail'] == 'SystemExit'


def test_runner_times_out_infinite_loops():
    assert run('def f(a, b):\n    while True:\n        pass\n', timeout_s=1)['status'] == 'timeout'
    assert run('while True:\n    pass\n', timeout_s=1)['status'] == 'timeout'
    slow_one = run('def f(a, b):\n    while a == 2:\n        pass\n    return a + b\n', timeout_s=4)
    assert slow_one['status'] == 'timeout' and slow_one['passed'] == 2
    assert slow_one['failures'] == [{'index': 1, 'kind': 'error', 'detail': 'timeout'}]


def test_runner_never_shows_expected_values_to_the_program(tmp_path):
    secret = [{'args': [1], 'expected': 'TOP-SECRET-EXPECTED'}]
    code = ('import gc, sys\n'
            'def f(x):\n'
            '    blob = repr(sys.argv) + repr([o for o in gc.get_objects() if isinstance(o, str)])\n'
            '    return "TOP-SECRET-EXPECTED" in blob\n')
    result = sandbox.run_tests(code, 'f', secret, require_sandbox=TRUSTED_SANDBOX)
    assert result['failures'] == [{'index': 0, 'kind': 'wrong',
                                   'detail': 'expected "TOP-SECRET-EXPECTED", got false'}]


def test_require_sandbox_fails_closed_without_sandbox_exec(monkeypatch):
    monkeypatch.setattr(sandbox, 'sandbox_available', lambda: False)
    calls = []
    monkeypatch.setattr(sandbox.subprocess, 'Popen', lambda *a, **k: calls.append(a))
    result = sandbox.run_tests('def f(a, b):\n    return a + b\n', 'f', SIMPLE)
    assert result['status'] == 'sandbox_unavailable' and result['passed'] == 0 and not calls
    task, _ = built('H', 1, 0)
    assert ct.BlindCodeEnvironment(task).validate(reply('def encode(s):\n    return s\n')) == {
        'passed': False, 'checks': ['sandbox_unavailable']}
    assert ct.grade_hidden(task, reply('def encode(s):\n    return s\n'))['status'] == 'sandbox_unavailable'


def test_sandbox_profile_denies_by_default():
    profile = sandbox.sandbox_profile('/tmp/work', '/opt/py/bin/python3', '/opt/py')
    assert profile.splitlines()[1] == '(deny default)'
    assert 'network' not in profile and 'process-fork' not in profile
    assert '(allow file-write* (subpath "/tmp/work") (literal "/dev/null"))' in profile
    with pytest.raises(ValueError):
        sandbox.sandbox_profile('/tmp/"x', '/opt/py/bin/python3', '/opt/py')


@needs_sandbox
def test_sandbox_blocks_network_writes_outside_and_subprocesses(tmp_path):
    outside = tmp_path / 'escaped.txt'
    code = f'''
import os, socket, subprocess
def f(kind):
    try:
        if kind == 'net':
            socket.create_connection(('127.0.0.1', 9), timeout=1)
        elif kind == 'write':
            open({str(outside)!r}, 'w').write('x')
        elif kind == 'home':
            open(os.path.expanduser('~/.zshrc')).read()
        elif kind == 'exec':
            subprocess.run(['/bin/echo', 'hi'], capture_output=True)
        elif kind == 'fork':
            pid = os.fork()
            if pid == 0:
                os._exit(0)
        elif kind == 'local':
            open('scratch.txt', 'w').write('ok')
        return 'allowed'
    except Exception as error:
        return 'blocked:' + type(error).__name__
'''
    kinds = ['net', 'write', 'home', 'exec', 'fork', 'local']
    tests = [{'args': [k], 'expected': None} for k in kinds]
    result = sandbox.run_tests(code, 'f', tests, require_sandbox=True)
    got = {kinds[f['index']]: f['detail'] for f in result['failures']}
    for kind in ('net', 'write', 'home', 'exec', 'fork'):
        assert 'blocked:' in got[kind], (kind, got[kind])
    assert '"allowed"' in got['local']
    assert not outside.exists()


def test_runner_uses_a_throwaway_directory_outside_the_project():
    code = 'import os\ndef f(a, b):\n    return os.getcwd()\n'
    result = run(code, [{'args': [0, 0], 'expected': ''}])
    cwd = json.loads(result['failures'][0]['detail'].split(', got ', 1)[1])
    assert 'cf-code-' in cwd and 'cloudyfan-mrc' not in cwd
    import os
    assert not os.path.exists(cwd)


# ---- 盲測環境 ----

def test_environment_has_no_tools():
    env = BlindCodeEnv(built('A', 1, 0)[0])
    assert env.replayable_tools == () and env.tools_schema() == []
    assert env.call('check', {})['error']['code'] == 'unknown_tool'


def test_plausible_wrong_solution_passes_public_but_fails_hidden():
    """門檻差一（只有 n > 3 才壓縮）：公開範例碰不到長度剛好 3 的連續字元，只有隱藏測試抓得到。"""
    task, _ = built('H', 1, 0)
    code = ('import itertools\n'
            'def encode(s):\n'
            '    out = []\n'
            '    for c, g in itertools.groupby(s):\n'
            '        n = len(list(g))\n'
            '        out.append(f"{n}{c}" if n > 3 else c * n)\n'
            '    return "".join(out)\n')
    assert BlindCodeEnv(task).validate(reply(code)) == {'passed': True, 'checks': []}
    graded = ct.grade_hidden(task, reply(code), require_sandbox=TRUSTED_SANDBOX)
    assert graded['passed'] is False and graded['status'] == 'ok'
    assert 0 < graded['hidden_passed'] < graded['hidden_total']


@pytest.mark.parametrize('key', all_tasks(seeds=(0,)), ids=lambda k: f'{k[0]}{k[1]}-{k[2]}')
def test_validate_reports_only_public_cases_and_fits_the_diagnostic_contract(key, monkeypatch):
    task, _ = built(*key)
    seen = []
    real = sandbox.run_tests

    def spy(code, entry, tests, **kwargs):
        seen.append(tests)
        return real(code, entry, tests, **kwargs)
    monkeypatch.setattr(sandbox, 'run_tests', spy)
    wrong = reply(f'def {task["entry_point"]}(*args):\n    return None\n')
    result = BlindCodeEnv(task).validate(wrong)
    assert seen == [task['public_tests']]
    assert result['passed'] is False and result['checks'][0] == 'public_test_failed'
    assert [c.split(':')[0] for c in result['checks'][1:]] == ['public_case_1', 'public_case_2', 'public_case_3']
    assert all(isinstance(c, str) and len(c) <= ct.CHECK_CHARS for c in result['checks'])
    DiagnosticContract().project(result)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 4096
    joined = '\n'.join(result['checks'])
    prompt = task['prompt']
    for test in task['hidden_tests']:
        for text in (repr(test['args']), ct._call(task['entry_point'], test['args'])):
            if len(text) >= 12 and text not in prompt:
                assert text not in joined


def test_validate_check_codes():
    task, _ = built('H', 1, 0)
    env = BlindCodeEnv(task)
    assert env.validate('I think it is fine.') == {'passed': False, 'checks': ['code_block_missing']}
    assert env.validate(reply('def encode(s:\n')) == {'passed': False, 'checks': ['syntax_error']}
    assert env.validate(reply('def other(s):\n    return s\n')) == {'passed': False, 'checks': ['entry_point_missing']}
    assert env.validate(reply('import numpy\ndef encode(s):\n    return s\n')) == {
        'passed': False, 'checks': ['load_error: ModuleNotFoundError']}
    raised = env.validate(reply('def encode(s):\n    return 1 / 0\n'))
    assert raised['checks'][0] == 'public_test_failed'
    assert raised['checks'][1].startswith("public_case_1: encode('") and raised['checks'][1].endswith(
        'raised ZeroDivisionError')
    wrong = env.validate(reply('def encode(s):\n    return s\n'))
    first = task['public_tests'][0]
    assert wrong['checks'][1] == (f"public_case_1: encode({first['args'][0]!r}) expected "
                                  f"{json.dumps(first['expected'])}, got {json.dumps(first['args'][0])}")
    long_task = dict(task, public_tests=[{'args': ['x' * 500], 'expected': 'y' * 500}])
    clipped = BlindCodeEnv(long_task).validate(reply('def encode(s):\n    return s\n'))
    assert len(clipped['checks'][1]) == ct.CHECK_CHARS and clipped['checks'][1].endswith('...')


def test_validate_reports_timeouts():
    task, _ = built('H', 1, 0)
    result = BlindCodeEnv(task).validate(reply('def encode(s):\n    while True:\n        pass\n'))
    assert result['passed'] is False and result['checks'][0] == 'timeout'


def test_public_checks_respect_the_byte_limit():
    task = {'entry_point': 'f', 'public_tests': [{'args': ['é' * 300], 'expected': 'ü' * 300}] * 40}
    outcome = {'status': 'ok', 'passed': 0, 'total': 40,
               'failures': [{'index': i, 'kind': 'wrong', 'detail': 'expected x, got y'} for i in range(40)]}
    checks = ct.public_checks(task, outcome)
    assert len(json.dumps({'passed': False, 'checks': checks}, ensure_ascii=False).encode()) <= ct.CHECKS_MAX_BYTES
    assert checks[0] == 'public_test_failed' and len(checks) > 3


def test_runner_platform_guard():
    assert TRUSTED_SANDBOX == (sys.platform == 'darwin' and sandbox.os.access(sandbox.SANDBOX_EXEC, sandbox.os.X_OK))
