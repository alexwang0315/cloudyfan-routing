"""第二版盲測程式題（I～P，演算法陷阱）：獨立核對期望值、錯誤解會中招、參考解夠快。全部離線。

核對方式（全部寫在這個檔案，和 code_tasks 的參考解不同）：
- 小輸入：窮舉或直接定義的暴力法（BRUTE）。
- 全部輸入（含大輸入）：第二種正確演算法（ALT），例如另一種流網路寫法、Pareto 階梯、
  分塊重建並查集、門檻分解加線段樹、雙佇列 Huffman。
錯誤解：wrong 型在行程內確認「公開範例全對、隱藏小測試至少錯 TRAP_COVERAGE 題」；
slow 型在隔離環境確認大輸入逾時（或遞迴過深）。
"""
from bisect import bisect_left, insort
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import heapq
from itertools import combinations
import json

import pytest

from cloudyfan_jev import code_sandbox as sandbox
from cloudyfan_jev import code_tasks as ct

TRUSTED_SANDBOX = sandbox.sandbox_available()
needs_sandbox = pytest.mark.skipif(not TRUSTED_SANDBOX, reason='sandbox-exec is only available on macOS')
KEYS = [(f, level, 0) for f in ct.TRAP_FAMILIES for level in ct.LEVELS]
MOD = 1_000_000_007


def ids(key):
    return f'{key[0]}{key[1]}-{key[2]}'


@lru_cache(maxsize=None)
def built(family, level, seed):
    return ct.build(family, level, seed)


def args_of(test):
    return json.loads(json.dumps(test['args']))


# ---- I：多間會議室 ----

def i_brute(jobs, rooms):
    if len(jobs) > 12:
        return None
    best = 0
    for mask in range(1 << len(jobs)):
        chosen = [jobs[i] for i in range(len(jobs)) if mask >> i & 1]
        depth = peak = 0
        for _, d in sorted([(s, 1) for s, _, _ in chosen] + [(e, -1) for _, e, _ in chosen]):
            depth += d
            peak = max(peak, depth)
        if peak <= rooms:
            best = max(best, sum(p for _, _, p in chosen))
    return best


def i_alt(jobs, rooms):
    if rooms == 1:
        # 依開始時間掃描，堆積裡放「已結束的工作 (結束時間, 到那時的最佳值)」。
        best, ended, out = 0, [], 0
        for s, e, p in sorted(jobs):
            while ended and ended[0][0] <= s:
                best = max(best, heapq.heappop(ended)[1])
            heapq.heappush(ended, (e, best + p))
            out = max(out, best + p)
        return max([out] + [v for _, v in ended])
    # 時間軸網路，用 Bellman-Ford（佇列版）做逐次最短路，不用位勢。
    times = sorted({t for j in jobs for t in j[:2]})
    if not times:
        return 0
    index = {t: i for i, t in enumerate(times)}
    graph = [[] for _ in times]

    def add(u, v, cap, cost):
        graph[u].append([v, cap, cost, len(graph[v])])
        graph[v].append([u, 0, -cost, len(graph[u]) - 1])
    for i in range(len(times) - 1):
        add(i, i + 1, rooms, 0)
    for s, e, p in jobs:
        add(index[s], index[e], 1, -p)
    total, flow, sink = 0, 0, len(times) - 1
    while flow < rooms:
        dist = [None] * len(times)
        dist[0], back, queue, queued = 0, [None] * len(times), deque([0]), {0}
        while queue:
            u = queue.popleft()
            queued.discard(u)
            for k, (v, cap, cost, _) in enumerate(graph[u]):
                if cap and (dist[v] is None or dist[u] + cost < dist[v]):
                    dist[v], back[v] = dist[u] + cost, (u, k)
                    if v not in queued:
                        queued.add(v)
                        queue.append(v)
        if dist[sink] is None or dist[sink] >= 0:
            break
        push, v = rooms - flow, sink
        while v:
            u, k = back[v]
            push = min(push, graph[u][k][1])
            v = u
        v = sink
        while v:
            u, k = back[v]
            graph[u][k][1] -= push
            graph[v][graph[u][k][3]][1] += push
            v = u
        flow += push
        total += push * dist[sink]
    return -total


# ---- J：工期與期限 ----

def _j_feasible(chosen):
    t = 0
    for d, deadline, _ in sorted(chosen, key=lambda j: j[1]):
        t += d
        if t > deadline:
            return False
    return True


