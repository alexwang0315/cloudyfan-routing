"""#81 試跑題集：跨領域、可由機器判對錯的推理難題（十個題型 × 五個難度）。

每個題型的 ``make(rng, level)`` 回傳題目文字、標準答案與答案種類；答案由參考解題程式算出，
``tests/test_hard_tasks.py`` 再以第二種獨立方法核對。需要的資訊都在題幹裡，不提供工具。
題目以固定亂數種子產生，可重現。驗收器只回傳代碼，不洩漏標準答案。
"""
from fractions import Fraction
from itertools import permutations
import json
from math import comb, gcd
import random
import re

VERSION = 'cloudyfan-hard-tasks-v1'
LEVELS = (1, 2, 3, 4, 5)
ANSWER_RULE = ('Think carefully. When you are done, end your response with exactly one final line of the form\n'
               'ANSWER: {fmt}\nwith nothing after it.')


def _task(family, level, seed, prompt, answer, kind, fmt):
    return {'id': f'{family}{level}-{seed}', 'family': family, 'level': level, 'seed': seed,
            'prompt': prompt.rstrip() + '\n\n' + ANSWER_RULE.format(fmt=fmt),
            'answer': answer, 'kind': kind}


# ---- A：組合計數（有障礙與檢查點的格子路徑）----

def a_count(grid, diagonal):
    """參考解：分段動態規劃（起點→檢查點、檢查點→終點）。"""
    rows, cols = len(grid), len(grid[0])
    check = next((r, c) for r in range(rows) for c in range(cols) if grid[r][c] == 'C')

    def ways(src, dst):
        table = [[0] * cols for _ in range(rows)]
        table[src[0]][src[1]] = 1
        for r in range(src[0], dst[0] + 1):
            for c in range(src[1], dst[1] + 1):
                if (r, c) == src or grid[r][c] == '#':
                    continue
                total = (table[r - 1][c] if r > src[0] else 0) + (table[r][c - 1] if c > src[1] else 0)
                if diagonal and r > src[0] and c > src[1]:
                    total += table[r - 1][c - 1]
                table[r][c] = total
        return table[dst[0]][dst[1]]
    return ways((0, 0), check) * ways(check, (rows - 1, cols - 1))


