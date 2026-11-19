"""#81 試跑題集：跨領域、可由機器判對錯的推理難題（十個題型 × 五個難度）。

每個題型的 ``make(rng, level)`` 回傳題目文字、標準答案與答案種類；答案由參考解題程式算出，
``tests/test_hard_tasks.py`` 再以第二種獨立方法核對。需要的資訊都在題幹裡，不提供工具。
題目以固定亂數種子產生，可重現。驗收器只回傳代碼，不洩漏標準答案。
"""
from fractions import Fraction
from itertools import permutations
import json
from math import gcd
import random
import re

VERSION = 'cloudyfan-hard-tasks-v4'
# 各題型的產生器版本：第 1 次校準後 B、C、D、G、I 太容易而改寫；第 2 次校準後 D、G 再改寫；第 3 次校準後 D 換成新題型；其餘沿用第 1 版，
# 亂數種子也沿用第 1 版的命名，已校準的題目不變。
FAMILY_REVISION = {'A': 1, 'B': 2, 'C': 2, 'D': 4, 'E': 1, 'F': 1, 'G': 3, 'H': 1, 'I': 2, 'J': 1}
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


# ---- B（第 2 版）：機率（有梯子與蛇的棋盤、限定回合數，精確分數）----
# 第 1 版（不放回抽球）有單一公式可套，開源模型全對（校準 2026-11-19），已改為需要逐回合推算的過程。

def b_probability(board, die, turns, jumps):
    """參考解：逐回合追蹤每個位置的機率；到達終點即停止並累計。"""
    dist, reached = {0: Fraction(1)}, Fraction(0)
    for _ in range(turns):
        nxt = {}
        for square, prob in dist.items():
            for roll in range(1, die + 1):
                target = square + roll
                if target > board:
                    target = board - (target - board)
                target = jumps.get(target, target)
                if target == board:
                    reached += prob / die
                else:
                    nxt[target] = nxt.get(target, 0) + prob / die
        dist = nxt
    return reached


def b_make(rng, level):
    board, die, turns, count = {1: (10, 3, 5, 2), 2: (12, 3, 6, 3), 3: (14, 4, 6, 3), 4: (16, 4, 7, 4),
                                5: (18, 4, 8, 5)}[level]
    while True:
        squares = rng.sample(range(2, board - 1), 2 * count)
        jumps, targets = {}, set()
        for start in squares[:count]:
            options = [t for t in range(1, board) if t != start and t not in squares[:count] and t not in targets]
            target = rng.choice(options)
            jumps[start] = target
            targets.add(target)
        answer = b_probability(board, die, turns, jumps)
        if Fraction(1, 20) < answer < Fraction(19, 20):
            break
    lines = '\n'.join(f'- landing on square {s} moves the token to square {t} '
                      f'({"ladder up" if t > s else "snake down"})' for s, t in sorted(jumps.items()))
    prompt = (f'A board has squares 0 to {board}. A token starts on square 0. Each turn you roll a fair die with faces '
              f'1 to {die} and move the token forward by the roll.\n'
              f'- If the move would go past square {board}, the token bounces back: it walks forward to square {board} '
              'and then backward for the remaining steps.\n'
              f'{lines}\n'
              '- A jump happens at most once per turn, after the move (a jump destination is never itself a jump square).\n'
              f'- As soon as the token is on square {board} the game stops.\n\n'
              f'What is the probability that the token reaches square {board} within at most {turns} turns? '
              'Give the exact probability as a reduced fraction p/q.')
    return (prompt, f'{answer.numerator}/{answer.denominator}', 'fraction', '<p>/<q>',
            {'board': board, 'die': die, 'turns': turns, 'jumps': jumps})


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