def j_brute(jobs):
    if len(jobs) > 12:
        return None
    return max(sum(j[2] for j in c) for r in range(len(jobs) + 1) for c in combinations(jobs, r) if _j_feasible(c))


def j_alt(jobs):
    if not jobs:
        return 0
    if all(d == 1 for d, _, _ in jobs):
        # 工期 1：利潤高的先放進「期限前最晚的空位」，空位用並查集跳。
        n = len(jobs)
        slot = list(range(n + 1))

        def find(x):
            while slot[x] != x:
                slot[x] = slot[slot[x]]
                x = slot[x]
            return x
        total = 0
        for _, deadline, p in sorted(jobs, key=lambda j: -j[2]):
            t = find(min(deadline, n))
            if t:
                slot[t] = t - 1
                total += p
        return total
    if len({p for _, _, p in jobs}) == 1:
        # Moore-Hodgson，用排序好的清單（不是堆積）丟掉最長的。
        kept, t = [], 0
        for d, deadline, _ in sorted(jobs, key=lambda j: j[1]):
            insort(kept, d)
            t += d
            if t > deadline:
                t -= kept.pop()
        return len(kept) * jobs[0][2]
    # 依期限排序，狀態是「已用時間 -> 最大利潤」的字典（稀疏）。
    states = {0: 0}
    for d, deadline, p in sorted(jobs, key=lambda j: j[1]):
        for t, value in list(states.items()):
            if t + d <= deadline and states.get(t + d, -1) < value + p:
                states[t + d] = value + p
    return max(states.values())


# ---- K：字串切割 ----

def k_brute(s, words):
    if len(s) > 24:
        return None
    costs, options = {}, {}
    for w, c in words:
        options.setdefault(w, {})
        options[w][c] = options[w].get(c, 0) + 1

    def walk(i, cost, copies):
        # 逐一列舉切法；同一個字的同價項目只差在選哪一筆，乘上筆數。
        if i == len(s):
            costs[cost] = costs.get(cost, 0) + copies
            return
        for w, by_cost in options.items():
            if s.startswith(w, i):
                for c, m in by_cost.items():
                    walk(i + len(w), cost + c, copies * m)
    walk(0, 0, 1)
    if not costs:
        return [-1, 0]
    best = min(costs)
    return [best, costs[best] % MOD]


def k_alt(s, words):
    trie = [{}]
    ends = [[]]
    for w, c in words:
        node = 0
        for ch in w:
            if ch not in trie[node]:
                trie[node][ch] = len(trie)
                trie.append({})
                ends.append([])
            node = trie[node][ch]
        ends[node].append(c)
    n = len(s)
    cost, ways = [None] * (n + 1), [0] * (n + 1)
    cost[0], ways[0] = 0, 1
    for i in range(n):
        if cost[i] is None:
            continue
        node, j = 0, i
        while j < n and s[j] in trie[node]:
            node = trie[node][s[j]]
            j += 1
            for c in ends[node]:
                if cost[j] is None or cost[i] + c < cost[j]:
                    cost[j], ways[j] = cost[i] + c, ways[i]
                elif cost[i] + c == cost[j]:
                    ways[j] = (ways[j] + ways[i]) % MOD
    return [-1, 0] if cost[n] is None else [cost[n], ways[n] % MOD]


# ---- L：套盒 ----

def l_brute(boxes, rotate):
    if len(boxes) > 300:
        return None

    def fits(a, b):
        # 可轉時兩兩比較「某個方向放得進去」即可：兩兩放得進去等價於 (短邊, 長邊) 都嚴格較小，
        # 所以整條鏈可以統一用短邊當寬，每個盒子只用一個方向。
        return (a[0] < b[0] and a[1] < b[1]) or (rotate and a[1] < b[0] and a[0] < b[1])
    items = sorted(boxes, key=lambda b: b[0] * b[1])
    best = []
    for i, b in enumerate(items):
        best.append(b[2] + max([best[j] for j in range(i) if fits(items[j], b)], default=0))
    return max(best, default=0)