def a_make(rng, level):
    diagonal = level >= 3
    while True:
        rows = 4 + 2 * level
        cols = rows + rng.randint(0, 2)
        density = 0.14 + 0.02 * level
        grid = [['#' if rng.random() < density else '.' for _ in range(cols)] for _ in range(rows)]
        grid[0][0] = grid[rows - 1][cols - 1] = '.'
        r, c = rng.randint(rows // 3, 2 * rows // 3), rng.randint(cols // 3, 2 * cols // 3)
        grid[r][c] = 'C'
        grid = [''.join(row) for row in grid]
        answer = a_count(grid, diagonal)
        if answer >= 10:
            break
    moves = 'right (r, c+1), down (r+1, c)' + (' or diagonally down-right (r+1, c+1)' if diagonal else '')
    prompt = (f'A grid has {rows} rows and {cols} columns. Row 0 is the top row, column 0 is the left column.\n'
              f'"#" is a blocked cell, "." is open, and "C" is an open checkpoint cell.\n\n' + '\n'.join(grid) +
              f'\n\nA path starts at the top-left cell (0, 0) and ends at the bottom-right cell ({rows - 1}, {cols - 1}). '
              f'Each step moves {moves}. A path may never enter a blocked cell and must pass through the '
              'checkpoint cell C. How many distinct paths are there?')
    return prompt, str(answer), 'integer', '<integer>', {'grid': grid, 'diagonal': diagonal}


# ---- B：機率（不放回抽球，精確分數）----

def b_probability(counts, draws, exact_red, min_blue, last_red):
    """參考解：組合公式；「最後一顆是紅球」依對稱性乘上 紅球數 / 抽出總數。"""
    red, blue, green = counts
    total = comb(red + blue + green, draws)
    result = Fraction(0)
    for b in range(min_blue, draws - exact_red + 1):
        g = draws - exact_red - b
        ways = comb(red, exact_red) * comb(blue, b) * comb(green, g)
        if ways:
            result += Fraction(ways, total) * (Fraction(exact_red, draws) if last_red else 1)
    return result


def b_make(rng, level):
    while True:
        red, blue, green = (rng.randint(3, 5 + 2 * level) for _ in range(3))
        draws = rng.randint(3 + level, 5 + 2 * level)
        if draws > red + blue + green:
            continue
        exact_red = rng.randint(1, min(red, draws - 1))
        min_blue = rng.randint(1, max(1, min(blue, draws - exact_red)))
        last_red = level >= 3
        answer = b_probability((red, blue, green), draws, exact_red, min_blue, last_red)
        if 0 < answer < 1:
            break
    extra = ' and the last ball drawn is red' if last_red else ''
    prompt = (f'A bag contains {red} red, {blue} blue and {green} green balls. Balls of the same color are '
              f'indistinguishable in color but every ball is equally likely to be drawn. You draw {draws} balls one '
              f'at a time, uniformly at random, without replacement.\n\nWhat is the probability that exactly '
              f'{exact_red} of the drawn balls are red, at least {min_blue} are blue{extra}? '
              'Give the exact probability as a reduced fraction p/q.')
    return (prompt, f'{answer.numerator}/{answer.denominator}', 'fraction', '<p>/<q>',
            {'counts': (red, blue, green), 'draws': draws, 'exact_red': exact_red, 'min_blue': min_blue,
             'last_red': last_red})


# ---- C：約束滿足（房屋邏輯謎題）----

C_NAMES = ['Ada', 'Ben', 'Cleo', 'Dev', 'Eli', 'Fay']
C_COLORS = ['red', 'blue', 'green', 'white', 'black', 'gold']
C_PETS = ['cat', 'dog', 'fish', 'bird', 'horse', 'snake']
C_ATTRS = ('names', 'colors', 'pets')


def c_holds(clue, pos):
    kind = clue[0]
    if kind == 'at':
        return pos[clue[1]] == clue[2]
    if kind == 'not_at':
        return pos[clue[1]] != clue[2]
    a, b = pos[clue[1]], pos[clue[2]]
    return {'same': a == b, 'left_of': a + 1 == b, 'next_to': abs(a - b) == 1, 'before': a < b}[kind]


def _c_items(clue):
    return [clue[1]] if clue[0] in ('at', 'not_at') else [clue[1], clue[2]]


def c_solutions(houses, clues, limit=2):
    """參考解：逐屬性回溯，每加一個屬性就檢查只涉及已指定屬性的線索。"""
    values = [C_NAMES[:houses], C_COLORS[:houses], C_PETS[:houses]]
    found = []

    def search(depth, pos):
        if len(found) >= limit:
            return
        if depth == 3:
            found.append(dict(pos))
            return
        for perm in permutations(range(houses)):
            trial = dict(pos)
            trial.update({(depth, v): p for v, p in zip(values[depth], perm)})
            if all(c_holds(cl, trial) for cl in clues
                   if all(item[0] <= depth for item in _c_items(cl))):
                search(depth + 1, trial)
    search(0, {})
    return found


def c_assignment(houses, pos):
    values = [C_NAMES[:houses], C_COLORS[:houses], C_PETS[:houses]]
    return {attr: [next(v for v in values[i] if pos[(i, v)] == h) for h in range(houses)]
            for i, attr in enumerate(C_ATTRS)}


def c_text(clue):
    def name(item):
        attr, value = item
        return {0: value, 1: f'the {value} house', 2: f'the {value} owner'}[attr]
    kind = clue[0]
    if kind == 'at':
        return f'{name(clue[1])} is in house {clue[2] + 1}.'
    if kind == 'not_at':
        return f'{name(clue[1])} is not in house {clue[2] + 1}.'
    a, b = name(clue[1]), name(clue[2])
    return {'same': f'{a} and {b} are in the same house.',
            'left_of': f'{a} is immediately to the left of {b}.',
            'next_to': f'{a} and {b} are in adjacent houses.',
            'before': f'{a} is somewhere to the left of {b}.'}[kind]


def c_make(rng, level):
    houses = 4 if level <= 2 else 5
    values = [C_NAMES[:houses], C_COLORS[:houses], C_PETS[:houses]]
    pos = {}
    for i in range(3):
        for v, p in zip(values[i], rng.sample(range(houses), houses)):
            pos[(i, v)] = p
    items = list(pos)
    direct = max(0.05, 0.35 - 0.07 * level)
    clues = []
    while True:
        roll = rng.random()
        a = rng.choice(items)
        if roll < direct:
            clue = ('at', a, pos[a])
        elif roll < direct + 0.15:
            clue = ('not_at', a, rng.choice([h for h in range(houses) if h != pos[a]]))
        else:
            b = rng.choice([i for i in items if i[0] != a[0]] if rng.random() < 0.4 else items)
            if a == b:
                continue
            kinds = [k for k in ('same', 'left_of', 'next_to', 'before') if c_holds((k, a, b), pos)]
            if not kinds:
                continue
            clue = (rng.choice(kinds), a, b)
        if clue in clues:
            continue
        clues.append(clue)
        if len(c_solutions(houses, clues)) == 1:
            break
    # 去掉多餘的線索（拿掉後仍唯一就拿掉），讓題目更需要推理。
    for clue in list(clues):
        trial = [c for c in clues if c != clue]
        if len(c_solutions(houses, trial)) == 1:
            clues = trial
    rng.shuffle(clues)
    answer = c_assignment(houses, pos)
    text = '\n'.join(f'{i + 1}. {c_text(c)[0].upper()}{c_text(c)[1:]}' for i, c in enumerate(clues))
    prompt = (f'There are {houses} houses in a row, numbered 1 to {houses} from left to right. Each house has exactly '
              f'one owner, one color and one pet, and no two houses share any of them.\n'
              f'Owners: {", ".join(values[0])}. Colors: {", ".join(values[1])}. Pets: {", ".join(values[2])}.\n'
              '"Immediately to the left" means the house number is exactly one smaller.\n\nClues:\n' + text +
              '\n\nThe clues have exactly one solution. Give the owners, colors and pets in house order.')
    fmt = 'names=<owner1>,<owner2>,...; colors=<color1>,...; pets=<pet1>,...'
    return prompt, answer, 'houses', fmt, {'houses': houses, 'clues': clues}


# ---- D：有限制的最短路徑（成本最小、總時間不超過上限）----

def d_best(nodes, edges, budget):
    """參考解：依拓撲順序對 (節點, 已用時間) 做動態規劃。"""
    best = {(0, 0): 0}
    for node in range(nodes):
        for (u, v, cost, time) in edges:
            if u != node:
                continue
            for (n, used), value in list(best.items()):
                if n == u and used + time <= budget:
                    key = (v, used + time)
                    if value + cost < best.get(key, 10 ** 18):
                        best[key] = value + cost
    finals = [value for (n, _), value in best.items() if n == nodes - 1]
    return min(finals) if finals else None


def _d_unconstrained(nodes, edges):
    dist = {0: (0, 0)}
    for node in range(nodes):
        for (u, v, cost, time) in edges:
            if u == node and u in dist:
                c, t = dist[u][0] + cost, dist[u][1] + time
                if v not in dist or c < dist[v][0]:
                    dist[v] = (c, t)
    return dist.get(nodes - 1)


def d_make(rng, level):
    names = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    while True:
        nodes = 6 + 2 * level
        edges = []
        for u in range(nodes - 1):
            targets = rng.sample(range(u + 1, nodes), min(nodes - u - 1, rng.randint(2, 3)))
            for v in targets:
                edges.append((u, v, rng.randint(1, 20), rng.randint(1, 15)))
        free = _d_unconstrained(nodes, edges)
        if free is None:
            continue
        budget = free[1] - rng.randint(2, 6)
        answer = d_best(nodes, edges, budget)
        if budget > 0 and answer is not None and answer > free[0]:
            break
    rng.shuffle(edges)
    lines = '\n'.join(f'{names[u]} -> {names[v]}: cost {c}, time {t}' for u, v, c, t in edges)
    prompt = (f'A directed road network has {nodes} towns labelled {names[0]} to {names[nodes - 1]}. '
              f'Each road can only be driven in the given direction:\n\n{lines}\n\n'
              f'Find a route from {names[0]} to {names[nodes - 1]} whose total time is at most {budget} and whose '
              'total cost is as small as possible. What is that minimum total cost?')
    return prompt, str(answer), 'integer', '<integer>', {'nodes': nodes, 'edges': edges, 'budget': budget}


# ---- E：程式追蹤 ----

E_OPS = ('+', '-', '*', '^')


def e_code(p):
    lines = [f'x = {p["x"]}', f'acc = {p["acc"]}',
             f'for i in range({p["n"]}):',
             f'    j = (i * {p["q"]} + {p["r"]}) % len(x)',
             f'    if (x[j] {p["ops"][0]} acc) % {p["d"]} == {p["e"]}:',
             f'        acc = (acc {p["ops"][1]} x[j]) % {p["m"]}',
             f'        x[j] = (x[j] {p["ops"][2]} i) % {p["m"]}',
             '    else:',
             '        k = (j + 1) % len(x)',
             f'        x[k] = (x[k] {p["ops"][3]} acc) % {p["m"]}',
             f'        acc = (acc + {p["step"]}) % {p["m"]}']
    if p['second']:
        # acc > floor >= 3 時，acc // 3 + 1 一定比 acc 小，迴圈必定結束。
        lines += ['count = 0', f'while acc > {p["floor"]}:', '    acc = acc // 3 + x[count % len(x)] % 2',
                  '    count = count + 1']
    lines.append('print(acc, sum(x))')
    return '\n'.join(lines)


def e_run(code):
    """參考解：在沒有內建函式（只留 range、len、sum）的命名空間執行，攔下 print 的輸出。"""
    out = []
    env = {'__builtins__': {'range': range, 'len': len, 'sum': sum,
                            'print': lambda *a: out.append(' '.join(str(v) for v in a))}}
    exec(compile(code, '<hard-task>', 'exec'), env)  # noqa: S102 - 程式由本模組產生，不含外部輸入
    return out[-1]


def e_make(rng, level):
    p = {'x': [rng.randint(0, 30) for _ in range(3 + level)], 'acc': rng.randint(0, 20),
         'n': 6 + 6 * level, 'q': rng.randint(2, 7), 'r': rng.randint(0, 5),
         'ops': [rng.choice(E_OPS) for _ in range(4)], 'd': rng.randint(2, 4), 'e': 0,
         'm': rng.choice([37, 41, 53, 61, 97]), 'step': rng.randint(1, 9),
         'second': level >= 4, 'floor': rng.randint(3, 9)}
    code = e_code(p)
    answer = e_run(code)
    prompt = ('What does this Python 3 program print? Note: ^ is bitwise XOR, % always returns a non-negative '
              'result for a positive modulus, and // is floor division.\n\n```python\n' + code + '\n```')
    return prompt, answer, 'tokens', '<first number> <second number>', {'params': p}


# ---- F：狀態機長鏈模擬（一維細胞自動機）----

F_RULES = (30, 45, 73, 86, 90, 105, 110, 124, 150, 169)


def f_run(rule, cells, steps):
    """參考解：以字串逐格套規則（左右循環）。"""
    width = len(cells)
    for _ in range(steps):
        cells = ''.join(str((rule >> int(cells[(i - 1) % width] + cells[i] + cells[(i + 1) % width], 2)) & 1)
                        for i in range(width))
    return cells


def f_make(rng, level):
    rule = rng.choice(F_RULES)
    width = 10 + 4 * level
    steps = 4 + 6 * level
    cells = ''.join(rng.choice('01') for _ in range(width))
    table = '\n'.join(f'  {format(n, "03b")} -> {(rule >> n) & 1}' for n in range(7, -1, -1))
    prompt = (f'A row of {width} cells, each 0 or 1, wraps around: the left neighbour of the first cell is the last '
              'cell and the right neighbour of the last cell is the first cell. In every step all cells update '
              'at the same time; the new value of a cell depends on (left neighbour, cell, right neighbour) as '
              f'follows:\n{table}\n\nThe starting row is:\n{cells}\n\nWhat is the row after exactly {steps} steps?')
    return prompt, f_run(rule, cells, steps), 'bits', f'<{width} digits of 0 and 1, no spaces>', \
        {'rule': rule, 'cells': cells, 'steps': steps}


# ---- G：表格資料推理（純文字表格，不用 SQL）----

def g_answer(data):
    """參考解：逐列處理。"""
    customers = {c['customer']: c for c in data['customers']}
    orders = {}
    for row in data['orders']:
        orders.setdefault(row['order_id'], row)  # 完全相同的重複列只算一次
    refunds = {}
    for row in data['refunds']:
        refunds[row['order_id']] = refunds.get(row['order_id'], 0) + row['amount']
    total = 0
    for order in orders.values():
        customer = customers[order['customer']]
        if (customer['region'] == data['region'] and order['status'] in ('shipped', 'delivered')
                and data['start'] <= order['date'] <= data['end'] and customer['joined'] <= order['date']):
            total += order['amount'] - min(order['amount'], refunds.get(order['order_id'], 0))
    return total


def g_make(rng, level):
    regions = ['North', 'South', 'East', 'West']
    statuses = ['shipped', 'delivered', 'cancelled', 'pending']
    def date(day):
        return f'2026-{1 + day // 28:02d}-{1 + day % 28:02d}'
    customers = [{'customer': f'C{i:02d}', 'region': rng.choice(regions), 'joined': date(rng.randint(0, 120))}
                 for i in range(5 + 2 * level)]
    orders = []
    for i in range(10 + 8 * level):
        orders.append({'order_id': f'O{100 + i}', 'customer': rng.choice(customers)['customer'],
                       'date': date(rng.randint(0, 160)), 'status': rng.choice(statuses),
                       'amount': rng.randint(5, 400)})
    for _ in range(1 + level):
        orders.insert(rng.randint(0, len(orders)), dict(rng.choice(orders)))
    refunds = [{'order_id': rng.choice(orders)['order_id'], 'amount': rng.randint(1, 250)}
               for _ in range(3 + 2 * level)]
    start, end = sorted(rng.sample(range(20, 150), 2))
    data = {'customers': customers, 'orders': orders, 'refunds': refunds, 'region': rng.choice(regions),
            'start': date(start), 'end': date(end)}
    def table(rows, fields):
        return '\n'.join([' | '.join(fields)] + [' | '.join(str(r[f]) for f in fields) for r in rows])
    prompt = ('Three tables follow.\n\ncustomers\n' + table(customers, ['customer', 'region', 'joined']) +
              '\n\norders\n' + table(orders, ['order_id', 'customer', 'date', 'status', 'amount']) +
              '\n\nrefunds\n' + table(refunds, ['order_id', 'amount']) +
              f'\n\nCompute the net revenue for region {data["region"]}, using these rules:\n'
              '1. Some rows in orders are exact duplicates; count each order_id only once.\n'
              f'2. Only orders with status shipped or delivered, dated from {data["start"]} to {data["end"]} '
              'inclusive.\n'
              '3. Only orders whose customer is in that region and had joined on or before the order date.\n'
              '4. For each counted order, subtract the sum of its refunds, but never subtract more than the '
              'order amount.\n5. Net revenue is the sum over counted orders.')
    return prompt, str(g_answer(data)), 'integer', '<integer>', {'data': data}


# ---- H：形式語言計數 ----

def h_count(alphabet, length, forbidden, letter, modulus, residue, banned_start):
    """參考解：以「最後幾個字元」與「目標字母個數的餘數」為狀態做動態規劃。"""
    keep = max(len(f) for f in forbidden) - 1
    states = {('', 0): 1}
    for position in range(length):
        nxt = {}
        for (tail, mod), ways in states.items():
            for ch in alphabet:
                if position == 0 and ch == banned_start:
                    continue
                window = tail + ch
                if any(window.endswith(f) for f in forbidden):
                    continue
                key = (window[-keep:] if keep else '', (mod + (ch == letter)) % modulus)
                nxt[key] = nxt.get(key, 0) + ways
        states = nxt
    return sum(ways for (_, mod), ways in states.items() if mod == residue)


def h_make(rng, level):
    alphabet, length = {1: ('ab', 12), 2: ('ab', 18), 3: ('abc', 10), 4: ('abc', 13), 5: ('abc', 16)}[level]
    while True:
        forbidden = sorted({''.join(rng.choice(alphabet) for _ in range(rng.randint(2, 3)))
                            for _ in range(1 + level // 2)})
        letter = rng.choice(alphabet)
        modulus = rng.randint(3, 5)
        residue = rng.randrange(modulus)
        banned_start = rng.choice(alphabet) if level >= 4 else None
        answer = h_count(alphabet, length, forbidden, letter, modulus, residue, banned_start)
        if answer > 5:
            break
    extra = f'\n- does not start with "{banned_start}"' if banned_start else ''
    prompt = (f'Count the strings of length exactly {length} over the alphabet {{{", ".join(alphabet)}}} that '
              f'satisfy all of the following:\n- contains none of these as a contiguous substring: '
              f'{", ".join(chr(34) + f + chr(34) for f in forbidden)}\n'
              f'- the number of "{letter}" characters leaves remainder {residue} when divided by {modulus}{extra}\n\n'
              'How many such strings are there?')
    return prompt, str(answer), 'integer', '<integer>', \
        {'alphabet': alphabet, 'length': length, 'forbidden': forbidden, 'letter': letter, 'modulus': modulus,
         'residue': residue, 'banned_start': banned_start}


# ---- I：格點幾何（多邊形內部格點數）----

def i_interior(points):
    """參考解：鞋帶公式求面積、gcd 求邊上格點，再用 Pick 定理。"""
    twice_area = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])))
    boundary = sum(gcd(abs(x2 - x1), abs(y2 - y1)) for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))
    return (twice_area - boundary + 2) // 2