def c_csp(houses, clues, limit=2):
    """生成用求解器：以「每個項目放哪一間」為變數的回溯（同屬性不可重複），每放一個就檢查相關線索。
    原本逐屬性排列的 ``c_solutions`` 保留作為測試裡的獨立核對。"""
    values = [C_NAMES[:houses], C_COLORS[:houses], C_PETS[:houses]]
    items = [(i, v) for i in range(3) for v in values[i]]
    related = {item: [cl for cl in clues if item in _c_items(cl)] for item in items}
    items.sort(key=lambda item: -len(related[item]))
    pos, used, found = {}, [set(), set(), set()], []

    def search(index):
        if len(found) >= limit:
            return
        if index == len(items):
            found.append(dict(pos))
            return
        item = items[index]
        for house in range(houses):
            if house in used[item[0]]:
                continue
            pos[item] = house
            if all(c_holds(cl, pos) for cl in related[item] if all(x in pos for x in _c_items(cl))):
                used[item[0]].add(house)
                search(index + 1)
                used[item[0]].discard(house)
            del pos[item]
    search(0)
    return found


def c_make(rng, level):
    """第 2 版：第 3 級以上六間房；第 2 級以上沒有「某項在第幾間」的直接線索。"""
    houses = 5 if level <= 2 else 6
    values = [C_NAMES[:houses], C_COLORS[:houses], C_PETS[:houses]]
    pos = {}
    for i in range(3):
        for v, p in zip(values[i], rng.sample(range(houses), houses)):
            pos[(i, v)] = p
    items = list(pos)
    direct = 0.08 if level == 1 else 0
    negative = 0.18 if level <= 3 else 0.1
    clues = []
    while True:
        roll = rng.random()
        a = rng.choice(items)
        if roll < direct:
            clue = ('at', a, pos[a])
        elif roll < direct + negative:
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
        if len(c_csp(houses, clues)) == 1:
            break
    for clue in list(clues):
        trial = [c for c in clues if c != clue]
        if len(c_csp(houses, trial)) == 1:
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


# ---- D（第 4 版）：同餘方程組加大數模冪 ----
# 第 1–3 版（受限最短路徑）不論怎麼加難，開源模型都答對 9–10／10；改為需要精確長串計算的題型。

D_MODULI = (7, 8, 9, 11, 13, 17, 19, 23, 25, 27, 29, 31, 32, 37, 41, 43, 47, 49, 53, 59)
D_PRIMES = (97, 101, 211, 257, 331, 409, 557, 769, 1009, 2003, 4099, 7919, 10007, 30011, 65537, 99991)


def d_crt(residues, moduli):
    """參考解：逐條合併同餘式（模數兩兩互質），回傳最小正整數解。"""
    x, m = 0, 1
    for r, n in zip(residues, moduli):
        # 找 k 使 x + m*k ≡ r (mod n)：k = (r - x) * m^{-1} mod n
        k = ((r - x) * pow(m, -1, n)) % n
        x, m = x + m * k, m * n
    return x % m or m


def d_answer(residues, moduli, exponent, prime):
    x = d_crt(residues, moduli)
    return f'{x} {pow(x, exponent, prime)}'


def d_make(rng, level):
    count, limit, exp_range, primes = {1: (2, 30, (20, 60), D_PRIMES[:3]), 2: (3, 40, (100, 400), D_PRIMES[2:6]),
                                       3: (3, 60, (1000, 9000), D_PRIMES[5:10]),
                                       4: (4, 45, (10 ** 4, 9 * 10 ** 4), D_PRIMES[8:13]),
                                       5: (4, 60, (10 ** 5, 9 * 10 ** 5), D_PRIMES[11:])}[level]
    while True:
        moduli = rng.sample([m for m in D_MODULI if m <= limit], count)
        if all(gcd(a, b) == 1 for i, a in enumerate(moduli) for b in moduli[i + 1:]):
            break
    prime = rng.choice(primes)
    while True:
        residues = [rng.randrange(m) for m in moduli]
        if d_crt(residues, moduli) % prime:  # x 是質數的倍數時餘數必為 0，太簡單
            break
    exponent = rng.randint(*exp_range)
    lines = '\n'.join(f'  x mod {m} = {r}' for r, m in zip(residues, moduli))
    prompt = (f'Let x be the smallest positive integer such that\n{lines}\n\n'
              f'Then compute y = x^{exponent} mod {prime} (the remainder of x raised to the power {exponent} when '
              f'divided by {prime}). Give both x and y.')
    return prompt, d_answer(residues, moduli, exponent, prime), 'tokens', '<x> <y>', \
        {'residues': residues, 'moduli': moduli, 'exponent': exponent, 'prime': prime}


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