def l_alt(boxes, rotate):
    items = sorted((min(w, h), max(w, h), v) if rotate else (w, h, v) for w, h, v in boxes)
    hs, vals, best, i = [], [], 0, 0
    while i < len(items):
        j = i
        while j < len(items) and items[j][0] == items[i][0]:
            j += 1
        group = []
        for _, h, v in items[i:j]:
            k = bisect_left(hs, h) - 1
            group.append((h, v + (vals[k] if k >= 0 else 0)))
        for h, v in group:
            best = max(best, v)
            k = bisect_left(hs, h + 1) - 1
            if k >= 0 and vals[k] >= v:
                continue
            k = bisect_left(hs, h)
            stop = k
            while stop < len(hs) and vals[stop] <= v:
                stop += 1
            hs[k:stop] = [h]
            vals[k:stop] = [v]
        i = j
    return best


# ---- M：拆邊與裝回 ----

def _m_present(m, toggles, k):
    present = [True] * m
    for i in toggles[:k]:
        present[i] = not present[i]
    return present


def m_brute(n, edges, toggles, checkpoints):
    if n > 12:
        return None
    out = []
    for k in checkpoints:
        present = _m_present(len(edges), toggles, k)
        label = list(range(n))
        changed = True
        while changed:
            changed = False
            for i, (u, v) in enumerate(edges):
                if present[i] and label[u] != label[v]:
                    label[u] = label[v] = min(label[u], label[v])
                    changed = True
        out.append(len(set(label)))
    return out


def m_alt(n, edges, toggles, checkpoints):
    """分塊：一塊切換之內沒動到的邊先併成並查集，再對每個檢查點只處理這塊裡動到的邊。"""
    block = max(1, int(len(toggles) ** .5))
    wanted = sorted(set(checkpoints))
    answer = {}
    present = [True] * len(edges)
    position = 0
    for start in range(0, len(toggles) + 1, block):
        stop = min(start + block, len(toggles))
        while position < start:
            present[toggles[position]] ^= True
            position += 1
        inside = [k for k in wanted if start <= k <= stop and k not in answer]
        if not inside:
            continue
        touched = set(toggles[start:stop])
        parent = list(range(n))

        def find(x, parent=parent):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        count = n
        for i, (u, v) in enumerate(edges):
            if present[i] and i not in touched:
                a, b = find(u), find(v)
                if a != b:
                    parent[a] = b
                    count -= 1
        for k in inside:
            state = {i: present[i] for i in touched}
            for i in toggles[start:k]:
                state[i] = not state[i]
            small = {}

            def top(x):
                while small.get(x, x) != x:
                    x = small[x]
                return x
            merged = 0
            for i, on in state.items():
                if on:
                    a, b = top(find(edges[i][0])), top(find(edges[i][1]))
                    if a != b:
                        small[a] = b
                        merged += 1
            answer[k] = count - merged
    return [answer[k] for k in checkpoints]


# ---- N：樹上最遠距離 ----

def _n_adjacency(n, edges):
    adjacency = [[] for _ in range(n)]
    for u, v, w in edges:
        adjacency[u].append((v, w))
        adjacency[v].append((u, w))
    return adjacency


def n_brute(n, edges, queries):
    if n > 300:
        return None
    adjacency = _n_adjacency(n, edges)
    out = []
    for q in queries:
        dist, stack = {q: 0}, [q]
        while stack:
            u = stack.pop()
            for v, w in adjacency[u]:
                if v not in dist:
                    dist[v] = dist[u] + w
                    stack.append(v)
        out.append(max(dist.values()))
    return out


def n_alt(n, edges, queries):
    """換根，但每個點對孩子清單做前綴／後綴最大值（不用前兩名）。"""
    adjacency = _n_adjacency(n, edges)
    parent, order, seen = [-1] * n, [0], [False] * n
    seen[0] = True
    children = [[] for _ in range(n)]
    for u in order:
        for v, w in adjacency[u]:
            if not seen[v]:
                seen[v] = True
                parent[v] = u
                children[u].append((v, w))
                order.append(v)
    down = [0] * n
    for u in reversed(order):
        for v, w in children[u]:
            down[u] = max(down[u], down[v] + w)
    up = [0] * n
    for u in order:
        branch = [down[v] + w for v, w in children[u]]
        prefix, suffix = [0], [0]
        for x in branch:
            prefix.append(max(prefix[-1], x))
        for x in reversed(branch):
            suffix.append(max(suffix[-1], x))
        suffix.reverse()
        for k, (v, w) in enumerate(children[u]):
            up[v] = w + max(up[u], prefix[k], suffix[k + 1])
    return [max(down[q], up[q]) for q in queries]


# ---- O：調成非遞減 ----