def i_make(rng, level):
    import math
    radius = 6 + 4 * level
    count = 5 + 2 * level
    while True:
        angles = sorted(rng.uniform(0, 2 * math.pi) for _ in range(count))
        gaps = [(b - a) for a, b in zip(angles, angles[1:] + [angles[0] + 2 * math.pi])]
        if max(gaps) >= math.pi * 0.9:
            continue
        points = [(round(rng.uniform(0.4, 1) * radius * math.cos(t)), round(rng.uniform(0.4, 1) * radius * math.sin(t)))
                  for t in angles]
        if len(set(points)) != len(points):
            continue
        ordered = sorted(points, key=lambda p: math.atan2(p[1], p[0]) % (2 * math.pi))
        if ordered != points:
            continue
        # 以原點為中心依角度排序且相鄰夾角小於 180 度：簡單多邊形；再確認沒有連續三點共線退化。
        if any((b[0] - a[0]) * (c[1] - a[1]) == (b[1] - a[1]) * (c[0] - a[0])
               for a, b, c in zip(points, points[1:] + points[:1], points[2:] + points[:2])):
            continue
        if all(x * y2 - x2 * y > 0 for (x, y), (x2, y2) in zip(points, points[1:] + points[:1])):
            break
    text = ', '.join(f'({x}, {y})' for x, y in points)
    prompt = (f'A simple polygon has these {count} vertices, listed in order around the boundary (the last vertex '
              f'connects back to the first):\n{text}\n\nHow many points with integer coordinates lie strictly inside '
              'the polygon (not on its boundary)?')
    return prompt, str(i_interior(points)), 'integer', '<integer>', {'points': points}