# ---- G（第 3 版）：表格資料推理（四個地區都要算、常客折扣需跨列統計）----
# 第 2 版（單一地區）開源模型仍全對，改為四個地區都要算對，並加上需要先跨列統計的常客折扣。

G_REGIONS = ('East', 'North', 'South', 'West')


def g_month(date):
    return date[:7]


def g_answer(data, stats=None):
    """參考解：逐列處理，回傳「地區=金額」依地區名稱排序、以空白分隔。"""
    history = {}
    for row in data['region_history']:
        history.setdefault(row['customer'], []).append(row)
    joined = {c['customer']: c['joined'] for c in data['customers']}
    rates = {r['month']: r['eur_to_usd_percent'] for r in data['rates']}
    orders = {}
    for row in data['orders']:
        orders.setdefault(row['order_id'], row)
    refunds = {}
    for row in data['refunds']:
        if row['date'] <= data['end']:
            refunds[row['order_id']] = refunds.get(row['order_id'], 0) + row['amount']
    counted = []
    for order in orders.values():
        if order['status'] not in ('shipped', 'delivered') or not data['start'] <= order['date'] <= data['end']:
            continue
        if joined[order['customer']] > order['date']:
            continue
        past = [h for h in history.get(order['customer'], []) if h['effective_from'] <= order['date']]
        if not past:
            continue
        region = max(past, key=lambda h: h['effective_from'])['region']
        net = order['amount'] - min(order['amount'], refunds.get(order['order_id'], 0))
        usd = net if order['currency'] == 'USD' else net * rates[g_month(order['date'])] // 100
        counted.append((order['customer'], region, usd))
    loyal = {c for c, _, _ in counted if sum(1 for x, _, _ in counted if x == c) >= 3}
    if stats is not None:
        stats.update(loyal=len(loyal), counted=len(counted))
    totals = {region: 0 for region in G_REGIONS}
    for customer, region, usd in counted:
        totals[region] += usd - (usd * 5 // 100 if customer in loyal else 0)
    return ' '.join(f'{region}={totals[region]}' for region in G_REGIONS)


def g_make(rng, level):
    # 至少三個地區有營收、至少一位常客折扣生效，避免「全填 0」也能矇對。
    while True:
        prompt, answer, kind, fmt, source = _g_candidate(rng, level)
        stats = {}
        g_answer(source['data'], stats)
        if sum(part.split('=')[1] != '0' for part in answer.split()) >= 3 and stats['loyal'] >= 1:
            return prompt, answer, kind, fmt, source


def _g_candidate(rng, level):
    statuses = ['shipped', 'delivered', 'cancelled', 'pending', 'returned']

    def date(day):
        return f'2026-{1 + day // 28:02d}-{1 + day % 28:02d}'
    customers = [{'customer': f'C{i:02d}', 'joined': date(rng.randint(0, 100))} for i in range(6 + 2 * level)]
    history = []
    for c in customers:
        for day in sorted(rng.sample(range(0, 160), rng.randint(1, 3))):
            history.append({'customer': c['customer'], 'region': rng.choice(G_REGIONS), 'effective_from': date(day)})
    rng.shuffle(history)
    rates = [{'month': f'2026-{m:02d}', 'eur_to_usd_percent': rng.randint(104, 118)} for m in range(1, 7)]
    orders = []
    for i in range(20 + 12 * level):
        orders.append({'order_id': f'O{100 + i}', 'customer': rng.choice(customers)['customer'],
                       'date': date(rng.randint(0, 160)), 'status': rng.choice(statuses),
                       'amount': rng.randint(5, 400), 'currency': rng.choice(['USD', 'EUR'])})
    for _ in range(2 + level):
        orders.insert(rng.randint(0, len(orders)), dict(rng.choice(orders)))
    start, end = sorted(rng.sample(range(20, 150), 2))
    if end - start < 40:
        start, end = max(0, start - 20), min(167, end + 20)
    refunds = [{'order_id': rng.choice(orders)['order_id'], 'amount': rng.randint(1, 250),
                'date': date(rng.randint(start, 167))} for _ in range(4 + 2 * level)]
    data = {'customers': customers, 'region_history': history, 'rates': rates, 'orders': orders,
            'refunds': refunds, 'start': date(start), 'end': date(end)}

    def table(rows, fields):
        return '\n'.join([' | '.join(fields)] + [' | '.join(str(r[f]) for f in fields) for r in rows])
    prompt = ('Five tables follow.\n\ncustomers\n' + table(customers, ['customer', 'joined']) +
              '\n\nregion_history (a customer belongs to the region of their latest row whose effective_from is on or '
              'before the date in question; before their first row they have no region)\n' +
              table(history, ['customer', 'region', 'effective_from']) +
              '\n\nrates (1 EUR = eur_to_usd_percent / 100 USD in that month)\n' +
              table(rates, ['month', 'eur_to_usd_percent']) +
              '\n\norders (amount is in the order currency)\n' +
              table(orders, ['order_id', 'customer', 'date', 'status', 'amount', 'currency']) +
              '\n\nrefunds (amount is in the currency of the refunded order)\n' +
              table(refunds, ['order_id', 'amount', 'date']) +
              '\n\nCompute the net revenue in USD of every region, using these rules:\n'
              '1. Some rows in orders are exact duplicates; count each order_id only once.\n'
              f'2. An order is counted only if its status is shipped or delivered, it is dated from {data["start"]} to '
              f'{data["end"]} inclusive, its customer had joined on or before the order date, and its customer had a '
              'region on the order date. The order belongs to the customer\'s region on the order date.\n'
              f'3. Ignore refunds dated after {data["end"]}. For each counted order, subtract the sum of its remaining '
              'refunds, but never subtract more than the order amount.\n'
              '4. Convert each counted order\'s net amount to USD: USD orders stay as they are; for EUR orders multiply '
              'by the rate of the order\'s month and divide by 100, rounding down to a whole number (per order).\n'
              '5. Loyalty discount: a customer with at least 3 counted orders (in any regions) is loyal. For each '
              'counted order of a loyal customer, subtract 5% of its USD amount, rounded down to a whole number '
              '(per order).\n'
              '6. A region\'s net revenue is the sum over its counted orders (0 if it has none).')
    fmt = 'East=<amount> North=<amount> South=<amount> West=<amount>'
    return prompt, g_answer(data), 'tokens', fmt, {'data': data}


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


# ---- I（第 2 版）：多邊形內部、滿足餘數條件的格點數 ----
# 第 1 版只問內部格點總數，用 Pick 定理就能算，開源模型全對；改問 Pick 定理算不出的量。

def i_points_inside(points):
    """內部格點（不含邊上）：逐點以射線交叉法判斷，全部用整數運算。"""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    edges = list(zip(points, points[1:] + points[:1]))

    def on_edge(x, y):
        return any((bx - ax) * (y - ay) == (by - ay) * (x - ax) and min(ax, bx) <= x <= max(ax, bx)
                   and min(ay, by) <= y <= max(ay, by) for (ax, ay), (bx, by) in edges)

    def inside(x, y):
        hits = 0
        for (ax, ay), (bx, by) in edges:
            if (ay > y) != (by > y):
                den = by - ay
                if ((bx - ax) * (y - ay) + (ax - x) * den) * (1 if den > 0 else -1) > 0:
                    hits += 1
        return hits % 2 == 1
    return [(x, y) for x in range(min(xs), max(xs) + 1) for y in range(min(ys), max(ys) + 1)
            if not on_edge(x, y) and inside(x, y)]


def i_count(points, a, b, modulus, residue):
    return sum(1 for x, y in i_points_inside(points) if (a * x + b * y) % modulus == residue)


def i_make(rng, level):
    import math
    radius = 6 + 3 * level
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
        if sorted(points, key=lambda p: math.atan2(p[1], p[0]) % (2 * math.pi)) != points:
            continue
        if any((b[0] - a[0]) * (c[1] - a[1]) == (b[1] - a[1]) * (c[0] - a[0])
               for a, b, c in zip(points, points[1:] + points[:1], points[2:] + points[:2])):
            continue
        if all(x * y2 - x2 * y > 0 for (x, y), (x2, y2) in zip(points, points[1:] + points[:1])):
            break
    a, b = rng.randint(1, 4), rng.randint(1, 4)
    modulus = rng.randint(3, 5)
    residue = rng.randrange(modulus)
    text = ', '.join(f'({x}, {y})' for x, y in points)
    prompt = (f'A simple polygon has these {count} vertices, listed in order around the boundary (the last vertex '
              f'connects back to the first):\n{text}\n\nConsider the points (x, y) with integer coordinates that lie '
              'strictly inside the polygon (not on its boundary). How many of them satisfy '
              f'({a}*x + {b}*y) mod {modulus} = {residue}? (mod gives a result from 0 to {modulus - 1}, also for '
              'negative numbers.)')
    return prompt, str(i_count(points, a, b, modulus, residue)), 'integer', '<integer>', \
        {'points': points, 'a': a, 'b': b, 'modulus': modulus, 'residue': residue}


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
FAMILY_NAMES = {'A': 'combinatorics', 'B': 'probability', 'C': 'constraint_puzzle', 'D': 'modular_arithmetic',
                'E': 'program_trace', 'F': 'cellular_automaton', 'G': 'table_reasoning', 'H': 'formal_language',
                'I': 'lattice_geometry', 'J': 'spec_transformation'}


def make(family, level, seed):
    """產生一題；同樣的 (題型, 難度, 種子) 一定得到同一題。"""
    rng = random.Random(f'cloudyfan-hard-tasks-v{FAMILY_REVISION[family]}:{family}:{level}:{seed}')
    prompt, answer, kind, fmt, source = FAMILIES[family](rng, level)
    task = _task(family, level, seed, prompt, answer, kind, fmt)
    task['revision'] = FAMILY_REVISION[family]
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


# ---- 選題與凍結 ----

def select(rows, *, per_family=5, failures_per_family=None):
    """依校準結果選題：每個題型以「首答答錯」為主（依難度由低到高），保留一題「首答答對、難度最高」
    的中等題；答錯的題不夠時，由首答答對的題依難度由高到低補滿。被供應商拒絕或沒跑完的題不選。"""
    failures_per_family = per_family - 1 if failures_per_family is None else failures_per_family
    usable = [r for r in rows if r.get('first_pass') in (True, False) and not r.get('provider_rejected')]
    picked = []
    for family in FAMILIES:
        mine = [r for r in usable if r['family'] == family]
        fails = sorted((r for r in mine if r['first_pass'] is False), key=lambda r: (r['level'], r['id']))
        passes = sorted((r for r in mine if r['first_pass'] is True), key=lambda r: (-r['level'], r['id']))
        chosen = fails[:failures_per_family]
        for r in passes + fails[failures_per_family:]:
            if len(chosen) >= per_family:
                break
            chosen.append(r)
        picked += chosen
    return picked


def freeze_selection(rows):
    """凍結選出的題目：只存 (題型, 難度, 種子)，題目與答案由產生器重建，並以雜湊核對。"""
    import hashlib
    tasks = [{'id': r['id'], 'family': r['family'], 'level': r['level'],
              'seed': int(r['id'].rsplit('-', 1)[1]), 'calibration_first_pass': r['first_pass']} for r in rows]
    digest = hashlib.sha256(json.dumps([make(t['family'], t['level'], t['seed'])['prompt'] for t in tasks],
                                       ensure_ascii=False).encode()).hexdigest()
    return {'version': VERSION, 'tasks': tasks, 'prompts_sha256': digest}


def load_selection(frozen):
    import hashlib
    if frozen.get('version') != VERSION:
        raise ValueError('Task set version changed')
    tasks = [make(t['family'], t['level'], t['seed']) for t in frozen['tasks']]
    if any(t['id'] != f['id'] for t, f in zip(tasks, frozen['tasks'])):
        raise ValueError('Frozen task identity changed')
    digest = hashlib.sha256(json.dumps([t['prompt'] for t in tasks], ensure_ascii=False).encode()).hexdigest()
    if digest != frozen['prompts_sha256']:
        raise ValueError('Frozen task prompts changed')
    return tasks