def o_brute(a, weights):
    if len(a) > 300:
        return None
    values = sorted(set(a))
    best = [0] * len(values)
    for x, w in zip(a, weights):
        best = [best[j] + w * abs(x - v) for j, v in enumerate(values)]
        for j in range(1, len(best)):
            best[j] = min(best[j], best[j - 1])
    return best[-1] if values else 0


def o_alt(a, weights):
    """門檻分解：對每個相鄰值的門檻，最佳的 0/1 階梯錯配數 = 線段樹上所有切點的最小值。"""
    n = len(a)
    if n == 0:
        return 0
    size = 1
    while size < n + 1:
        size *= 2
    lo_add, low = [0] * (2 * size), [0] * (2 * size)
    for k in range(n + 1, size):
        low[size + k] = float('inf')
    for i in range(size - 1, 0, -1):
        low[i] = min(low[2 * i], low[2 * i + 1])

    def pull(i):
        while i > 1:
            i //= 2
            low[i] = min(low[2 * i], low[2 * i + 1]) + lo_add[i]

    def add(lo, hi, value):
        lo += size
        hi += size
        a0, b0 = lo, hi - 1
        while lo < hi:
            if lo & 1:
                lo_add[lo] += value
                low[lo] += value
                lo += 1
            if hi & 1:
                hi -= 1
                lo_add[hi] += value
                low[hi] += value
            lo //= 2
            hi //= 2
        pull(a0)
        pull(b0)
    # 門檻在最小值之下時，所有元素都 > 門檻：切點 k 的錯配是前 k 個元素的權重和。
    for i, w in enumerate(weights):
        add(i + 1, n + 1, w)
    values = sorted(set(a))
    at = {}
    for i, x in enumerate(a):
        at.setdefault(x, []).append(i)
    total = 0
    for j in range(len(values) - 1):
        for i in at[values[j]]:
            add(i + 1, n + 1, -weights[i])
            add(0, i + 1, weights[i])
        total += (values[j + 1] - values[j]) * low[1]
    return total


# ---- P：多路合併 ----

def p_brute(piles, k):
    if len(piles) > 9:
        return None

    @lru_cache(maxsize=None)
    def go(state):
        if len(state) == 1:
            return 0
        best = None
        for r in range(2, min(k, len(state)) + 1):
            for pick in combinations(range(len(state)), r):
                merged = sum(state[i] for i in pick)
                rest = tuple(sorted([state[i] for i in range(len(state)) if i not in pick] + [merged]))
                value = merged + go(rest)
                best = value if best is None or value < best else best
        return best
    return go(tuple(sorted(piles)))


def p_alt(piles, k):
    n = len(piles)
    if n == 1:
        return 0
    k = min(k, n)
    first = deque(sorted(piles) if k == n or (n - 1) % (k - 1) == 0 else
                  [0] * ((k - 1) - (n - 1) % (k - 1)) + sorted(piles))
    merged, total = deque(), 0
    while len(first) + len(merged) > 1:
        s = 0
        for _ in range(k):
            if not first and not merged:
                break
            if not merged or (first and first[0] <= merged[0]):
                s += first.popleft()
            else:
                s += merged.popleft()
        total += s
        merged.append(s)
    return total


CHECKERS = {'I': (i_brute, i_alt), 'J': (j_brute, j_alt), 'K': (k_brute, k_alt), 'L': (l_brute, l_alt),
            'M': (m_brute, m_alt), 'N': (n_brute, n_alt), 'O': (o_brute, o_alt), 'P': (p_brute, p_alt)}


# ---- 期望值核對 ----

@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_expected_values_match_brute_force_on_small_inputs(key):
    task, meta = built(*key)
    brute = CHECKERS[key[0]][0]
    checked = 0
    for test in task['public_tests'] + task['hidden_tests'][:meta['large_from']]:
        value = brute(*args_of(test))
        if value is not None:
            checked += 1
            assert sandbox.strict_equal(value, test['expected']), (task['id'], test['args'])
    # 公開範例與隱藏小測試幾乎都在暴力法能跑的範圍內。
    assert checked >= 40


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_expected_values_match_a_second_algorithm_including_large_inputs(key):
    task, _ = built(*key)
    alt = CHECKERS[key[0]][1]
    for test in task['public_tests'] + task['hidden_tests']:
        assert sandbox.strict_equal(alt(*args_of(test)), test['expected']), task['id']


# ---- 題目形狀 ----