# ---- J：依規格的結構轉換 ----

J_CATEGORIES = ['tools', 'garden', 'toys', 'books', 'audio']
J_TAGS = ['new', 'sale', 'clearance', 'gift', 'eco', 'bulk']


def j_transform(records, rules):
    """參考解：依規則順序逐步轉換。"""
    rows = [dict(r, tags=list(r['tags'])) for r in records]
    rows = [r for r in rows if r['qty'] > 0]
    for r in rows:
        r['price'] = r.pop('price_cents')
        r['name'] = '-'.join(r['name'].strip().lower().split())
        r['tags'] = sorted(set(r['tags']))
        r['total'] = max(0, r['price'] * r['qty'] - r['discount'])
        if 'clearance' in r['tags'] and 'half_clearance' in rules:
            r['total'] //= 2
        if 'bulk' in r['tags'] and r['qty'] >= 10 and 'bulk_rebate' in rules:
            r['total'] -= r['total'] // 10
    if 'drop_small' in rules:
        rows = [r for r in rows if r['total'] >= 500]
    groups = {}
    for r in rows:
        groups.setdefault(r['category'], []).append(r)
    if 'merge_small_groups' in rules:
        small = [k for k, v in groups.items() if len(v) < 2]
        for k in small:
            groups.setdefault('other', []).extend(groups.pop(k))
    result = {}
    for category in sorted(groups):
        items = sorted(groups[category], key=lambda r: (-r['total'], r['id']))
        result[category] = {'count': len(items), 'sum': sum(r['total'] for r in items),
                            'items': [{'id': r['id'], 'name': r['name'], 'total': r['total'], 'tags': r['tags']}
                                      for r in items]}
    return result


