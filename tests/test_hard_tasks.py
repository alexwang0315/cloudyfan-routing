"""#81 試跑題集：每個題型的參考答案以第二種獨立方法核對；驗收器只回傳代碼。不呼叫任何模型。"""
from fractions import Fraction
from functools import lru_cache
from itertools import permutations, product
import json
import sqlite3

import pytest

from cloudyfan_jev import hard_tasks as h

SEEDS = range(3)


def tasks(family, levels=h.LEVELS, seeds=SEEDS):
    return [h.make(family, level, seed) for level in levels for seed in seeds]


# ---- A：遞迴（含是否經過檢查點的狀態）對分段 DP ----

def a_recursive(grid, diagonal):
    rows, cols = len(grid), len(grid[0])

    @lru_cache(maxsize=None)
    def go(r, c, seen):
        if r >= rows or c >= cols or grid[r][c] == '#':
            return 0
        seen = seen or grid[r][c] == 'C'
        if (r, c) == (rows - 1, cols - 1):
            return 1 if seen else 0
        return go(r + 1, c, seen) + go(r, c + 1, seen) + (go(r + 1, c + 1, seen) if diagonal else 0)
    return go(0, 0, False)


@pytest.mark.parametrize('task', tasks('A'), ids=lambda t: t['id'])
def test_a_paths_match_an_independent_recursion(task):
    assert int(task['answer']) == a_recursive(tuple(task['source']['grid']), task['source']['diagonal']) >= 10


# ---- B：逐次抽球的狀態 DP 對組合公式 ----

def b_sequential(counts, draws, exact_red, min_blue, last_red):
    @lru_cache(maxsize=None)
    def go(r, b, g, left, last):
        if left == 0:
            red, blue = counts[0] - r, counts[1] - b
            ok = red == exact_red and blue >= min_blue and (not last_red or last == 'r')
            return Fraction(int(ok))
        total = r + b + g
        out = Fraction(0)
        for colour, n in (('r', r), ('b', b), ('g', g)):
            if n:
                nxt = (r - (colour == 'r'), b - (colour == 'b'), g - (colour == 'g'))
                out += Fraction(n, total) * go(*nxt, left - 1, colour)
        return out
    return go(*counts, draws, None)


@pytest.mark.parametrize('task', tasks('B'), ids=lambda t: t['id'])
def test_b_probability_matches_draw_by_draw_dp(task):
    s = task['source']
    expected = b_sequential(s['counts'], s['draws'], s['exact_red'], s['min_blue'], s['last_red'])
    assert Fraction(task['answer']) == expected and 0 < expected < 1


# ---- C：四間房以窮舉核對唯一解；五間房核對答案滿足所有線索 ----

def c_brute(houses, clues):
    values = [h.C_NAMES[:houses], h.C_COLORS[:houses], h.C_PETS[:houses]]
    found = []
    for a in permutations(range(houses)):
        for b in permutations(range(houses)):
            for c in permutations(range(houses)):
                pos = {}
                for i, perm in enumerate((a, b, c)):
                    pos.update({(i, v): p for v, p in zip(values[i], perm)})
                if all(h.c_holds(cl, pos) for cl in clues):
                    found.append(h.c_assignment(houses, pos))
    return found


@pytest.mark.parametrize('task', tasks('C', levels=(1, 2), seeds=range(2)), ids=lambda t: t['id'])
def test_c_four_house_puzzles_have_exactly_the_reference_solution(task):
    s = task['source']
    assert c_brute(s['houses'], s['clues']) == [task['answer']]


@pytest.mark.parametrize('task', tasks('C', levels=(3, 5), seeds=range(1)), ids=lambda t: t['id'])
def test_c_five_house_answer_satisfies_every_clue(task):
    s, answer = task['source'], task['answer']
    pos = {(i, v): house for i, attr in enumerate(h.C_ATTRS) for house, v in enumerate(answer[attr])}
    assert all(h.c_holds(clue, pos) for clue in s['clues'])
    assert len(h.c_solutions(s['houses'], s['clues'], limit=3)) == 1


# ---- D：列舉所有路徑對時間維度 DP ----

def d_enumerate(nodes, edges, budget):
    out = {}
    for u, v, c, t in edges:
        out.setdefault(u, []).append((v, c, t))
    best = []

    def walk(node, cost, time):
        if time > budget:
            return
        if node == nodes - 1:
            best.append(cost)
            return
        for v, c, t in out.get(node, []):
            walk(v, cost + c, time + t)
    walk(0, 0, 0)
    return min(best)