@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_task_shape_prompt_and_determinism(key):
    task, meta = built(*key)
    fam = ct.TRAP_FAMILIES[key[0]]
    assert ct.make(*key) == task
    assert set(task) == {'id', 'family', 'level', 'seed', 'prompt', 'entry_point', 'public_tests', 'hidden_tests',
                         'kind'}
    assert task['id'] == ids(key) and task['entry_point'] == fam.entry and task['kind'] == 'code'
    assert len(task['public_tests']) == 3 and 40 <= len(task['hidden_tests']) <= 60
    large = task['hidden_tests'][meta['large_from']:]
    assert 1 <= len(large) <= 6
    prompt = task['prompt']
    assert f'def {fam.entry}(...)' in prompt and '\nLimits:\n' in prompt and ct.TIME_LIMIT_RULE in prompt
    assert prompt.rstrip().endswith('Do not include tests or example calls in the block.')
    for test in task['public_tests']:
        assert ct._call(task['entry_point'], test['args']) in prompt and repr(test['expected']) in prompt
        assert len(json.dumps(test['args'])) < 400
    keys = [json.dumps(t['args'], sort_keys=True) for t in task['public_tests'] + task['hidden_tests']]
    assert len(set(keys)) == len(keys)


# 每級題目「Limits:」寫給模型看的上限；這裡獨立寫一份，確認每個測試都在上限內（不公平的測試不算陷阱）。

def _ints(values, lo, hi):
    return all(type(v) is int and lo <= v <= hi for v in values)


def i_ok(level, jobs, rooms):
    n, t, p = {1: (12, 100, 50), 2: (50000, 10 ** 9, 10 ** 6), 3: (60, 1000, 1000), 4: (300, 10 ** 6, 10 ** 6),
               5: (1500, 10 ** 9, 10 ** 6)}[level]
    lo, hi = {1: (1, 1), 2: (1, 1), 3: (2, 2), 4: (1, 4), 5: (1, 6)}[level]
    return lo <= rooms <= hi and len(jobs) <= n and all(
        len(j) == 3 and _ints(j, 0, t) and j[0] < j[1] and 1 <= j[2] <= p for j in jobs)


def j_ok(level, jobs):
    n, d, dl, p, unit_profit, unit_duration = {
        1: (12, 10, 60, 1, True, False), 2: (100000, 10000, 10 ** 9, 1, True, False),
        3: (12, 10, 60, 1000, False, False), 4: (100, 50, 10 ** 9, 10 ** 6, False, False),
        5: (100000, 1, 10 ** 9, 10 ** 9, False, True)}[level]
    return len(jobs) <= n and all(_ints([a], 1, d) and _ints([b], 1, dl) and _ints([c], 1, p) for a, b, c in jobs) \
        and (not unit_profit or all(c == 1 for *_, c in jobs)) and (not unit_duration or all(a == 1 for a, *_ in jobs))


def k_ok(level, s, words):
    n, count, longest = {1: (20, 8, 6), 2: (30000, 200, 10), 3: (20, 10, 6), 4: (20000, 500, 20),
                         5: (20000, 5000, 12)}[level]
    lower = set('abcdefghijklmnopqrstuvwxyz')
    distinct = level > 2 or len({w for w, _ in words}) == len(words)
    return len(s) <= n and set(s) <= lower and len(words) <= count and distinct and all(
        1 <= len(w) <= longest and set(w) <= lower and _ints([c], 1, 1000) for w, c in words)


def l_ok(level, boxes, rotate):
    n = {1: 12, 2: 30000, 3: 30000, 4: 30000, 5: 30000}[level]
    return type(rotate) is bool and (rotate is False or level in (3, 5)) and len(boxes) <= n and all(
        _ints(b[:2], 1, 10 ** 9) and (b[2] == 1 if level <= 3 else _ints(b[2:], 1, 10 ** 6)) for b in boxes)


def m_ok(level, n, edges, toggles, checkpoints):
    limit_n, m, c, t = {1: (8, 12, 10, 12), 2: (10, 16, 20, 16), 3: (30000, 40000, 300, 40000),
                        4: (10, 16, 20, 30), 5: (5000, 5000, 2000, 10000)}[level]
    return 1 <= n <= limit_n and len(edges) <= m and len(toggles) <= t and len(checkpoints) <= c \
        and all(_ints(e, 0, n - 1) and (level > 1 or e[0] != e[1]) for e in edges) \
        and _ints(toggles, 0, len(edges) - 1) and (level > 3 or len(set(toggles)) == len(toggles)) \
        and _ints(checkpoints, 0, len(toggles))