J_RULE_TEXT = {
    'half_clearance': 'If the record has the tag "clearance", halve total, rounding down.',
    'bulk_rebate': 'Then, if the record has the tag "bulk" and qty is at least 10, subtract floor(total / 10) from total.',
    'drop_small': 'After computing totals, remove records whose total is less than 500.',
    'merge_small_groups': 'Any category that ends up with fewer than 2 records is merged into a category named '
                          '"other" (the sorting rules below still apply inside "other").',
}


def j_make(rng, level):
    rules = [r for r, lv in (('half_clearance', 1), ('bulk_rebate', 2), ('drop_small', 3),
                             ('merge_small_groups', 4)) if level >= lv]
    words = ['red', 'big', 'Mini', 'steel', 'Lamp', 'garden hose', 'robot', 'Chess', 'drum', 'novel', 'Kite']
    records = []
    for i in range(5 + 3 * level):
        name = ' ' * rng.randint(0, 2) + '  '.join(rng.sample(words, rng.randint(1, 3))) + ' ' * rng.randint(0, 2)
        records.append({'id': 10 + i, 'name': name, 'category': rng.choice(J_CATEGORIES),
                        'price_cents': rng.randint(50, 900), 'qty': rng.choice([0, 1, 2, 3, 5, 10, 12]),
                        'discount': rng.choice([0, 0, 25, 100, 300]),
                        'tags': rng.choices(J_TAGS, k=rng.randint(0, 4))})
    steps = ['Remove records whose qty is 0.', 'Rename price_cents to price.',
             'name: trim leading and trailing spaces, lowercase it, and replace every run of spaces with a single "-".',
             'tags: remove duplicates and sort alphabetically.',
             'total = price * qty - discount, but never below 0.']
    steps += [J_RULE_TEXT[r] for r in rules if r in ('half_clearance', 'bulk_rebate', 'drop_small')]
    steps += ['Group records by category.'] + ([J_RULE_TEXT['merge_small_groups']] if 'merge_small_groups' in rules
                                                else [])
    steps += ['Output one JSON object whose keys are the category names in alphabetical order. Each value is an '
              'object {"count": number of records, "sum": sum of their totals, "items": [...]}.',
              'items are sorted by total descending, ties by id ascending; each item is exactly '
              '{"id", "name", "total", "tags"} (no other fields).']
    prompt = ('Transform the input records by applying these rules in order:\n' +
              '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(steps)) +
              '\n\nInput (JSON):\n' + json.dumps(records) +
              '\n\nGive the output JSON on a single line.')
    return prompt, j_transform(records, rules), 'json', '<the JSON object on one line>', \
        {'records': records, 'rules': rules}