@pytest.mark.parametrize('task', tasks('D'), ids=lambda t: t['id'])
def test_d_minimum_cost_matches_path_enumeration(task):
    s = task['source']
    assert int(task['answer']) == d_enumerate(s['nodes'], s['edges'], s['budget'])


# ---- E：直接以 Python 寫出同樣的語意，對執行產生的程式碼 ----

def e_native(p):
    ops = {'+': lambda a, b: a + b, '-': lambda a, b: a - b, '*': lambda a, b: a * b, '^': lambda a, b: a ^ b}
    o = [ops[k] for k in p['ops']]
    x, acc = list(p['x']), p['acc']
    for i in range(p['n']):
        j = (i * p['q'] + p['r']) % len(x)
        if o[0](x[j], acc) % p['d'] == p['e']:
            acc = o[1](acc, x[j]) % p['m']
            x[j] = o[2](x[j], i) % p['m']
        else:
            k = (j + 1) % len(x)
            x[k] = o[3](x[k], acc) % p['m']
            acc = (acc + p['step']) % p['m']
    if p['second']:
        count = 0
        while acc > p['floor']:
            acc = acc // 3 + x[count % len(x)] % 2
            count += 1
    return f'{acc} {sum(x)}'


@pytest.mark.parametrize('task', tasks('E'), ids=lambda t: t['id'])
def test_e_output_matches_a_native_reimplementation(task):
    assert task['answer'] == e_native(task['source']['params'])


# ---- F：位元運算對字串模擬 ----

def f_bits(rule, cells, steps):
    width, value = len(cells), int(cells, 2)
    mask = (1 << width) - 1
    for _ in range(steps):
        left = ((value << 1) | (value >> (width - 1))) & mask   # 每格的右鄰
        right = ((value >> 1) | ((value & 1) << (width - 1))) & mask  # 每格的左鄰
        new = 0
        for i in range(width):
            pattern = (((right >> i) & 1) << 2) | (((value >> i) & 1) << 1) | ((left >> i) & 1)
            new |= ((rule >> pattern) & 1) << i
        value = new
    return format(value, f'0{width}b')


@pytest.mark.parametrize('task', tasks('F'), ids=lambda t: t['id'])
def test_f_row_matches_a_bitwise_simulation(task):
    s = task['source']
    assert task['answer'] == f_bits(s['rule'], s['cells'], s['steps'])


# ---- G：SQLite 查詢對逐列處理 ----

def g_sql(data):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE customers (customer, region, joined)')
    db.execute('CREATE TABLE orders (order_id, customer, date, status, amount)')
    db.execute('CREATE TABLE refunds (order_id, amount)')
    db.executemany('INSERT INTO customers VALUES (:customer, :region, :joined)', data['customers'])
    db.executemany('INSERT INTO orders VALUES (:order_id, :customer, :date, :status, :amount)', data['orders'])
    db.executemany('INSERT INTO refunds VALUES (:order_id, :amount)', data['refunds'])
    row = db.execute("""
        SELECT COALESCE(SUM(o.amount - MIN(o.amount, COALESCE(r.total, 0))), 0)
        FROM (SELECT DISTINCT * FROM orders) o
        JOIN customers c ON c.customer = o.customer
        LEFT JOIN (SELECT order_id, SUM(amount) AS total FROM refunds GROUP BY order_id) r ON r.order_id = o.order_id
        WHERE c.region = ? AND o.status IN ('shipped', 'delivered') AND o.date BETWEEN ? AND ?
          AND c.joined <= o.date""", (data['region'], data['start'], data['end'])).fetchone()
    return row[0]


@pytest.mark.parametrize('task', tasks('G'), ids=lambda t: t['id'])
def test_g_net_revenue_matches_sqlite(task):
    data = task['source']['data']
    ids = [o['order_id'] for o in data['orders']]
    # 重複的 order_id 一定是完全相同的列，「去重」才沒有歧義。
    assert all(o == next(x for x in data['orders'] if x['order_id'] == o['order_id']) for o in data['orders'])
    assert len(ids) > len(set(ids))
    assert int(task['answer']) == g_sql(data)