def n_ok(level, n, edges, queries):
    limit, lo, hi = {1: (12, 1, 20), 2: (30000, 1, 10 ** 6), 3: (12, -20, 20), 4: (30000, -10 ** 6, 10 ** 6),
                     5: (50000, -10 ** 6, 10 ** 6)}[level]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for u, v, w in edges:
        if not (_ints([u, v], 0, n - 1) and _ints([w], lo, hi)) or find(u) == find(v):
            return False
        parent[find(u)] = find(v)
    return 1 <= n <= limit and len(edges) == n - 1 and len(queries) <= 1000 and _ints(queries, 0, n - 1)


def o_ok(level, a, weights):
    n, value, weight = {1: (10, 20, 1), 2: (20, 10 ** 9, 1), 3: (20000, 10 ** 9, 1), 4: (12, 10 ** 9, 10 ** 6),
                        5: (20000, 10 ** 9, 10 ** 6)}[level]
    return len(a) == len(weights) <= n and _ints(a, -value, value) and _ints(weights, 1, weight)


def p_ok(level, piles, k):
    n, size, lo, hi = {1: (10, 100, 2, 2), 2: (50000, 10 ** 9, 2, 2), 3: (10, 100, 3, 5),
                       4: (50000, 10 ** 9, 2, 10), 5: (50000, 10 ** 9, 2, 100000)}[level]
    return 1 <= len(piles) <= n and _ints(piles, 1, size) and _ints([k], lo, hi)


WITHIN_LIMITS = {'I': i_ok, 'J': j_ok, 'K': k_ok, 'L': l_ok, 'M': m_ok, 'N': n_ok, 'O': o_ok, 'P': p_ok}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_every_test_input_respects_the_limits_in_the_prompt(key):
    task, _ = built(*key)
    for test in task['public_tests'] + task['hidden_tests']:
        assert WITHIN_LIMITS[key[0]](key[1], *test['args']), (task['id'], json.dumps(test['args'])[:200])


def test_every_family_has_large_inputs_where_a_slow_trap_applies():
    for letter, fam in ct.TRAP_FAMILIES.items():
        assert any(t in fam.slow_traps for level in ct.LEVELS for t in fam.traps(level)), letter
        assert any(t not in fam.slow_traps for level in ct.LEVELS for t in fam.traps(level)), letter
        for level in ct.LEVELS:
            assert len(fam.traps(level)) >= 2, (letter, level)
            assert set(fam.traps(level)) <= set(fam.trap_sources)


# ---- 錯誤解：過公開範例、栽在隱藏測試 ----

@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_every_trap_passes_the_public_examples_and_wrong_traps_fail_small_hidden_tests(key):
    task, meta = built(*key)
    fam = ct.TRAP_FAMILIES[key[0]]
    for trap in fam.traps(key[1]):
        function = fam.function(trap)
        for test in task['public_tests']:
            assert sandbox.strict_equal(function(*args_of(test)), test['expected']), (task['id'], trap)
    small = task['hidden_tests'][:meta['large_from']]
    for trap in fam.wrong_traps(key[1]):
        function = fam.function(trap)
        wrong = 0
        for test in small:
            try:
                wrong += not sandbox.strict_equal(json.loads(json.dumps(function(*args_of(test)))), test['expected'])
            except Exception:
                wrong += 1
        assert wrong >= ct.TRAP_COVERAGE, (task['id'], trap, wrong)


def reply(code):
    return 'Here is the function.\n\n```python\n' + code + '\n```\n'


@needs_sandbox
def test_slow_traps_pass_public_examples_but_time_out_on_large_inputs_in_the_sandbox():
    jobs = []
    for key in KEYS:
        task, meta = built(*key)
        fam = ct.TRAP_FAMILIES[key[0]]
        for trap in fam.traps(key[1]):
            if trap in fam.slow_traps:
                jobs.append((task, meta['large_from'], fam.trap_sources[trap], trap))
    assert len(jobs) >= 20

    def run(job):
        task, large_from, source, trap = job
        public = ct.BlindCodeEnvironment(task).validate(reply(source))
        # 只跑第一個大輸入就夠證明逾時；全部隱藏測試的計分也一定沒過。
        outcome = sandbox.run_tests(source, task['entry_point'], task['hidden_tests'][large_from:large_from + 1])
        return task['id'], trap, public, outcome
    with ThreadPoolExecutor(4) as pool:
        for task_id, trap, public, outcome in pool.map(run, jobs):
            assert public == {'passed': True, 'checks': []}, (task_id, trap)
            details = {f['detail'] for f in outcome['failures']}
            assert outcome['passed'] == 0 and details & {'timeout', 'RecursionError'}, (task_id, trap, outcome)