FAMILIES = {'A': a_make, 'B': b_make, 'C': c_make, 'D': d_make, 'E': e_make,
            'F': f_make, 'G': g_make, 'H': h_make, 'I': i_make, 'J': j_make}
FAMILY_NAMES = {'A': 'combinatorics', 'B': 'probability', 'C': 'constraint_puzzle', 'D': 'constrained_shortest_path',
                'E': 'program_trace', 'F': 'cellular_automaton', 'G': 'table_reasoning', 'H': 'formal_language',
                'I': 'lattice_geometry', 'J': 'spec_transformation'}


def make(family, level, seed):
    """產生一題；同樣的 (題型, 難度, 種子) 一定得到同一題。"""
    rng = random.Random(f'{VERSION}:{family}:{level}:{seed}')
    prompt, answer, kind, fmt, source = FAMILIES[family](rng, level)
    task = _task(family, level, seed, prompt, answer, kind, fmt)
    task['source'] = source
    return task


def candidates(seeds_per_level=2, first_seed=0):
    """校準用候選題：每個題型、每個難度各 ``seeds_per_level`` 題。"""
    return [make(f, level, first_seed + s) for f in FAMILIES for level in LEVELS for s in range(seeds_per_level)]


# ---- 驗收 ----

def extract_answer(text):
    if not isinstance(text, str):
        return None
    for line in reversed(text.strip().splitlines()):
        match = re.match(r'^\s*\**\s*ANSWER\s*:\s*(.*?)\s*\**\s*$', line)
        if match:
            return match.group(1).strip().strip('`').strip()
    return None