# ---- H：窮舉所有字串對狀態 DP ----

@pytest.mark.parametrize('task', tasks('H', seeds=range(2)), ids=lambda t: t['id'])
def test_h_count_matches_brute_force(task):
    s = dict(task['source'])
    if len(s['alphabet']) ** s['length'] > 3 ** 12:
        # 最難一級窮舉太久：同一組限制縮短長度後窮舉，核對同一套狀態轉移。
        s['length'] = 11
        expected = h.h_count(s['alphabet'], s['length'], s['forbidden'], s['letter'], s['modulus'], s['residue'],
                             s['banned_start'])
    else:
        expected = int(task['answer'])
    total = 0
    for chars in product(s['alphabet'], repeat=s['length']):
        word = ''.join(chars)
        if s['banned_start'] and word.startswith(s['banned_start']):
            continue
        if any(f in word for f in s['forbidden']):
            continue
        total += word.count(s['letter']) % s['modulus'] == s['residue']
    assert expected == total


# ---- I：逐點判斷是否在內部對 Pick 定理 ----

def i_brute(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    edges = list(zip(points, points[1:] + points[:1]))

    def on_edge(x, y):
        return any((bx - ax) * (y - ay) == (by - ay) * (x - ax) and min(ax, bx) <= x <= max(ax, bx)
                   and min(ay, by) <= y <= max(ay, by) for (ax, ay), (bx, by) in edges)

    def inside(x, y):
        hits = 0
        for (ax, ay), (bx, by) in edges:
            if (ay > y) != (by > y):
                # 交點 x 座標 > x 時計一次；用整數比較避免浮點誤差。
                num, den = (bx - ax) * (y - ay), (by - ay)
                if (num + ax * den - x * den) * (1 if den > 0 else -1) > 0:
                    hits += 1
        return hits % 2 == 1
    return sum(1 for x in range(min(xs), max(xs) + 1) for y in range(min(ys), max(ys) + 1)
               if not on_edge(x, y) and inside(x, y))


@pytest.mark.parametrize('task', tasks('I'), ids=lambda t: t['id'])
def test_i_interior_points_match_point_by_point_check(task):
    assert int(task['answer']) == i_brute(task['source']['points']) > 0


# ---- J：另一種寫法（先算每筆、再一次分組排序）對逐步轉換 ----

def j_alternative(records, rules):
    def finish(r):
        total = max(0, r['price_cents'] * r['qty'] - r['discount'])
        tags = sorted(set(r['tags']))
        if 'half_clearance' in rules and 'clearance' in tags:
            total = total // 2
        if 'bulk_rebate' in rules and 'bulk' in tags and r['qty'] >= 10:
            total = total - total // 10
        return {'id': r['id'], 'name': '-'.join(r['name'].lower().split()), 'total': total, 'tags': tags,
                'category': r['category']}
    rows = [finish(r) for r in records if r['qty'] != 0]
    if 'drop_small' in rules:
        rows = [r for r in rows if not r['total'] < 500]
    sizes = {}
    for r in rows:
        sizes[r['category']] = sizes.get(r['category'], 0) + 1
    if 'merge_small_groups' in rules:
        for r in rows:
            if sizes[r['category']] < 2:
                r['category'] = 'other'
    out = {}
    for key in sorted({r['category'] for r in rows}):
        members = sorted((r for r in rows if r['category'] == key), key=lambda r: (-r['total'], r['id']))
        out[key] = {'count': len(members), 'sum': sum(m['total'] for m in members),
                    'items': [{k: m[k] for k in ('id', 'name', 'total', 'tags')} for m in members]}
    return out


@pytest.mark.parametrize('task', tasks('J'), ids=lambda t: t['id'])
def test_j_output_matches_an_alternative_implementation(task):
    s = task['source']
    assert task['answer'] == j_alternative(s['records'], s['rules'])
    json.dumps(task['answer'])


# ---- 題集與驗收器 ----

def test_generation_is_deterministic_and_covers_ten_families_five_levels():
    first, again = h.make('E', 3, 7), h.make('E', 3, 7)
    assert first == again and h.make('E', 3, 8)['prompt'] != first['prompt']
    pool = [h.make(f, level, 0) for f in 'ABDEFGHIJ' for level in h.LEVELS]
    assert len({t['id'] for t in pool}) == 45
    assert all(t['prompt'].rstrip().endswith('with nothing after it.') for t in pool)
    # 標準答案不會出現在題目最後的作答說明裡。
    assert all('ANSWER: ' + str(t['answer']) not in t['prompt'] for t in pool if t['kind'] != 'json')


@pytest.mark.parametrize('kind,answer,reply,passed,code', [
    ('integer', '1234', 'work...\nANSWER: 1,234', True, 'answer_correct'),
    # 答案行後面多寫一行也照收：比的是推理對錯，不是格式是否完全照做。
    ('integer', '1234', 'ANSWER: 1234\nthanks', True, 'answer_correct'),
    ('integer', '1234', 'no final line', False, 'answer_line_missing'),
    ('integer', '1234', 'ANSWER: about 1200', False, 'answer_format_invalid'),
    ('integer', '1234', '**ANSWER: 1235**', False, 'answer_incorrect'),
    ('fraction', '3/8', 'ANSWER: 6/16', True, 'answer_correct'),
    ('bits', '0101', 'ANSWER: `0101`', True, 'answer_correct'),
    ('tokens', '12 345', 'ANSWER: 12 345', True, 'answer_correct'),
    ('tokens', '12 345', 'ANSWER: 12, 345', False, 'answer_incorrect'),
    ('houses', {'names': ['Ada', 'Ben'], 'colors': ['red', 'blue'], 'pets': ['cat', 'dog']},
     'ANSWER: names=ada,Ben; colors=red, blue; pets=cat,dog', True, 'answer_correct'),
    ('json', {'a': {'count': 1}}, 'ANSWER: {"a": {"count": 1}}', True, 'answer_correct'),
    ('json', {'a': {'count': 1}}, 'ANSWER: {"a": {"count": 2}}', False, 'answer_incorrect'),
])
def test_grader(kind, answer, reply, passed, code):
    assert h.grade({'kind': kind, 'answer': answer}, reply) == (passed, code)


def test_environment_reports_only_codes():
    task = h.make('A', 1, 0)
    env = h.HardTaskEnvironment(task)
    assert env.tools_schema() == []
    assert env.validate('ANSWER: ' + task['answer']) == {'passed': True, 'checks': []}
    wrong = env.validate('ANSWER: 1')
    assert wrong == {'passed': False, 'checks': ['answer_incorrect']} and task['answer'] not in json.dumps(wrong)


def row(task_id, first_pass, *, rejected=False, status=None):
    family, rest = task_id[0], task_id[1:]
    level = int(rest.split('-')[0])
    out = {'id': task_id, 'family': family, 'level': level, 'first_pass': first_pass,
           'final_pass': first_pass, 'provider_rejected': rejected}
    if status:
        out = {'id': task_id, 'family': family, 'level': level, 'status': status}
    return out


def test_selection_prefers_open_model_failures_and_keeps_one_medium_task_per_family():
    rows = [row(f'A{lv}-{s}', lv <= 2) for lv in h.LEVELS for s in range(2)]
    picked = h.select(rows, per_family=5)
    assert [r['id'] for r in picked] == ['A3-0', 'A3-1', 'A4-0', 'A4-1', 'A2-0']
    assert [r['first_pass'] for r in picked] == [False, False, False, False, True]


def test_selection_fills_from_hardest_passes_and_skips_rejected_or_unfinished():
    rows = ([row(f'B{lv}-0', True) for lv in h.LEVELS] + [row('B5-1', False, rejected=True),
            row('B4-1', None, status='error'), row('B1-1', False)])
    picked = h.select(rows, per_family=5)
    assert [r['id'] for r in picked] == ['B1-1', 'B5-0', 'B4-0', 'B3-0', 'B2-0']


def test_frozen_set_rebuilds_tasks_and_hash_detects_changes():
    rows = [row(f'{f}{lv}-0', False) for f in 'AE' for lv in h.LEVELS]
    frozen = h.freeze_selection(h.select(rows, per_family=5))
    assert frozen['version'] == h.VERSION and len(frozen['tasks']) == 10
    rebuilt = h.load_selection(frozen)
    assert [t['id'] for t in rebuilt] == [t['id'] for t in frozen['tasks']]
    assert rebuilt[0]['answer'] == h.make('A', 1, 0)['answer']
    frozen['tasks'][0]['seed'] = 9
    with pytest.raises(ValueError):
        h.load_selection(frozen)