# ---- 參考解：隔離環境在正式的每題 1 秒上限下全過（實測大輸入每題約 0.05～0.3 秒）----

@needs_sandbox
@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_reference_passes_every_test_in_the_sandbox(key):
    task, meta = built(*key)
    fam = ct.TRAP_FAMILIES[key[0]]
    text = reply(fam.reference_source)
    assert ct.BlindCodeEnvironment(task).validate(text) == {'passed': True, 'checks': []}
    outcome = sandbox.run_tests(fam.reference_source, task['entry_point'], task['hidden_tests'],
                                timeout_s=ct.HIDDEN_TIMEOUT_S)
    assert outcome == {'status': 'ok', 'passed': len(task['hidden_tests']), 'total': len(task['hidden_tests']),
                       'failures': []}, task['id']


@needs_sandbox
def test_grade_hidden_uses_the_longer_overall_limit():
    task, _ = built('N', 5, 0)
    graded = ct.grade_hidden(task, reply(ct.TRAP_FAMILIES['N'].reference_source))
    assert graded == {'passed': True, 'hidden_passed': len(task['hidden_tests']),
                      'hidden_total': len(task['hidden_tests']), 'status': 'ok'}
    memorized = reply('def farthest(n, edges, queries):\n    return [0] * len(queries)\n')
    graded = ct.grade_hidden(task, memorized)
    assert graded['passed'] is False and graded['status'] == 'ok'


# ---- 凍結與相容 ----

def test_committed_v2_frozen_set_is_the_default_and_interleaved():
    tasks = ct.frozen_code_tasks()
    assert len(tasks) == 40
    assert {(t['family'], t['level']) for t in tasks} == {(f, level) for f in ct.TRAP_FAMILIES for level in ct.LEVELS}
    assert [t['family'] for t in tasks[:8]] == list(ct.TRAP_FAMILIES)
    assert all(a['family'] != b['family'] for a, b in zip(tasks, tasks[1:]))
    assert [t['id'] for t in ct.frozen_code_tasks(limit=2)] == ['I1-0', 'J1-0']
    frozen = json.loads(ct.FROZEN_CODE_TASKS.read_text())
    assert frozen['version'] == ct.TRAP_VERSION and set(frozen['family_revision']) == set(ct.TRAP_FAMILIES)


def test_freeze_refuses_to_mix_versions_and_checks_the_version(tmp_path):
    tasks = ct.generate(families=['I', 'P'], levels=(1,), seeds=(0,))
    assert [t['id'] for t in tasks] == ['I1-0', 'P1-0']
    path = tmp_path / 'v2.json'
    assert ct.freeze(path, tasks)['version'] == ct.TRAP_VERSION
    assert [t['id'] for t in ct.frozen_code_tasks(path)] == ['I1-0', 'P1-0']
    data = json.loads(path.read_text())
    data['version'] = ct.VERSION
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='version'):
        ct.frozen_code_tasks(path)
    with pytest.raises(ValueError, match='first-version or only second-version'):
        ct.freeze(tmp_path / 'mixed.json', tasks + ct.generate(families=['A'], levels=(1,)))


def test_cached_builds_return_independent_copies():
    first = ct.make('P', 1, 0)
    first['hidden_tests'].clear()
    assert len(ct.make('P', 1, 0)['hidden_tests']) >= 40


# ---- 隔離執行：記憶體上限 ----

def test_runner_kills_programs_that_use_too_much_memory():
    code = 'def f(a, b):\n    x = [0] * (3 * 10 ** 8)\n    return len(x)\n'
    result = sandbox.run_tests(code, 'f', [{'args': [1, 2], 'expected': 3}], require_sandbox=TRUSTED_SANDBOX)
    # macOS 由父行程的記憶體看守殺掉（crashed）；Linux 由 RLIMIT_AS 在子行程內丟 MemoryError。
    assert result['passed'] == 0
    assert result['status'] == 'crashed' or result['failures'][0]['detail'] == 'MemoryError'