def _normalize(kind, raw):
    if kind == 'integer':
        cleaned = raw.replace(',', '').replace(' ', '')
        return int(cleaned) if re.fullmatch(r'-?\d+', cleaned) else None
    if kind == 'fraction':
        match = re.fullmatch(r'\s*(-?\d+)\s*/\s*(\d+)\s*', raw) or re.fullmatch(r'\s*(-?\d+)\s*', raw)
        if not match:
            return None
        return Fraction(int(match.group(1)), int(match.group(2)) if match.lastindex == 2 else 1)
    if kind == 'bits':
        cleaned = raw.replace(' ', '')
        return cleaned if re.fullmatch(r'[01]+', cleaned) else None
    if kind == 'tokens':
        return raw.split()
    if kind == 'houses':
        parts = {}
        for chunk in raw.split(';'):
            key, sep, value = chunk.partition('=')
            if not sep:
                return None
            parts[key.strip().lower()] = [v.strip().lower() for v in value.split(',')]
        return parts if set(parts) == set(C_ATTRS) else None
    if kind == 'json':
        try:
            return json.loads(raw)
        except ValueError:
            return None
    raise ValueError('Unknown answer kind')


def grade(task, text):
    """回傳 (是否通過, 公開檢查碼)。檢查碼不含標準答案。"""
    raw = extract_answer(text)
    if raw is None:
        return False, 'answer_line_missing'
    given = _normalize(task['kind'], raw)
    if given is None:
        return False, 'answer_format_invalid'
    expected = task['answer']
    if task['kind'] == 'houses':
        expected = {k: [v.lower() for v in vs] for k, vs in expected.items()}
    elif task['kind'] in ('integer', 'fraction', 'bits', 'tokens'):
        expected = _normalize(task['kind'], expected)
    return (given == expected), ('answer_correct' if given == expected else 'answer_incorrect')


class HardTaskEnvironment:
    """接上 Core 的公開驗收：沒有工具；驗收只回傳通過與否與檢查碼。"""

    def __init__(self, task):
        self.task = task

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        raise ValueError('This task has no tools')

    def validate(self, final_text):
        passed, code = grade(self.task, final_text)
        return {'passed': passed, 'checks': [] if passed else [code]}
