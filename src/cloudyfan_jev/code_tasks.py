"""盲測改程式題：題目產生器、凍結、取程式碼與盲測環境（設計見 plans/mrc-blind-validation-experiment-2026-11-24.md）。

第一版（A～H，凍結檔 code-tasks-20261124.json）是「規格逐條寫明邊界條款」的題；校準 01 證實太簡單，
保留只為了能重現舊凍結檔與校準證據。第二版（I～P，凍結檔 code-tasks-v2-20261124.json）是演算法陷阱題，
說明見下方「第二版題集」一節；校準 02、03 證實規格把陷阱點明時一樣太簡單。第三版（Q～X，凍結檔
code-tasks-v3-20261124.json，現在的預設）是工單式題目，寫在 code_tickets.py，這裡只登記與凍結。

以下說明第一版。每題要求寫一個純函式。題目文字列出完整規格與 3 個公開範例；公開範例刻意只碰一般情況，
隱藏測試（至少 50 個，固定亂數種子產生）大量覆蓋規格裡的邊界條款。

每個題型有一個參考解 ``solve(level, params, args, skip)``。``skip`` 可以關掉一條「邊界條款」
（改成模型最可能寫錯的樣子），用來量化覆蓋：
- 公開範例：關掉任何一條邊界條款，答案都不變（公開範例碰不到邊界）。
- 隱藏測試：每條邊界條款至少有 ``EDGE_COVERAGE`` 個測試會因為關掉它而答案改變。
CSV 題型用解析時記錄的事件代替開關。``tests/test_code_tasks.py`` 以第二種獨立寫法核對全部期望值。

盲測環境的 ``validate`` 只跑公開範例；隱藏測試只在事後的 ``grade_hidden`` 計分，絕不回給模型。
"""
from collections import Counter
from datetime import date, timedelta
from functools import cmp_to_key, lru_cache
import hashlib
import json
from pathlib import Path
import random
import re

from . import code_sandbox

VERSION = 'cloudyfan-code-tasks-v1'
LEVELS = (1, 2, 3, 4, 5)
# 各題型產生器版本；校準後改題時只升該題型的版本，其餘題目不變。
FAMILY_REVISION = {'A': 1, 'B': 1, 'C': 1, 'D': 1, 'E': 1, 'F': 1, 'G': 1, 'H': 1,
                   'I': 1, 'J': 1, 'K': 1, 'L': 1, 'M': 1, 'N': 1, 'O': 1, 'P': 1,
                   'Q': 1, 'R': 1, 'S': 1, 'T': 1, 'U': 1, 'V': 1, 'W': 1, 'X': 1,
                   'ZA': 1, 'ZB': 1, 'ZC': 2, 'ZD': 1, 'ZE': 1, 'ZF': 1, 'ZG': 1, 'ZH': 1}
FROZEN_CODE_TASKS_V1 = Path(__file__).resolve().parents[2] / 'docs/evidence/code-tasks-20261124.json'
FROZEN_CODE_TASKS_V2 = Path(__file__).resolve().parents[2] / 'docs/evidence/code-tasks-v2-20261124.json'
FROZEN_CODE_TASKS_V3 = Path(__file__).resolve().parents[2] / 'docs/evidence/code-tasks-v3-20261124.json'
FROZEN_CODE_TASKS_V4 = Path(__file__).resolve().parents[2] / 'docs/evidence/code-tasks-v4-20261124.json'
FROZEN_CODE_TASKS = FROZEN_CODE_TASKS_V3
FROZEN_CODE_TASK_SETS = {'v1': FROZEN_CODE_TASKS_V1, 'v2': FROZEN_CODE_TASKS_V2, 'v3': FROZEN_CODE_TASKS_V3,
                         'v4': FROZEN_CODE_TASKS_V4}
PUBLIC_COUNT = 3
HIDDEN_MIN = 60
HIDDEN_MAX = 120
EDGE_COVERAGE = 4
CHECK_CHARS = 200
CHECKS_MAX_BYTES = 3500

REPLY_RULES = ('Reply with exactly one ```python code block that contains `def {entry}(...)` and any helper code it '
               'needs. Use only the Python standard library. The code must not read or write files, access the '
               'network, start processes or print anything. Do not include tests or example calls in the block.')


class Family:
    letter = ''
    name = ''
    entry = ''

    def params(self, rng, level):
        return {}

    def signature(self, level):
        raise NotImplementedError

    def rules(self, level, params):
        raise NotImplementedError

    def toggles(self, level):
        return ()

    def solve(self, level, params, args, skip=frozenset()):
        raise NotImplementedError

    def edges(self, level, params, args):
        """這組輸入碰到哪些邊界條款：關掉該條款後答案會改變的那些。"""
        expected = self.solve(level, params, args)
        return {t for t in self.toggles(level) if self.solve(level, params, args, frozenset({t})) != expected}

    def edge_names(self, level):
        return tuple(self.toggles(level))

    def random_case(self, rng, level, params):
        raise NotImplementedError

    def public_case(self, rng, level, params):
        raise NotImplementedError

    def plain_enough(self, level, params, args, expected):
        return True


# ---- A：區間合併（端點開閉、相接、空區間、扣除、橋接）----

A_KINDS = ('[]', '[)', '(]', '()')


class Intervals(Family):
    letter, name, entry = 'A', 'interval_union', 'merge_intervals'

    def signature(self, level):
        return {1: 'merge_intervals(intervals)', 2: 'merge_intervals(intervals)',
                3: 'merge_intervals(intervals, min_len)', 4: 'merge_intervals(intervals, blocked, min_len)',
                5: 'merge_intervals(intervals, blocked, min_len, max_gap)'}[level]

    def rules(self, level, params):
        if level == 1:
            return [
                '`intervals` is a list of pairs `[a, b]` of integers. Each pair denotes the closed interval of all '
                'real numbers x with min(a, b) <= x <= max(a, b). In particular, if a > b the two numbers are '
                'simply swapped: [5, 2] means the same as [2, 5].',
                'A pair with a == b denotes a single point. It is a valid interval and is never dropped.',
                'Return the union of all intervals as a list of pairwise disjoint closed intervals `[start, end]` '
                'sorted by start. Two intervals end up in the same output interval exactly when their union is a '
                'connected set of real numbers: [1, 3] and [3, 5] give [1, 5], but [1, 2] and [3, 4] stay separate '
                'because the real numbers strictly between 2 and 3 are not covered.',
                'The input may be empty (return []) and may contain duplicates.',
            ]
        rules = [
            '`intervals` is a list of items `[a, b, kind]` with integers a, b and kind one of "[]", "[)", "(]", '
            '"()". The first character describes the smaller endpoint and the second character the larger '
            'endpoint: "[" or "]" means the endpoint is included, "(" or ")" means it is excluded. Example: '
            '[2, 5, "[)"] is the set {x : 2 <= x < 5} of real numbers.',
            'If a > b, swap the two numbers but keep kind unchanged: [5, 2, "[)"] is the same as [2, 5, "[)"].',
            'If a == b, kind "[]" denotes the single point {a}, which is a valid interval; the other three kinds '
            'denote the empty set. Empty items are ignored completely.',
            'Merging: the union of all items is split into its maximal connected parts. Each part is written as '
            '`[start, end, kind]` where kind states exactly which endpoints belong to the part. Examples: '
            '[1, 3, "[)"] and [3, 5, "[]"] give [1, 5, "[]"]; [1, 3, "()"] and [3, 5, "()"] stay separate because '
            '3 is not covered; [1, 3, "[)"] and [1, 3, "(]"] give [1, 3, "[]"]. Parts are sorted by start.',
        ]
        if level == 2:
            return rules + ['Return the list of parts. The input may be empty (return []).']
        if level == 3:
            return rules + [
                'Each output item is `[start, end, kind, count]`, where count is the number of non-empty input '
                'items (duplicates counted separately) that lie inside that part.',
                'Drop every part whose length end - start is less than `min_len` (a non-negative integer). A single '
                'point has length 0. A part whose length equals min_len is kept.',
                'Return the remaining items sorted by start. The input may be empty (return []).',
            ]
        rules[0] = rules[0].replace('`intervals` is', '`intervals` and `blocked` are each')
        steps = []
        if level == 5:
            steps.append(
                'Bridging: after merging, walk through the parts from left to right. Whenever the gap '
                'next.start - current.end between the current part (which may already be the result of bridging) '
                'and the next part is at most `max_gap` (a non-negative integer), join them into one part that '
                'also covers everything in between; it keeps the left endpoint (and its kind) of the current part '
                'and the right endpoint (and its kind) of the next part. A gap of 0 occurs when both touching '
                'endpoints are excluded, e.g. [1, 3, "[)"] and [3, 5, "(]"] have gap 0. Bridging only uses '
                '`intervals`; it happens before the removal step.')
        steps += [
            'Removal: then remove every real number that belongs to some non-empty item of `blocked` (rules 1-3 '
            'also apply to `blocked`). This can split, shrink or delete parts. An endpoint created by the removal '
            'is included exactly when the blocked item that touches it excludes it. Examples: removing '
            '[2, 3, "[]"] from [0, 5, "[]"] leaves [0, 2, "[)"] and [3, 5, "(]"]; removing [2, 4, "()"] from '
            '[2, 4, "[]"] leaves the two single points [2, 2, "[]"] and [4, 4, "[]"]. Parts that become empty '
            'disappear.',
            'Each output item is `[start, end, kind, count]`, where count is the number of non-empty items of '
            '`intervals` (duplicates counted separately) that share at least one real number with that output '
            'item (after removal).',
            'Finally drop every item whose length end - start is less than `min_len` (a non-negative integer). A '
            'single point has length 0. An item whose length equals min_len is kept.',
            'Return the remaining items sorted by start. Either list may be empty.',
        ]
        return rules + steps

    def toggles(self, level):
        if level == 1:
            return ('swap', 'point', 'gap_not_connected')
        # 第 5 級的橋接一定會接起間隙為 0 的兩段，所以 open_touch 改由 bridge_open 量。
        return ('swap', 'empty') + (('open_touch',) if level <= 4 else ()) + (('min_len_equal',) if level >= 3 else ()) \
            + (('cut_kind', 'count_intersect') if level >= 4 else ()) \
            + (('gap_equal', 'bridge_open', 'bridge_before_cut') if level == 5 else ())

    @staticmethod
    def _cells(level, item, skip):
        a, b = item[0], item[1]
        kind = item[2] if level >= 2 else '[]'
        if a > b:
            if 'swap' in skip:
                return None
            a, b = b, a
        if level == 1 and a == b and 'point' in skip:
            return None
        lo = 2 * a if kind[0] == '[' else 2 * a + 1
        hi = 2 * b if kind[1] == ']' else 2 * b - 1
        if lo > hi:
            if 'empty' not in skip:
                return None
            lo = hi = 2 * a
        return lo, hi

    @staticmethod
    def _runs(cells):
        runs = []
        for c in sorted(cells):
            if runs and c == runs[-1][1] + 1:
                runs[-1][1] = c
            else:
                runs.append([c, c])
        return runs

    @staticmethod
    def _bridge(runs, max_gap, skip):
        out = []
        for run in runs:
            if out:
                gap = run[0] // 2 - (out[-1][1] + 1) // 2
                ok = gap < max_gap if 'gap_equal' in skip else gap <= max_gap
                if gap == 0 and 'bridge_open' in skip:
                    ok = False
                if ok:
                    out[-1][1] = run[1]
                    continue
            out.append(list(run))
        return out

    def solve(self, level, params, args, skip=frozenset()):
        items = [r for r in (self._cells(level, it, skip) for it in args[0]) if r]
        blocked = [r for r in (self._cells(level, it, skip) for it in args[1]) if r] if level >= 4 else []
        min_len = args[1] if level == 3 else args[2] if level >= 4 else None
        cells = set()
        for lo, hi in items:
            cells.update(range(lo, hi + 1))
        runs = self._runs(cells)
        # 漏掉「相接但沒覆蓋」條款時，會把只差一個格點的兩段接起來。
        if (level == 1 and 'gap_not_connected' in skip) or (level >= 2 and 'open_touch' in skip):
            parity = 0 if level == 1 else 1
            merged = []
            for run in runs:
                if merged and run[0] == merged[-1][1] + 2 and run[0] % 2 == parity and merged[-1][1] % 2 == parity:
                    merged[-1][1] = run[1]
                else:
                    merged.append(run)
            runs = merged
        removed = set()
        for lo, hi in blocked:
            removed.update(range(lo, hi + 1))
        if level == 5 and 'bridge_before_cut' not in skip:
            runs = self._bridge(runs, args[3], skip)
        kept = set()
        for lo, hi in runs:
            kept.update(range(lo, hi + 1))
        before = set(kept)
        kept -= removed
        runs = self._runs(kept)
        if level == 5 and 'bridge_before_cut' in skip:
            runs = self._bridge(runs, args[3], skip)
        out = []
        for lo, hi in runs:
            start, end = lo // 2, (hi + 1) // 2
            left = '[' if lo % 2 == 0 else '('
            right = ']' if hi % 2 == 0 else ')'
            if level >= 4 and 'cut_kind' in skip:
                if lo % 2 and (lo - 1) in before and (lo - 1) in removed:
                    left = '['
                if hi % 2 and (hi + 1) in before and (hi + 1) in removed:
                    right = ']'
            if level == 1:
                out.append([start, end])
                continue
            if level == 2:
                out.append([start, end, left + right])
                continue
            if level == 3 or 'count_intersect' in skip:
                count = sum(1 for a, b in items if lo <= a and b <= hi)
            else:
                count = sum(1 for a, b in items if any(lo <= c <= hi and c in kept for c in range(a, b + 1)))
            length = end - start
            if length < min_len or (length == min_len and 'min_len_equal' in skip):
                continue
            out.append([start, end, left + right, count])
        return out

    def _item(self, rng, level, a, b, kind=None):
        if level == 1:
            return [a, b]
        return [a, b, kind or rng.choice(A_KINDS)]

    def random_case(self, rng, level, params):
        points = [rng.randint(-6, 14) for _ in range(rng.randint(3, 7))]

        def endpoint():
            return rng.choice(points) if rng.random() < .7 else rng.randint(-6, 14)

        def items(count):
            out = []
            for _ in range(count):
                a = endpoint()
                b = a if rng.random() < .15 else endpoint()
                if rng.random() < .25 and a + 1 <= 16:
                    b = a + 1
                out.append(self._item(rng, level, a, b))
            return out
        intervals = items(rng.randint(0, 7))
        if level <= 2:
            return [intervals]
        min_len = rng.choice((0, 0, 1, 1, 2, 3, 4))
        if level == 3:
            return [intervals, min_len]
        blocked = items(rng.randint(0, 3))
        return [intervals, blocked, min_len] + ([rng.choice((0, 0, 1, 2, 3))] if level == 5 else [])

    def public_case(self, rng, level, params):
        count = rng.randint(3, 5)
        starts = sorted(rng.sample(range(0, 40), count))
        intervals = []
        for s in starts:
            e = s + rng.randint(2, 9)
            intervals.append(self._item(rng, level, s, e))
        rng.shuffle(intervals)
        if level <= 2:
            return [intervals]
        min_len = rng.randint(1, 3)
        if level == 3:
            return [intervals, min_len]
        bs = rng.randrange(0, 60)
        blocked = [self._item(rng, level, bs, bs + rng.randint(1, 4))]
        return [intervals, blocked, min_len] + ([rng.randint(1, 3)] if level == 5 else [])

    def plain_enough(self, level, params, args, expected):
        return 2 <= len(expected) < len(args[0])


# ---- B：工作日加減（假日、補假、每年固定假日、自訂週末、n = 0 的順延規則）----

class Workdays(Family):
    letter, name, entry = 'B', 'workday_arithmetic', 'add_workdays'

    def signature(self, level):
        return {1: 'add_workdays(start, n, holidays)', 2: 'add_workdays(start, n, holidays)',
                3: 'add_workdays(start, n, holidays)', 4: 'add_workdays(start, n, holidays, weekend)',
                5: 'add_workdays(start, n, holidays, weekend, roll)'}[level]

    def rules(self, level, params):
        rules = ['Dates are strings "YYYY-MM-DD" in the proleptic Gregorian calendar. `start` is such a string and '
                 '`n` is an integer (it may be negative or zero). Return a date string in the same format.']
        if level <= 3:
            rules.append('Saturdays and Sundays are not working days. Every date listed in `holidays` is not a '
                         'working day. All other dates are working days. `holidays` may contain duplicates and '
                         'dates that fall on a weekend.')
        else:
            rules.append('`weekend` is a list of weekday numbers (0 = Monday, 1 = Tuesday, ..., 6 = Sunday). Days '
                         'whose weekday is in `weekend` are not working days. `weekend` may be empty or contain '
                         'duplicates and never contains more than 5 distinct numbers. Every holiday (see below) is '
                         'not a working day. All other dates are working days. `holidays` may contain duplicates '
                         'and dates that fall on a weekend day.')
        if level >= 3:
            rules.append('An entry of `holidays` is either a dated holiday "YYYY-MM-DD" or a recurring holiday '
                         '"MM-DD", which is a holiday on that month and day in every year. "02-29" is a holiday '
                         'only in leap years.')
        if level in (2, 3):
            rules.append('Observed days: if a holiday falls on a Saturday, the Friday immediately before it is also '
                         'not a working day; if a holiday falls on a Sunday, the Monday immediately after it is also '
                         'not a working day. This holds even when that Friday or Monday lies in another month or '
                         'year. An observed day never causes another observed day.'
                         + (' Recurring holidays create observed days exactly like dated ones.' if level == 3 else ''))
        if level >= 4:
            rules.append('Observed days: go through every holiday (dated or recurring) that falls on a weekend day, '
                         'in chronological order. Each one makes exactly one extra day non-working: the first day '
                         'after it that is not a weekend day, not a holiday, and not already chosen as the observed '
                         'day of an earlier holiday. Observed days may lie in another month or year.')
        rules += ['If n > 0: starting from `start`, step forward one calendar day at a time. Every working day you '
                  'step onto counts 1. The answer is the date on which the count reaches n. `start` itself is '
                  'never counted, even if it is a working day.',
                  'If n < 0: the same, stepping backward one day at a time, until |n| working days were counted.']
        if level <= 4:
            rules.append('If n == 0: return `start` if it is a working day; otherwise return the first working day '
                         'after `start`.')
        else:
            rules.append('If n == 0: return `start` if it is a working day. Otherwise `roll` decides: "following" '
                         'returns the first working day after start; "preceding" returns the last working day '
                         'before start; "modified_following" is like "following" unless that day is in a different '
                         'calendar month than start, in which case it is like "preceding"; "modified_preceding" is '
                         'like "preceding" unless that day is in a different calendar month than start, in which '
                         'case it is like "following". `roll` has no effect in any other situation.')
        return rules

    def toggles(self, level):
        base = ('zero_roll', 'negative')
        if level == 1:
            return base
        if level == 2:
            return base + ('observed_sat', 'observed_sun')
        if level == 3:
            return base + ('observed_sat', 'observed_sun', 'recurring', 'recurring_observed', 'leap_day')
        return base + ('recurring', 'leap_day', 'observed', 'observed_cascade') \
            + (('roll', 'modified_month') if level == 5 else ())

    def calendar(self, level, holidays, weekend, year, skip):
        dated, recurring = set(), set()
        for entry in holidays:
            if len(entry) == 10:
                dated.add(date.fromisoformat(entry))
            elif level >= 3 and 'recurring' not in skip:
                month, day = int(entry[:2]), int(entry[3:])
                for y in range(year - 2, year + 3):
                    try:
                        recurring.add(date(y, month, day))
                    except ValueError:
                        # 漏掉「02-29 只在閏年」時，常見寫法是平年改放 2 月 28 日。
                        if 'leap_day' in skip:
                            recurring.add(date(y, 2, 28))
        holiday = dated | recurring
        observed = set()
        if 2 <= level <= 3:
            sources = dated if 'recurring_observed' in skip else holiday
            for h in sources:
                if h.weekday() == 5 and 'observed_sat' not in skip:
                    observed.add(h - timedelta(days=1))
                if h.weekday() == 6 and 'observed_sun' not in skip:
                    observed.add(h + timedelta(days=1))
        elif level >= 4 and 'observed' not in skip:
            for h in sorted(h for h in holiday if h.weekday() in weekend):
                d = h + timedelta(days=1)
                while d.weekday() in weekend or ('observed_cascade' not in skip and (d in holiday or d in observed)):
                    d += timedelta(days=1)
                observed.add(d)
        off = holiday | observed
        return lambda d: d.weekday() not in weekend and d not in off

    def solve(self, level, params, args, skip=frozenset()):
        start, n, holidays = date.fromisoformat(args[0]), args[1], args[2]
        weekend = set(args[3]) if level >= 4 else {5, 6}
        roll = args[4] if level == 5 and 'roll' not in skip else 'following'
        working = self.calendar(level, holidays, weekend, start.year, skip)

        def walk(d, step):
            d += timedelta(days=step)
            while not working(d):
                d += timedelta(days=step)
            return d
        if n == 0:
            if working(start) or 'zero_roll' in skip:
                return start.isoformat()
            first = 1 if roll in ('following', 'modified_following') else -1
            d = walk(start, first)
            if roll.startswith('modified') and 'modified_month' not in skip and (d.year, d.month) != (start.year, start.month):
                d = walk(start, -first)
            return d.isoformat()
        if n < 0 and 'negative' in skip:
            return start.isoformat()
        d = start
        for _ in range(abs(n)):
            d = walk(d, 1 if n > 0 else -1)
        return d.isoformat()

    def random_case(self, rng, level, params):
        year = rng.choice((2024, 2025, 2026, 2027, 2028))
        anchors = [date(year, 1, 1), date(year, 3, 1), date(year, 12, 31), date(year, 6, 30),
                   date(year, rng.randint(1, 12), rng.randint(1, 28))]
        start = rng.choice(anchors) + timedelta(days=rng.randint(-6, 6))
        n = rng.choice((0, 0, 0, rng.randint(-8, 8), rng.randint(-8, 8), rng.randint(-3, 3)))
        holidays = []
        for _ in range(rng.randint(0, 7)):
            d = start + timedelta(days=rng.randint(-12, 12))
            if level >= 3 and rng.random() < .35:
                holidays.append(d.strftime('%m-%d'))
            else:
                holidays.append(d.isoformat())
        if level >= 3 and rng.random() < .2:
            holidays.append('02-29')
            start = date(rng.choice((2024, 2025, 2027, 2028)), 2, 26) + timedelta(days=rng.randint(0, 5))
        if holidays and rng.random() < .2:
            holidays.append(rng.choice(holidays))
        args = [start.isoformat(), n, holidays]
        if level >= 4:
            weekend = rng.choice(([5, 6], [5, 6], [4, 5], [6], [], [0, 6], rng.sample(range(7), rng.randint(0, 5))))
            if weekend and rng.random() < .2:
                weekend = weekend + [weekend[0]]
            args.append(list(weekend))
        if level == 5:
            args.append(rng.choice(('following', 'preceding', 'modified_following', 'modified_preceding')))
        return args

    def public_case(self, rng, level, params):
        start = date(2026, rng.randint(2, 11), rng.randint(5, 20))
        n = rng.randint(3, 12)
        holidays = sorted({(start + timedelta(days=rng.randint(1, 14))).isoformat() for _ in range(rng.randint(1, 2))})
        args = [start.isoformat(), n, holidays]
        if level >= 4:
            args.append(rng.choice(([5, 6], [4, 5])))
        if level == 5:
            args.append(rng.choice(('following', 'preceding', 'modified_following')))
        return args


# ---- C：類 CSV 解析（引號、跳脫、換行、註解、表頭與型別）----

C_DELIMITERS = (',', ';', '|')
C_EDGES = {
    1: ('blank_line', 'trailing_delimiter', 'empty_field'),
    2: ('quoted_delimiter', 'quoted_newline', 'doubled_quote', 'mid_quote', 'unterminated', 'quoted_whitespace',
        'space_around_quotes', 'quoted_empty'),
    3: ('crlf', 'cr', 'comment', 'hash_not_first', 'comment_with_quote'),
    4: ('escape_delimiter', 'escape_newline', 'escape_quote', 'escape_backslash', 'escape_space_edge',
        'escape_eof', 'escape_hash'),
    5: ('pad_missing', 'extra_fields', 'leading_zero', 'neg_zero', 'quoted_number', 'empty_none', 'bool_like',
        'escaped_number', 'header_only'),
}
C_INT = re.compile(r'-?(0|[1-9][0-9]*)')


class Records(Family):
    letter, name, entry = 'C', 'record_parsing', 'parse_records'

    def params(self, rng, level):
        return {'delimiter': rng.choice(C_DELIMITERS)}

    def signature(self, level):
        return 'parse_records(text)'

    def rules(self, level, params):
        d = params['delimiter']
        breaks = 'a line feed "\\n"' if level <= 2 else '"\\r\\n", a lone "\\r" or a lone "\\n"'
        rules = [f'`text` is a string. Records are separated by line breaks; a line break is {breaks}. Fields '
                 f'inside a record are separated by the delimiter {d!r}.',
                 'Before a record starts, a line that is empty or contains only spaces and tabs is skipped entirely '
                 '(it produces no record). A line break at the very end of the text does not create an extra '
                 'record. If no records remain, return [].',
                 f'A record that ends with the delimiter has a final empty field: "a{d}b{d}" has the fields "a", "b" '
                 f'and "". Two delimiters in a row produce an empty field.',
                 'Spaces and tabs at the beginning and end of an unquoted field are removed; whitespace inside the '
                 'field is kept.']
        if level >= 2:
            rules.append(
                'Quoted fields: if the first character of a field that is not a space or tab is a double quote ", '
                'the field is quoted. Its value is everything up to the matching closing quote, kept exactly as '
                'written (including spaces, tabs, delimiters and line breaks). Inside a quoted field, two double '
                'quotes "" stand for one literal ". Spaces and tabs before the opening quote and after the closing '
                'quote are ignored; nothing else ever appears between a closing quote and the next delimiter, line '
                'break or end of text. A double quote anywhere else in an unquoted field is an ordinary character. '
                'If a quoted field is never closed, it extends to the end of the text.')
        if level >= 3:
            rules.append(
                'Comment lines: when a record would start and the very first character of the line is "#", the '
                'whole line up to the next line break is skipped, whatever it contains (quotes and delimiters in a '
                'comment line have no effect). A "#" that is not the first character of the line (for example after '
                'a space) is ordinary data.')
        if level >= 4:
            rules.append(
                'Escapes: a backslash \\ makes the next character (any character, including the delimiter, ", \\, '
                'space, tab, "#", "\\r" and "\\n") an ordinary character of the field, in quoted and unquoted '
                'fields alike. An escaped character never ends a field or record, never closes a quote, and an '
                'escaped space or tab is never removed by trimming. A field that starts with \\" is unquoted. A '
                'line whose first character is \\ is not a comment line. A backslash that is the very last '
                'character of the text is a literal backslash. Blank-line and comment-line detection look only at '
                'the raw characters of the line.')
        if level <= 4:
            rules.append('Return a list of records, each a list of field strings.')
        else:
            rules.append(
                'The first record is the header: a list of field names (strings, all distinct and never "_extra"). '
                'Return one dict per remaining record, in order. The i-th field of a record is stored under the '
                'i-th header name. If a record has fewer fields than the header, the missing names map to None. If '
                'it has more fields, the extra values are stored as a list under the key "_extra" (this key is '
                'absent otherwise). If there is only a header, return [].')
            rules.append(
                'Values: a field that was quoted or contains a backslash is always a string. Every other field (after '
                'trimming) is converted: the empty string becomes None; text matching the regular expression '
                '-?(0|[1-9][0-9]*) becomes an int (so "-0" becomes 0, but "007", "+5" and "1.0" stay strings); '
                'exactly "true" or "false" becomes a bool; everything else stays a string. Header names are never '
                'converted.')
        return rules

    def edge_names(self, level):
        return tuple(e for lv in range(1, level + 1) for e in C_EDGES[lv])

    def toggles(self, level):
        return self.edge_names(level)

    def edges(self, level, params, args):
        events = set()
        self.parse(level, params['delimiter'], args[0], events)
        return events & set(self.edge_names(level))

    def solve(self, level, params, args, skip=frozenset()):
        return self.parse(level, params['delimiter'], args[0], set())

    @staticmethod
    def _break(level, text, i):
        if i >= len(text):
            return 0
        if text[i] == '\n':
            return 1
        if level >= 3 and text[i] == '\r':
            return 2 if text[i + 1:i + 2] == '\n' else 1
        return 0

    def parse(self, level, d, text, events):
        n, i, records = len(text), 0, []
        brk = lambda j: self._break(level, text, j)
        while i < n:
            j = i
            while j < n and not brk(j):
                j += 1
            line = text[i:j]
            if line.strip(' \t') == '':
                events.add('blank_line')
                i = j + brk(j)
                continue
            if level >= 3 and text[i] == '#':
                events.add('comment')
                if '"' in line:
                    events.add('comment_with_quote')
                i = j + brk(j)
                continue
            if level >= 3 and line.lstrip(' \t').startswith('#') and not line.startswith('#'):
                events.add('hash_not_first')
            fields = []
            while True:
                field, i = self._field(level, d, text, i, events)
                fields.append(field)
                if i < n and text[i] == d:
                    i += 1
                    continue
                b = brk(i)
                if b == 2:
                    events.add('crlf')
                elif b == 1 and text[i] == '\r':
                    events.add('cr')
                i += b
                break
            if len(fields) > 1 and fields[-1] == ('', False, False):
                events.add('trailing_delimiter')
            if any(f == ('', False, False) for f in fields[:-1]):
                events.add('empty_field')
            records.append(fields)
        if level <= 4:
            return [[f[0] for f in r] for r in records]
        if len(records) == 1:
            events.add('header_only')
        if not records:
            return []
        header = [f[0] for f in records[0]]
        rows = []
        for record in records[1:]:
            values = [self._convert(f, events) for f in record]
            row = {h: (values[k] if k < len(values) else None) for k, h in enumerate(header)}
            if len(values) < len(header):
                events.add('pad_missing')
            if len(values) > len(header):
                events.add('extra_fields')
                row['_extra'] = values[len(header):]
            rows.append(row)
        return rows

    @staticmethod
    def _convert(field, events):
        text, quoted, escaped = field
        if quoted or escaped:
            if C_INT.fullmatch(text) or text in ('true', 'false'):
                events.add('quoted_number' if quoted else 'escaped_number')
            return text
        if text == '':
            events.add('empty_none')
            return None
        if C_INT.fullmatch(text):
            if text == '-0':
                events.add('neg_zero')
            return int(text)
        if re.fullmatch(r'-?0[0-9]+', text):
            events.add('leading_zero')
        if text in ('true', 'false'):
            return text == 'true'
        if text.lower() in ('true', 'false'):
            events.add('bool_like')
        return text

    def _field(self, level, d, text, i, events):
        """回傳 ((值, 是否引號欄位, 是否含反斜線), 下一個位置)。"""
        n = len(text)
        k = i
        while k < n and text[k] in ' \t':
            k += 1
        if level >= 2 and k < n and text[k] == '"':
            if k > i:
                events.add('space_around_quotes')
            i, chars, closed = k + 1, [], False
            while i < n:
                c = text[i]
                if level >= 4 and c == '\\':
                    if i + 1 < n:
                        self._escape_event(text[i + 1], d, events)
                        chars.append(text[i + 1])
                        i += 2
                    else:
                        events.add('escape_eof')
                        chars.append('\\')
                        i += 1
                    continue
                if c == '"':
                    if text[i + 1:i + 2] == '"':
                        events.add('doubled_quote')
                        chars.append('"')
                        i += 2
                        continue
                    closed = True
                    i += 1
                    break
                if c == d:
                    events.add('quoted_delimiter')
                elif c in '\r\n':
                    events.add('quoted_newline')
                elif c in ' \t':
                    events.add('quoted_whitespace')
                chars.append(c)
                i += 1
            if not closed:
                events.add('unterminated')
            after = i
            while i < n and text[i] in ' \t':
                i += 1
            if i > after:
                events.add('space_around_quotes')
            if not chars:
                events.add('quoted_empty')
            return (''.join(chars), True, False), i
        chars, escaped = [], False
        while i < n and text[i] != d and not self._break(level, text, i):
            c = text[i]
            if level >= 4 and c == '\\':
                escaped = True
                if i + 1 < n:
                    self._escape_event(text[i + 1], d, events)
                    chars.append((text[i + 1], True))
                    i += 2
                else:
                    events.add('escape_eof')
                    chars.append(('\\', True))
                    i += 1
                continue
            if level >= 2 and c == '"':
                events.add('mid_quote')
            chars.append((c, False))
            i += 1
        lo, hi = 0, len(chars)
        while lo < hi and not chars[lo][1] and chars[lo][0] in ' \t':
            lo += 1
        while hi > lo and not chars[hi - 1][1] and chars[hi - 1][0] in ' \t':
            hi -= 1
        if (lo < hi and chars[lo][1] and chars[lo][0] in ' \t') or (hi > lo and chars[hi - 1][1] and chars[hi - 1][0] in ' \t'):
            events.add('escape_space_edge')
        if lo < hi and chars[lo] == ('#', True) and lo == 0:
            events.add('escape_hash')
        return (''.join(c for c, _ in chars[lo:hi]), False, escaped), i

    @staticmethod
    def _escape_event(c, d, events):
        if c == d:
            events.add('escape_delimiter')
        elif c in '\r\n':
            events.add('escape_newline')
        elif c == '"':
            events.add('escape_quote')
        elif c == '\\':
            events.add('escape_backslash')

    def _text(self, rng, level, d, plain_only):
        """依難度隨機組出原始文字；期望值一律由參考解析器算出。"""
        words = ['alpha', 'b', 'x y', 'Mo', 'k9', 'z', 'pear', '12', 'data'] + ([] if plain_only else [''])
        if level >= 5:
            words += ['0', '42', '-7', '-0', '007', '+5', '1.0', 'true', 'false', 'True', 'FALSE']
        breaks = ['\n'] if level <= 2 or plain_only else ['\n', '\n', '\r\n', '\r']
        specials = [d, '\n', '"', ' ', 'q'] + (['\r', '\\', '#', '\t'] if level >= 3 else [])

        def esc(c):
            return '\\' + c

        def plain():
            word = rng.choice(words)
            if plain_only:
                return rng.choice(('', ' ')) + word + rng.choice(('', ' '))
            pad = lambda: rng.choice(('', '', ' ', '\t', '  '))
            if level >= 5 and rng.random() < .1:
                return pad() + esc(rng.choice('0123456789')) + rng.choice(('', '2', '0')) + pad()
            if level >= 2 and rng.random() < .1:
                word = word[:1] + '"' + word[1:] if word else 'a"b'
            if level >= 4 and rng.random() < .35:
                c = rng.choice([d, '"', '\\', ' ', '\n', '#', 'r', '\t'])
                where = rng.choice(('start', 'end', 'mid'))
                word = esc(c) + word if where == 'start' else word + esc(c) if where == 'end' else word[:1] + esc(c) + word[1:]
            return pad() + word + pad()

        def quoted():
            if plain_only:
                return '"' + rng.choice(words[:5]) + '"'
            if level >= 5 and rng.random() < .25:
                return '"' + rng.choice(('42', '-0', '0', 'true', '7')) + '"'
            content = ''.join(rng.choice(specials + ['a', 'b', 'c']) for _ in range(rng.randint(0, 5)))
            body = ''
            for c in content:
                if c == '"':
                    body += '\\"' if level >= 4 and rng.random() < .4 else '""'
                elif c == '\\':
                    body += '\\\\' if level >= 4 else '\\'
                elif c == '\r' and level <= 2:
                    body += 'r'
                elif level >= 4 and rng.random() < .15:
                    body += esc(c)
                else:
                    body += c
            pad = lambda: rng.choice(('', '', ' ', '\t '))
            return pad() + '"' + body + '"' + pad()

        lines = []
        if level >= 5:
            names = rng.sample(['id', 'name', 'qty', 'note', 'x y', 'tag', 'k9', 'when'], rng.randint(1, 4))
            lines.append(d.join(rng.choice(('', ' ')) + ('"' + n + '"' if rng.random() < .3 else n) for n in names))
        for r in range(rng.randint(1, 5) if not plain_only else rng.randint(2, 4)):
            if not plain_only and rng.random() < .2:
                lines.append(rng.choice(('', ' ', '\t', '  \t')))
            if not plain_only and level >= 3 and rng.random() < .2:
                lines.append('#' + rng.choice(('note', ' a"b', f'x{d}y', '"open')))
            if not plain_only and level >= 3 and rng.random() < .1:
                lines.append(' #' + rng.choice(words))
            fields = []
            width = rng.randint(1, 4) if not plain_only else 3
            for _ in range(width):
                if level >= 2 and rng.random() < (.3 if plain_only else .35):
                    fields.append(quoted())
                else:
                    fields.append(plain())
            if not plain_only and rng.random() < .15:
                fields.append('')
            line = d.join(fields)
            if level >= 4 and not plain_only and rng.random() < .1:
                line = '\\#' + line
            lines.append(line)
        text = ''
        for k, line in enumerate(lines):
            text += line
            if k < len(lines) - 1:
                text += rng.choice(breaks)
        if not plain_only:
            tail = rng.random()
            if tail < .3:
                text += rng.choice(breaks)
            elif level >= 2 and tail < .38:
                text += d + '"never closed' + rng.choice(('', ' ', '\n'))
            elif level >= 4 and tail < .45:
                text += '\\'
        return text

    def random_case(self, rng, level, params):
        if rng.random() < .03:
            return [rng.choice(('', '\n', ' \n\t'))]
        return [self._text(rng, level, params['delimiter'], False)]

    def public_case(self, rng, level, params):
        return [self._text(rng, level, params['delimiter'], True)]

    def plain_enough(self, level, params, args, expected):
        return len(expected) >= (2 if level <= 4 else 1)


# ---- D：金額依權重分攤到分（最大餘數法、平手規則、負數、上限、最小單位）----

class Allocation(Family):
    letter, name, entry = 'D', 'cent_allocation', 'allocate'

    def signature(self, level):
        return {1: 'allocate(total, weights)', 2: 'allocate(total, weights)', 3: 'allocate(total, weights)',
                4: 'allocate(total, weights, caps)', 5: 'allocate(total, weights, caps, unit)'}[level]

    def rules(self, level, params):
        rules = []
        if level == 1:
            rules.append('`total` is a non-negative integer number of cents and `weights` is a non-empty list of '
                         'positive integers. Return a list of integers (cents), one per weight, in the same order, '
                         'summing to total.')
        else:
            rules.append('`total` is an integer number of cents' + (' (never negative)' if level == 2 else
                         ' (it may be negative)') + ' and `weights` is a list of non-negative integers (it may be '
                         'empty and may contain zeros).')
        rules.append('Largest remainder method for an amount A >= 0 and weights w with W = sum(w) > 0: recipient i '
                     'first gets floor(A * w[i] / W) cents. The cents still missing are then given out one cent '
                     'each to the recipients with the largest exact remainders A * w[i] / W - floor(A * w[i] / W), '
                     'compared exactly (no floating point). No recipient gets more than one extra cent.')
        if level == 1:
            rules.append('Ties between equal remainders go to the lower index first.')
            rules.append('The answer is the largest remainder method applied to total and weights.')
            return rules
        rules.append('Ties between equal remainders: the larger weight first; if the weights are also equal, the '
                     'lower index first.')
        rules.append('If W = 0 (all weights are zero), the method gives every recipient 0 and distributes nothing. '
                     'An empty weights list gives [].')
        if level == 2:
            rules.append('Return the largest remainder method applied to total and weights.')
            return rules
        if level == 3:
            rules.append('Return the method applied to A = |total|, with every share negated when total < 0.')
            return rules
        caps_rule = ('`caps` has one entry per weight: None (no cap) or a non-negative integer bounding the '
                     'absolute value of that recipient\'s share. Capped allocation of an amount R >= 0: all '
                     'recipients start open. Repeat: split R among the open recipients with the largest remainder '
                     'method (using only their weights). If no open recipient gets more than its cap, every open '
                     'recipient receives that tentative share and the procedure ends. Otherwise every open recipient '
                     'whose tentative share exceeds its cap receives exactly its cap and is closed, those caps are '
                     'subtracted from R, and the loop repeats with the remaining open recipients. If no recipient is '
                     'open, the loop ends with nothing more distributed.')
        rules.append(caps_rule)
        if level == 4:
            rules.append('Let A = |total|. Apply the capped allocation to A. Return {"shares": [...], '
                         '"unallocated": U} where U = A - sum(shares). If total < 0, negate every share and U.')
            return rules
        rules.append('`unit` is a positive integer. Let A = |total|, q = A // unit and r = A % unit. Apply the capped '
                     'allocation to q (counting in whole units) where each cap becomes floor(cap / unit) units, then '
                     'multiply every share by unit. Then the r leftover cents (if r > 0) go to a single recipient: '
                     'the one with the largest weight (ties: lower index) among the recipients with weight > 0 whose '
                     'share plus r does not exceed its cap (None = no cap). If there is no such recipient, the r cents '
                     'stay unallocated.')
        rules.append('Return {"shares": [...], "unallocated": U} where U = A - sum(shares). If total < 0, negate every '
                     'share and U.')
        return rules

    def toggles(self, level):
        if level == 1:
            return ('tie_index',)
        out = ('tie_index', 'tie_weight', 'all_zero')
        if level >= 3:
            out += ('negative',)
        if level >= 4:
            out += ('cap_second_round',)
        if level == 5:
            out += ('unit_cap_floor', 'remainder_cap')
        return out

    @staticmethod
    def lrm(amount, weights, level, skip):
        total_w = sum(weights)
        if total_w == 0:
            if 'all_zero' in skip and weights:
                weights, total_w = [1] * len(weights), len(weights)
            else:
                return [0] * len(weights)
        base = [amount * w // total_w for w in weights]
        rem = [amount * w % total_w for w in weights]
        by_weight = level >= 2 and 'tie_weight' not in skip
        order = sorted(range(len(weights)), key=lambda i: (-rem[i], -weights[i] if by_weight else 0,
                                                           -i if 'tie_index' in skip else i))
        for i in order[:amount - sum(base)]:
            base[i] += 1
        return base

    def capped(self, amount, weights, caps, level, skip):
        shares = [0] * len(weights)
        open_ = list(range(len(weights)))
        remaining, rounds = amount, 0
        while open_:
            rounds += 1
            tentative = self.lrm(remaining, [weights[i] for i in open_], level, skip)
            over = [(i, t) for i, t in zip(open_, tentative) if caps[i] is not None and t > caps[i]]
            # 漏掉「重分之後要再檢查上限」：第二輪起不再檢查，直接收下暫定分額。
            if not over or (rounds >= 2 and 'cap_second_round' in skip):
                for i, t in zip(open_, tentative):
                    shares[i] = t
                break
            for i, _ in over:
                shares[i] = caps[i]
                remaining -= caps[i]
                open_.remove(i)
        return shares

    def solve(self, level, params, args, skip=frozenset()):
        total, weights = args[0], args[1]
        if level <= 2:
            return self.lrm(total, weights, level, skip)
        sign = -1 if total < 0 else 1
        amount = abs(total)
        if level == 3:
            if total < 0 and 'negative' in skip:
                return self._floor_negative(total, weights, skip)
            return [sign * s if s else 0 for s in self.lrm(amount, weights, level, skip)]
        caps = args[2]
        if level == 4:
            shares = self.capped(amount, weights, caps, level, skip)
        else:
            unit = args[3]
            q, r = divmod(amount, unit)
            unit_caps = [None if c is None else (-(-c // unit) if 'unit_cap_floor' in skip else c // unit) for c in caps]
            shares = [s * unit for s in self.capped(q, weights, unit_caps, level, skip)]
            if r:
                fits = [i for i, w in enumerate(weights) if w > 0 and
                        (caps[i] is None or 'remainder_cap' in skip or shares[i] + r <= caps[i])]
                if fits:
                    shares[max(fits, key=lambda i: (weights[i], -i))] += r
        unallocated = amount - sum(shares)
        # 漏掉負數條款時，未分配金額忘了跟著變號。
        left_sign = 1 if 'negative' in skip else sign
        return {'shares': [sign * s if s else 0 for s in shares],
                'unallocated': left_sign * unallocated if unallocated else 0}

    def _floor_negative(self, total, weights, skip):
        total_w = sum(weights)
        if total_w == 0:
            return [0] * len(weights)
        base = [total * w // total_w for w in weights]
        rem = [total * w - b * total_w for w, b in zip(weights, base)]
        order = sorted(range(len(weights)), key=lambda i: (-rem[i], -weights[i], i))
        for i in order[:total - sum(base)]:
            base[i] += 1
        return base

    def random_case(self, rng, level, params):
        count = rng.randint(1, 5) if level == 1 else rng.randint(0, 5)
        low = 1 if level == 1 else 0
        weights = [rng.choice((low, 1, 1, 2, 2, 3, 4, 6)) if rng.random() < .8 else rng.randint(low, 9)
                   for _ in range(count)]
        if level >= 2 and weights and rng.random() < .1:
            weights = [0] * len(weights)
        if rng.random() < .3 and weights:
            weights = [weights[0]] * len(weights)
        total = rng.choice((rng.randint(0, 30), rng.randint(0, 200), rng.randint(0, 9) * 10 + rng.randint(0, 3)))
        if level >= 3 and rng.random() < .45:
            total = -total
        args = [total, weights]
        if level >= 4:
            caps = [None if rng.random() < .4 else rng.randint(0, max(1, abs(total) // max(1, len(weights))) + 3)
                    for _ in weights]
            args.append(caps)
        if level == 5:
            args.append(rng.choice((1, 2, 5, 5, 10, 25)))
        return args

    def public_case(self, rng, level, params):
        count = rng.randint(3, 4)
        weights = rng.sample(range(1, 9), count)
        total = rng.randint(50, 999)
        args = [total, weights]
        if level >= 4:
            capped = rng.randrange(count)
            fair = total * weights[capped] // sum(weights)
            args.append([fair // 2 if k == capped else None for k in range(count)])
        if level == 5:
            args.append(rng.choice((5, 10)))
        return args


# ---- E：版本字串排序（預發行版本、建置資訊、無效字串、降冪穩定）----

E_STRICT = re.compile(r' *[vV]?([0-9]+(?:\.[0-9]+)*)(?:-([0-9A-Za-z.-]*))?(?:\+([0-9A-Za-z.-]*))? *')


class Versions(Family):
    letter, name, entry = 'E', 'version_ordering', 'sort_versions'

    def signature(self, level):
        return 'sort_versions(versions, descending)' if level == 5 else 'sort_versions(versions)'

    def rules(self, level, params):
        rules = []
        if level == 1:
            rules.append('`versions` is a list of strings. Each one consists of 1 to 4 non-negative integers '
                         'separated by dots, e.g. "1.2", "10.0.3", "2.07" (leading zeros may occur).')
        elif level == 2:
            rules.append('`versions` is a list of strings of the form CORE or CORE-PRE. CORE consists of 1 to 4 '
                         'non-negative integers separated by dots (leading zeros may occur). PRE is a pre-release '
                         'tag: one or more identifiers separated by dots, each made of ASCII letters and digits.')
        else:
            rules.append('A valid version string has the form: optional spaces, optionally "v" or "V", CORE, '
                         'optionally "-" followed by PRE, optionally "+" followed by BUILD, optional spaces. CORE is 1 '
                         'to 4 non-negative integers (digits only, leading zeros allowed) separated by single dots. '
                         'PRE and BUILD are each one or more non-empty identifiers separated by single dots; an '
                         'identifier consists of ASCII letters, digits and hyphens "-". Only the first "-" after CORE '
                         'starts PRE; later hyphens belong to identifiers (in "1.0-rc-1.x" PRE has the identifiers '
                         '"rc-1" and "x").')
            if level == 3:
                rules.append('Every string in `versions` is valid.')
        rules.append('Comparing cores: compare the integers from left to right by numeric value; a missing '
                     'component counts as 0, so "1.2", "1.2.0" and "1.02.0.0" are equal.')
        if level >= 2:
            rules.append('If the cores are equal, a version with PRE is lower than the same version without PRE. If '
                         'both have PRE, compare identifiers from left to right: identifiers consisting only of '
                         'digits compare by numeric value ("01" equals "1"); other identifiers compare by ASCII '
                         'string order; an all-digit identifier is lower than any other identifier. If all '
                         'identifiers of the shorter list equal the start of the longer list, the shorter list is '
                         'lower ("alpha" < "alpha.1").')
        if level >= 3:
            rules.append('BUILD, the "v"/"V" prefix and surrounding spaces never affect the order. The output contains '
                         'the original strings unchanged.')
        if level >= 4:
            rules.append('Strings that are not valid (wrong characters, empty identifiers, 5 or more core '
                         'components, an empty string, ...) are placed after all valid strings, in their original '
                         'input order.')
        order = 'ascending order' if level < 5 else ('ascending order, or descending order when `descending` is '
                                                      'True')
        rules.append(f'Return a new list with the {"valid " if level >= 4 else ""}versions sorted in {order}. '
                     'Versions that compare equal keep their original relative order from the input.'
                     + (' This also holds in descending order, and invalid strings stay last in input order.'
                        if level == 5 else ''))
        return rules

    def toggles(self, level):
        out = ('missing_zero', 'stable')
        if level >= 2:
            out += ('pre_numeric', 'pre_num_lt_alpha', 'pre_shorter')
        if level >= 3:
            out += ('build_ignored', 'pre_hyphen')
        if level >= 4:
            out += ('invalid_last', 'too_many', 'empty_ident')
        if level == 5:
            out += ('desc_stable', 'desc_invalid')
        return out

    def parse(self, level, text, skip):
        m = E_STRICT.fullmatch(text)
        if not m:
            return None
        core, pre, build = m.group(1), m.group(2), m.group(3)
        if level <= 2 and (text != text.strip() or text[:1] in 'vV' or build is not None):
            return None
        parts = core.split('.')
        if len(parts) > 4 and 'too_many' not in skip:
            return None
        pre_ids = None
        if pre is not None:
            pre_ids = re.split(r'[.-]', pre) if 'pre_hyphen' in skip else pre.split('.')
            if any(not p for p in pre_ids) and 'empty_ident' not in skip:
                return None
        if build is not None and any(not b for b in build.split('.')) and 'empty_ident' not in skip:
            return None
        return [int(p) for p in parts], pre_ids, build

    def compare(self, a, b, skip):
        ca, cb = list(a[0]), list(b[0])
        if 'missing_zero' not in skip:
            width = max(len(ca), len(cb))
            ca += [0] * (width - len(ca))
            cb += [0] * (width - len(cb))
        if ca != cb:
            return -1 if ca < cb else 1
        pa, pb = a[1], b[1]
        if pa is None or pb is None:
            if pa is not pb:
                return 1 if pa is None else -1
        else:
            for x, y in zip(pa, pb):
                dx, dy = x.isdigit(), y.isdigit()
                if dx and dy and 'pre_numeric' not in skip:
                    if int(x) != int(y):
                        return -1 if int(x) < int(y) else 1
                elif dx != dy:
                    low = -1 if dx else 1
                    return -low if 'pre_num_lt_alpha' in skip else low
                elif x != y:
                    return -1 if x < y else 1
            if len(pa) != len(pb):
                shorter = -1 if len(pa) < len(pb) else 1
                return -shorter if 'pre_shorter' in skip else shorter
        if 'build_ignored' in skip and a[2] != b[2]:
            ba, bb = a[2] or '', b[2] or ''
            return -1 if ba < bb else 1 if ba > bb else 0
        return 0

    def solve(self, level, params, args, skip=frozenset()):
        versions = args[0]
        descending = level == 5 and args[1]
        valid, invalid = [], []
        for v in versions:
            parsed = self.parse(level, v, skip)
            (valid if parsed is not None else invalid).append((v, parsed))
        key = cmp_to_key(lambda x, y: self.compare(x[1], y[1], skip) or
                         ((x[0] > y[0]) - (x[0] < y[0]) if 'stable' in skip else 0))
        if descending and 'desc_stable' in skip:
            ordered = list(reversed(sorted(valid, key=key)))
        else:
            ordered = sorted(valid, key=key, reverse=bool(descending))
        out = [v for v, _ in ordered]
        tail = [v for v, _ in invalid] if 'invalid_last' not in skip else []
        if descending and 'desc_invalid' in skip:
            return list(reversed(tail)) + out
        return out + tail

    def random_case(self, rng, level, params):
        cores = ['1', '1.0', '1.0.0', '1.00', '1.2', '1.2.0', '1.10', '1.9', '2', '2.0.1', '01.2', '0.9.9.9', '1.2.0.0']
        pres = ['alpha', 'alpha.1', 'alpha.beta', 'alpha.2', 'alpha.10', 'beta', 'beta.2', 'beta.11', 'rc.1', '1', '01',
                '2', 'x.7', 'x.7z', 'alpha.1.1', 'Alpha', 'a1']
        if level >= 3:
            pres += ['rc-1', 'rc-1.x', 'alpha-beta', 'rc', 'rc.1-2']
        builds = ['b1', '001', 'exp.sha.5', 'build-7']
        invalid = ['', '1..2', '1.2.', '1.2-', '1.2-a..b', '1.2+', 'x1.2', 'v 1.2', '1.2.3.4.5', '1.2-a_b', '1.2 3',
                   '.1', 'vv1', '1.2+b..c', '1.0-rc!']
        out = []
        for _ in range(rng.randint(2, 8)):
            if level >= 4 and rng.random() < .2:
                out.append(rng.choice(invalid))
                continue
            text = rng.choice(cores)
            if level >= 2 and rng.random() < .55:
                text += '-' + rng.choice(pres)
            if level >= 3 and rng.random() < .3:
                text += '+' + rng.choice(builds)
            if level >= 3 and rng.random() < .25:
                text = rng.choice('vV') + text
            if level >= 3 and rng.random() < .15:
                text = ' ' + text + rng.choice(('', ' '))
            out.append(text)
        if out and rng.random() < .3:
            out.insert(rng.randrange(len(out) + 1), rng.choice(out))
        return [out] + ([rng.random() < .6] if level == 5 else [])

    def public_case(self, rng, level, params):
        out = set()
        while len(out) < rng.randint(4, 5):
            text = '.'.join(str(rng.randint(0, 12)) for _ in range(3))
            if level >= 2 and rng.random() < .4:
                text += '-' + rng.choice(('alpha', 'beta', 'rc'))
            out.add(text)
        out = sorted(out)
        rng.shuffle(out)
        return [out] + ([rng.random() < .5] if level == 5 else [])


# ---- F：滑動視窗限流（視窗邊界、被拒不計、亂序、成本、全域上限、冷卻）----

class RateLimit(Family):
    letter, name, entry = 'F', 'sliding_window_limit', 'rate_limit'

    def signature(self, level):
        return {1: 'rate_limit(times, limit, window)', 2: 'rate_limit(events, limit, window)',
                3: 'rate_limit(events, limit, window)', 4: 'rate_limit(events, limit, window, global_limit)',
                5: 'rate_limit(events, limit, window, global_limit, cooldown)'}[level]

    def rules(self, level, params):
        if level == 1:
            rules = ['`times` is a list of integer timestamps (milliseconds) in non-decreasing order, one per request. '
                     '`limit` is a non-negative integer and `window` a positive integer. Requests are decided one '
                     'after another in list order.']
        else:
            fmt = '[t, key]' if level == 2 else '[t, key, cost]'
            rules = [f'`events` is a list of requests {fmt}: an integer timestamp t (milliseconds), a string key'
                     + (' and a non-negative integer cost' if level >= 3 else '') + '. `limit` is a non-negative '
                     'integer and `window` a positive integer. The list is NOT necessarily sorted. Requests are '
                     'decided in order of increasing t; requests with equal t are decided in list order.']
        rules.append('The window of a request at time t contains the earlier-decided ALLOWED requests whose '
                     'timestamp s satisfies t - window < s <= t. A request exactly `window` milliseconds older is '
                     'no longer in the window. Rejected requests never count for later decisions.')
        if level == 1:
            rules.append('A request is allowed if fewer than `limit` requests are in its window; otherwise it is '
                         'rejected.')
            rules.append('Return a list of booleans (True = allowed), one per request, in input order.')
            return rules
        if level == 2:
            rules.append('Each key has its own independent window: a request is allowed if fewer than `limit` '
                         'requests with the same key are in its window.')
        else:
            rules.append('Each key has its own independent window. A request with cost c is allowed by its key if '
                         '(sum of the costs of same-key requests in its window) + c <= limit. A request with cost 0 is '
                         'therefore always allowed by its key.')
        if level >= 4:
            rules.append('`global_limit` is a non-negative integer. A request is allowed by the global check if (sum '
                         'of the costs of ALL requests, of any key, in its window) + c <= global_limit. A request is '
                         'allowed only if both its key check and the global check allow it. A rejected request '
                         'counts for neither check later.')
        if level == 5:
            rules.append('`cooldown` is a non-negative integer. If a request with key k at time t fails its key '
                         'check (whatever the global check says), key k is blocked: every later-decided request with '
                         'key k and timestamp < t + cooldown is rejected without any other check. Those blocked '
                         'rejections do not extend or restart the block. A request rejected only by the global check '
                         'does not block its key.')
        rules.append('Return a list of booleans (True = allowed), one per request, in the original input order.')
        return rules

    def toggles(self, level):
        out = ('window_boundary', 'rejected_not_counted')
        if level >= 2:
            out += ('sort_order',)
        if level >= 3:
            out += ('cost_le',)
        if level >= 4:
            out += ('global_consume',)
        if level == 5:
            out += ('cooldown_boundary', 'cooldown_extend', 'cooldown_global')
        return out

    def solve(self, level, params, args, skip=frozenset()):
        if level == 1:
            events = [(t, '', 1, i) for i, t in enumerate(args[0])]
        else:
            events = [(e[0], e[1], e[2] if level >= 3 else 1, i) for i, e in enumerate(args[0])]
        limit, window = args[1], args[2]
        global_limit = args[3] if level >= 4 else None
        cooldown = args[4] if level == 5 else None
        order = events if level == 1 or 'sort_order' in skip else sorted(events, key=lambda e: (e[0], e[3]))
        history, everything, blocked = {}, [], {}
        result = [None] * len(events)

        def used(items, t):
            if 'window_boundary' in skip:
                return sum(c for s, c in items if t - window <= s <= t)
            return sum(c for s, c in items if t - window < s <= t)
        for t, key, cost, i in order:
            if key in blocked:
                until = blocked[key]
                if t < until or (t == until and 'cooldown_boundary' in skip):
                    result[i] = False
                    if 'cooldown_extend' in skip:
                        blocked[key] = t + cooldown
                    continue
            mine = history.setdefault(key, [])
            if level >= 3:
                key_ok = used(mine, t) + cost < limit if 'cost_le' in skip else used(mine, t) + cost <= limit
            else:
                key_ok = used(mine, t) < limit
            global_ok = global_limit is None or used(everything, t) + cost <= global_limit
            result[i] = key_ok and global_ok
            if result[i] or 'rejected_not_counted' in skip:
                mine.append((t, cost))
                everything.append((t, cost))
            elif key_ok and 'global_consume' in skip:
                mine.append((t, cost))
            if level == 5 and (not key_ok or (not global_ok and 'cooldown_global' in skip)):
                blocked[key] = t + cooldown
        return result

    def random_case(self, rng, level, params):
        window = rng.choice((5, 10, 10, 20))
        limit = rng.choice((0, 1, 2, 2, 3, 3, 4)) if level < 3 else rng.choice((0, 2, 3, 4, 5, 6))
        count = rng.randint(4, 12)
        times, t = [], rng.randint(0, 5)
        for _ in range(count):
            t += rng.choice((0, 0, 1, 2, 3, window, window, window // 2, window - 1, window + 1))
            times.append(t)
        if level == 1:
            return [times, limit, window]
        keys = rng.choice((['a'], ['a', 'b'], ['a', 'b', 'c']))
        events = []
        for t in times:
            e = [t, rng.choice(keys)]
            if level >= 3:
                e.append(rng.choice((0, 1, 1, 1, 2, 2, 3, limit, limit + 1)))
            events.append(e)
        if rng.random() < .6:
            rng.shuffle(events)
        args = [events, limit, window]
        if level >= 4:
            args.append(rng.choice((limit, limit + 1, limit * 2, 2 * limit + 1, 3)))
        if level == 5:
            args.append(rng.choice((0, window // 2, window, window + 3)))
        return args

    def public_case(self, rng, level, params):
        window = rng.choice((10, 100))
        limit = rng.randint(2, 3) if level < 3 else rng.randint(3, 5)
        times = sorted(rng.sample(range(0, 2 * window), rng.randint(5, 7)))
        if level == 1:
            return [times, limit, window]
        events = []
        for t in times:
            e = [t, rng.choice(('a', 'b'))]
            if level >= 3:
                e.append(rng.randint(1, 3))
            events.append(e)
        args = [events, limit, window]
        if level >= 4:
            args.append(2 * limit + 3)
        if level == 5:
            args.append(rng.choice((1, 2)))
        return args

    def plain_enough(self, level, params, args, expected):
        return False in expected and True in expected


# ---- G：庫存帳（先進先出成本、拒絕超賣、部分成交、退貨、盤點調整、取消）----

class Inventory(Family):
    letter, name, entry = 'G', 'fifo_inventory', 'process'

    def signature(self, level):
        return 'process(ops)'

    def rules(self, level, params):
        if level <= 2:
            ops = ('`ops` is a list of operations: ["buy", qty, unit_cost] or ["sell", qty]'
                   + (' or ["sell", qty, partial] where partial is a bool (a missing partial means False)'
                      if level == 2 else '') + '. qty and unit_cost are non-negative integers.')
        else:
            ops = ('`ops` is a list of operations: ["buy", sku, qty, unit_cost], ["sell", sku, qty] or ["sell", '
                   'sku, qty, partial] (missing partial means False), ["return", sku, qty]'
                   + (', ["adjust", sku, new_qty]' if level >= 4 else '')
                   + (', ["cancel", j]' if level == 5 else '')
                   + '. sku is a string; qty, new_qty and unit_cost are non-negative integers. Each sku has its own '
                   'independent stock.')
        rules = [ops,
                 'Stock is a first-in-first-out queue of units, each with its unit cost. "buy" appends qty units at the '
                 'end of the queue. Operations are processed in order; index i refers to ops[i].',
                 '"sell" removes qty units from the front of the queue; their costs are added to cogs (cost of goods '
                 'sold). If fewer than qty units are in stock, the sale is rejected entirely: nothing changes and its '
                 'index is appended to "rejected".' + (' With partial True, the sale is never rejected: it removes '
                                                       'every unit it can (up to qty) and, if it was short by u > 0 '
                                                       'units, [index, u] is appended to "shortfalls".'
                                                       if level >= 2 else ''),
                 'An operation with qty 0 changes nothing and is never rejected.']
        if level >= 3:
            rules.append('"return" takes back the qty units of that sku that were sold most recently and not yet '
                         'returned. They go back to the FRONT of the queue, keeping the order in which they had been '
                         'sold (the earliest-sold of them first), and their costs are subtracted from cogs. If fewer '
                         'than qty such units exist, the return is rejected entirely (index appended to "rejected").')
        if level >= 4:
            rules.append('"adjust" sets the number of units in stock of that sku to new_qty. If units must be removed, '
                         'they are removed from the END of the queue (newest first) and their costs are added to '
                         '"shrinkage" (not to cogs). If units must be added, they are appended at the end with the '
                         'unit cost of the unit currently at the end of the queue, or cost 0 if the stock is empty. '
                         'Removed units can never be returned.')
        if level == 5:
            rules.append('"cancel" refers to an earlier operation ops[j] (0 <= j < current index). It succeeds only if '
                         'ops[j] is a "buy" that was not cancelled before and every unit that buy added is still in '
                         'stock: never sold, never removed by "adjust" (units that were sold and later returned do NOT '
                         'count as still in stock). A buy of 0 units can always be cancelled once. On success all '
                         'units of that buy are removed from the queue (wherever they are) and nothing is added to '
                         'cogs or shrinkage. Otherwise the cancel is rejected (index appended to "rejected").')
        stock = ('"stock": a list of lots [qty, unit_cost] describing the queue from front to end, where a lot is a '
                 'maximal run of consecutive units with the same unit cost' if level >= 2 else
                 '"stock": a list of lots [qty, unit_cost] from front to end, where each buy that is still (partly) '
                 'in stock is one lot holding its remaining units')
        if level >= 3:
            stock = ('"stock": a dict mapping each sku that has at least one unit in stock to its list of lots '
                     '[qty, unit_cost] from front to end, where a lot is a maximal run of consecutive units with the '
                     'same unit cost')
        keys = ['"cogs": int', stock, '"rejected": list of indices in increasing order']
        if level >= 2:
            keys.append('"shortfalls": list of [index, u] in increasing index order')
        if level >= 4:
            keys.append('"shrinkage": int')
        rules.append('Return a dict with exactly these keys: ' + '; '.join(keys) + '.')
        return rules

    def toggles(self, level):
        out = ('reject_whole', 'zero_qty')
        if level >= 2:
            out += ('merge', 'shortfall_positive')
        if level >= 3:
            out += ('return_front', 'return_lifo', 'omit_empty')
        if level >= 4:
            out += ('adjust_newest', 'adjust_cost')
        if level == 5:
            out += ('cancel_partial', 'cancel_zero')
        return out

    def solve(self, level, params, args, skip=frozenset()):
        queues, sold, seen = {}, {}, []
        cogs = shrinkage = 0
        rejected, shortfalls, cancelled = [], [], set()
        ops = args[0]
        for i, op in enumerate(ops):
            kind = op[0]
            if level <= 2:
                op = [kind, ''] + list(op[1:])
            sku = op[1] if kind != 'cancel' else None
            if sku is not None and sku not in queues:
                queues[sku], sold[sku] = [], []
                seen.append(sku)
            q = queues.get(sku)
            if kind == 'buy':
                q.extend([(op[3], i)] * op[2])
            elif kind == 'sell':
                qty, partial = op[2], (len(op) > 3 and op[3])
                if qty == 0 and 'zero_qty' in skip:
                    rejected.append(i)
                    continue
                take = qty
                if qty > len(q):
                    if partial:
                        take = len(q)
                        shortfalls.append([i, qty - take])
                    elif 'reject_whole' in skip:
                        take = len(q)
                    else:
                        rejected.append(i)
                        continue
                elif partial and 'shortfall_positive' in skip:
                    shortfalls.append([i, 0])
                units, queues[sku] = q[:take], q[take:]
                cogs += sum(c for c, _ in units)
                sold[sku].extend(units)
            elif kind == 'return':
                qty = op[2]
                if qty > len(sold[sku]):
                    rejected.append(i)
                    continue
                if qty == 0:
                    continue
                if 'return_lifo' in skip:
                    units, sold[sku] = sold[sku][:qty], sold[sku][qty:]
                else:
                    units, sold[sku] = sold[sku][-qty:], sold[sku][:-qty]
                units = [(c, None) for c, _ in units]
                cogs -= sum(c for c, _ in units)
                queues[sku] = units + q if 'return_front' not in skip else q + units
            elif kind == 'adjust':
                new = op[2]
                if new < len(q):
                    if 'adjust_newest' in skip:
                        removed, queues[sku] = q[:len(q) - new], q[len(q) - new:]
                    else:
                        removed, queues[sku] = q[new:], q[:new]
                    shrinkage += sum(c for c, _ in removed)
                elif new > len(q):
                    cost = q[-1][0] if q and 'adjust_cost' not in skip else 0
                    q.extend([(cost, None)] * (new - len(q)))
            elif kind == 'cancel':
                j = op[1]
                target = ops[j]
                ok = target[0] == 'buy' and j not in cancelled
                if ok:
                    bought = target[2]
                    present = sum(1 for units in queues.values() for _, origin in units if origin == j)
                    if bought == 0 and 'cancel_zero' in skip:
                        ok = False
                    elif present != bought and 'cancel_partial' not in skip:
                        ok = False
                if not ok:
                    rejected.append(i)
                    continue
                cancelled.add(j)
                tsku = target[1]
                queues[tsku] = [u for u in queues[tsku] if u[1] != j]
        stock = {}
        for sku in seen:
            lots = []
            for cost, origin in queues[sku]:
                same = lots and lots[-1][1] == cost and (level >= 2 and 'merge' not in skip or lots[-1][2] == origin)
                if same:
                    lots[-1][0] += 1
                else:
                    lots.append([1, cost, origin])
            stock[sku] = [[n, c] for n, c, _ in lots]
        out = {'cogs': cogs, 'rejected': rejected}
        if level <= 2:
            out['stock'] = stock.get('', [])
        else:
            out['stock'] = {k: v for k, v in stock.items() if v or 'omit_empty' in skip}
        if level >= 2:
            out['shortfalls'] = shortfalls
        if level >= 4:
            out['shrinkage'] = shrinkage
        return out

    def random_case(self, rng, level, params):
        skus = ['A'] if level <= 2 or rng.random() < .3 else ['A', 'B']
        ops = []
        for i in range(rng.randint(3, 12)):
            sku = rng.choice(skus)
            roll = rng.random()
            qty = rng.choice((0, 1, 2, 2, 3, 3, 4, 5, 6))
            if roll < .35 or not ops:
                op = ['buy', sku, qty, rng.choice((3, 5, 5, 7, 7, 9))]
            elif roll < .7:
                op = ['sell', sku, qty] + ([rng.random() < .5] if level >= 2 and rng.random() < .6 else [])
            elif level >= 3 and roll < .8:
                op = ['return', sku, rng.choice((0, 1, 1, 2, 3, 4))]
            elif level >= 4 and roll < .9:
                op = ['adjust', sku, rng.choice((0, 1, 2, 3, 5, 6))]
            elif level == 5:
                op = ['cancel', rng.randrange(i)]
            else:
                op = ['sell', sku, qty]
            if level <= 2 and op[0] != 'cancel':
                op = [op[0]] + op[2:]
            ops.append(op)
        return [ops]

    def public_case(self, rng, level, params):
        ops, stock = [], {}
        skus = ['A', 'B'] if level >= 3 else ['A']
        costs = rng.sample(range(2, 20), 6)
        for k in range(rng.randint(4, 6)):
            sku = rng.choice(skus)
            if stock.get(sku, 0) >= 3 and rng.random() < .5:
                qty = rng.randint(1, stock[sku] + 2)
                op = ['sell', sku, qty]
                if qty <= stock[sku]:
                    stock[sku] -= qty
            else:
                qty = rng.randint(2, 6)
                op = ['buy', sku, qty, costs[k % 6]]
                stock[sku] = stock.get(sku, 0) + qty
            ops.append(op if level >= 3 else [op[0]] + op[2:])
        return [ops]

    def plain_enough(self, level, params, args, expected):
        return expected['cogs'] > 0


# ---- H：字串壓縮（門檻、跳脫、九個一組、跳脫字元的門檻、空白與換行）----

class RunLength(Family):
    letter, name, entry = 'H', 'run_length_encoding', 'encode'

    def signature(self, level):
        return 'encode(s)'

    def rules(self, level, params):
        rules = []
        if level == 1:
            rules.append('`s` is a string that contains no digit characters. Split s into maximal runs of the same '
                         'character.')
        else:
            rules.append('`s` is any string' + (' of printable ASCII characters' if level <= 4 else
                                                ' of printable ASCII characters, spaces and line feeds "\\n"')
                         + '. Split s into maximal runs of the same character.')
        if level >= 2:
            special = 'digits 0-9 and the backslash \\' if level <= 4 else \
                'digits 0-9, the backslash \\, the underscore _ and the line feed'
            rules.append(f'Special characters: the {special}. The TOKEN of a character is "\\" followed by the '
                         'character for ' + ('digits and the backslash' if level <= 4 else 'digits, backslash and underscore')
                         + (', "\\n" (a backslash followed by the letter n) for the line feed' if level == 5 else '')
                         + ', and the character itself for every other character.')
        if level == 5:
            rules.append('Spaces: a single space is written as itself, but in a counted run (see below) the space '
                         'character is written as "_" instead of its token (so four spaces become "4_").')
        threshold = ('A run of length n >= 3 is written as the decimal number n followed by the token of the character '
                     '(e.g. "aaaa" becomes "4a"). A shorter run is written as its token repeated n times.')
        if level == 1:
            threshold = ('A run of length n >= 3 is written as the decimal number n followed by the character (e.g. '
                         '"aaaa" becomes "4a"). A shorter run is written unchanged.')
        if level >= 4:
            threshold = ('A run of length n is written as the decimal number n followed by the token of the character '
                         'when n >= 3, or when n >= 2 for special characters. Otherwise it is written as the token '
                         'repeated n times.')
        rules.append(threshold)
        if level >= 3:
            rules.append('A count never exceeds 9: a run longer than 9 is cut from the left into pieces of 9, and the '
                         'remaining piece (if any) follows the same rule as any other run of its length (it may be '
                         'written without a count). E.g. 12 "a" become "9a" + "aaa"-rule = "9a3a", and 10 "a" become '
                         '"9aa".')
        rules.append('Return the concatenation of the encodings of all runs. The empty string encodes to "".')
        return rules

    def toggles(self, level):
        out = ('threshold_eq',)
        if level >= 2:
            out += ('escape_in_run', 'escape_backslash')
        if level >= 3:
            out += ('chunk_remainder',)
        if level >= 4:
            out += ('escape_threshold',)
        if level == 5:
            out += ('space_underscore', 'underscore_escape', 'newline_escape')
        return out

    @staticmethod
    def special(level, c):
        return level >= 2 and (c.isdigit() or c == '\\' or (level == 5 and c in '_\n'))

    def token(self, level, c, counted, skip):
        if level == 5 and c == ' ' and counted:
            return ' ' if 'space_underscore' in skip else '_'
        if not self.special(level, c):
            return c
        if c == '\\' and 'escape_backslash' in skip:
            return c
        if c == '_' and 'underscore_escape' in skip:
            return c
        if c == '\n':
            return '\n' if 'newline_escape' in skip else '\\n'
        if counted and c.isdigit() and 'escape_in_run' in skip:
            return c
        return '\\' + c

    def solve(self, level, params, args, skip=frozenset()):
        s, out, i = args[0], [], 0
        while i < len(s):
            j = i
            while j < len(s) and s[j] == s[i]:
                j += 1
            c, n = s[i], j - i
            i = j
            limit = 2 if level >= 4 and self.special(level, c) and 'escape_threshold' not in skip else 3
            pieces = []
            while level >= 3 and n > 9:
                pieces.append(9)
                n -= 9
            pieces.append(n)
            for k, m in enumerate(pieces):
                remainder = k == len(pieces) - 1 and len(pieces) > 1
                counted = m > limit if 'threshold_eq' in skip else m >= limit
                if remainder and 'chunk_remainder' in skip:
                    counted = True
                if m == 9 and not remainder:
                    counted = True
                out.append(str(m) + self.token(level, c, True, skip) if counted else
                           self.token(level, c, False, skip) * m)
        return ''.join(out)

    def random_case(self, rng, level, params):
        pool = list('abcXY-!.')
        if level >= 2:
            pool += list('0157') + ['\\', '\\']
        if level == 5:
            pool += [' ', ' ', '_', '\n', 'n']
        lengths = (1, 1, 2, 2, 3, 3, 4) if level <= 2 else (1, 2, 2, 3, 3, 9, 10, 11, 12, 18, 19, 20, 21)
        out = ''
        for _ in range(rng.randint(0, 7)):
            c = rng.choice(pool)
            if out and out[-1] == c:
                continue
            out += c * (rng.choice(lengths) if rng.random() < .8 else rng.randint(1, 25 if level >= 3 else 13))
        return [out]

    def public_case(self, rng, level, params):
        letters = rng.sample('abcdefgxyz', 5)
        return [''.join(c * rng.choice((1, 1, 4, 5, 6, 7)) for c in letters)]

    def plain_enough(self, level, params, args, expected):
        return any(ch.isdigit() for ch in expected) and len(expected) < len(args[0])


FAMILIES = {f.letter: f for f in (Intervals(), Workdays(), Records(), Allocation(), Versions(), RateLimit(),
                                  Inventory(), RunLength())}
FAMILY_NAMES = {k: f.name for k, f in FAMILIES.items()}


# ==== 第二版題集：演算法陷阱（I～P）====
#
# 第一版的 A～H 把邊界條款逐條寫在規格裡，強模型會逐條照做（校準 01 誤放行只有 6%）。
# 第二版每題的規格很短、完全無歧義，難處在演算法：
# - 「看似合理」的貪婪或直覺解有反例（wrong 型陷阱）；
# - 暴力或 O(n^2) 解在公開範例正確、在隱藏的大輸入逾時（slow 型陷阱，每個測試 1 秒）。
# 每個題型寫出參考解與至少兩種典型錯誤解（都是可獨立執行的原始碼字串，方便丟進隔離環境）：
# 公開範例只收「所有錯誤解都答對」的小輸入；隱藏測試保證每個 wrong 型錯誤解至少有
# TRAP_COVERAGE 個小測試答錯，slow 型錯誤解由大輸入逾時。tests/test_code_tasks_v2.py
# 以暴力法與第二種正確演算法核對全部期望值，並在隔離環境確認 slow 型錯誤解真的逾時。

TRAP_VERSION = 'cloudyfan-code-tasks-v2'
TRAP_COVERAGE = 3
TRAP_SMALL_HIDDEN = 44
TRAP_SMALL_MAX = 56
HIDDEN_TIMEOUT_S = 30
TIME_LIMIT_RULE = ('Hidden tests use inputs up to these limits. Each call must return within 1 second in CPython '
                   '(standard library only), so choose an algorithm that is fast enough for the largest inputs.')


def _fast_ints(rng, count, lo, hi):
    """大輸入用：一次取亂數位元組，比逐一 randint 快很多；同一個 rng 狀態一定得到同一串數字。"""
    span = hi - lo + 1
    return [lo + v % span for v in memoryview(rng.randbytes(8 * count)).cast('Q')]


def _compile(source, entry):
    namespace = {'__name__': 'cloudyfan_trap'}
    exec(compile(source, f'<{entry}>', 'exec'), namespace)
    return namespace[entry]


class TrapFamily:
    """第二版題型：同一個函式簽名跨 5 級，級數只改輸入限制（規模與允許的情況）。"""
    letter = ''
    name = ''
    entry = ''
    signature = ''
    reference_source = ''
    trap_sources = {}
    slow_traps = frozenset()
    level_traps = {}

    def __init__(self):
        self._compiled = {}

    def rules(self):
        raise NotImplementedError

    def limits(self, level):
        raise NotImplementedError

    def traps(self, level):
        return self.level_traps[level]

    def wrong_traps(self, level):
        return tuple(t for t in self.traps(level) if t not in self.slow_traps)

    def function(self, name=None):
        """name=None 是參考解，否則是錯誤解。"""
        if name not in self._compiled:
            source = self.reference_source if name is None else self.trap_sources[name]
            self._compiled[name] = _compile(source, self.entry)
        return self._compiled[name]

    def solve(self, args):
        return self.function()(*_copy_args(args))

    def public_case(self, rng, level):
        raise NotImplementedError

    def random_case(self, rng, level):
        raise NotImplementedError

    def large_cases(self, rng, level):
        return []

    def plain_enough(self, level, args, expected):
        return True


def _copy_args(args):
    return json.loads(json.dumps(args))


def _agrees(function, args, expected):
    try:
        return code_sandbox.strict_equal(json.loads(json.dumps(function(*_copy_args(args)))), expected)
    except Exception:
        return False


# ---- I：多間會議室的帶權排程（貪婪、重複單間 DP 都錯；正解是最小成本流）----

I_REFERENCE = r'''
import heapq
from bisect import bisect_right


def _one_room(jobs):
    jobs = sorted(jobs, key=lambda j: j[1])
    ends = [j[1] for j in jobs]
    best = [0] * (len(jobs) + 1)
    for i, (s, e, p) in enumerate(jobs):
        k = bisect_right(ends, s, 0, i)
        best[i + 1] = max(best[i], best[k] + p)
    return best[-1]


def schedule(jobs, rooms):
    if not jobs:
        return 0
    if rooms >= len(jobs):
        return sum(j[2] for j in jobs)
    if rooms == 1:
        return _one_room(jobs)
    # 最小成本流：時間點串成一條鏈（容量 rooms、成本 0），每個工作是一條容量 1、成本 -profit 的捷徑。
    times = sorted({t for j in jobs for t in j[:2]})
    index = {t: i for i, t in enumerate(times)}
    m = len(times)
    to, cap, cost, nxt, head = [], [], [], [], [-1] * m

    def add(u, v, c, w):
        for a, b, cc, ww in ((u, v, c, w), (v, u, 0, -w)):
            to.append(b)
            cap.append(cc)
            cost.append(ww)
            nxt.append(head[a])
            head[a] = len(to) - 1
    for i in range(m - 1):
        add(i, i + 1, rooms, 0)
    for s, e, p in jobs:
        add(index[s], index[e], 1, -p)
    inf = float('inf')
    pot = [inf] * m
    pot[0] = 0
    for u in range(m):
        e = head[u]
        while e != -1:
            if cap[e] > 0 and pot[u] + cost[e] < pot[to[e]]:
                pot[to[e]] = pot[u] + cost[e]
            e = nxt[e]
    flow = total = 0
    while flow < rooms:
        dist = [inf] * m
        prev = [-1] * m
        dist[0] = 0
        heap = [(0, 0)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            e = head[u]
            pu = pot[u]
            while e != -1:
                if cap[e] > 0:
                    v = to[e]
                    nd = d + cost[e] + pu - pot[v]
                    if nd < dist[v]:
                        dist[v] = nd
                        prev[v] = e
                        heapq.heappush(heap, (nd, v))
                e = nxt[e]
        for v in range(m):
            if dist[v] < inf:
                pot[v] += dist[v]
        gain = pot[m - 1] - pot[0]
        if gain >= 0:
            break
        f, v = rooms - flow, m - 1
        while v != 0:
            e = prev[v]
            f = min(f, cap[e])
            v = to[e ^ 1]
        v = m - 1
        while v != 0:
            e = prev[v]
            cap[e] -= f
            cap[e ^ 1] += f
            v = to[e ^ 1]
        flow += f
        total += f * gain
    return -total
'''

I_TRAPS = {
    # 活動選擇法：依結束時間排，放進「最晚空出來且來得及」的房間；最大化件數，不是利潤。
    'earliest_end': r'''
def schedule(jobs, rooms):
    free = [0] * rooms
    total = 0
    for s, e, p in sorted(jobs, key=lambda j: (j[1], j[0])):
        best = -1
        for r in range(rooms):
            if free[r] <= s and (best < 0 or free[r] > free[best]):
                best = r
        if best >= 0:
            free[best] = e
            total += p
    return total
''',
    # 利潤由高到低，放得下就收。
    'max_profit_first': r'''
def schedule(jobs, rooms):
    chosen, total = [], 0
    for s, e, p in sorted(jobs, key=lambda j: -j[2]):
        trial = chosen + [(s, e)]
        events = sorted([(a, 1) for a, b in trial] + [(b, -1) for a, b in trial])
        depth = peak = 0
        for _, d in events:
            depth += d
            peak = max(peak, depth)
        if peak <= rooms:
            chosen = trial
            total += p
    return total
''',
    # 單間正解，但把「結束時刻 = 下一件開始時刻」當成重疊（差一）。
    'touching_overlap': r'''
from bisect import bisect_left


def schedule(jobs, rooms):
    jobs = sorted(jobs, key=lambda j: j[1])
    ends = [j[1] for j in jobs]
    best = [0] * (len(jobs) + 1)
    for i, (s, e, p) in enumerate(jobs):
        k = bisect_left(ends, s, 0, i)
        best[i + 1] = max(best[i], best[k] + p)
    return best[-1]
''',
    # 單間正解但 O(n^2)。
    'quadratic_dp': r'''
def schedule(jobs, rooms):
    jobs = sorted(jobs, key=lambda j: j[1])
    best = []
    for i, (s, e, p) in enumerate(jobs):
        b = p
        for j in range(i):
            if jobs[j][1] <= s and best[j] + p > b:
                b = best[j] + p
        best.append(b)
    return max(best, default=0)
''',
    # 每間房各跑一次單間最佳解，拿掉已選的再排下一間。
    'repeat_dp': r'''
from bisect import bisect_right


def _pick(jobs):
    order = sorted(range(len(jobs)), key=lambda i: jobs[i][1])
    ends = [jobs[i][1] for i in order]
    best, take = [0] * (len(order) + 1), [False] * len(order)
    for t, i in enumerate(order):
        k = bisect_right(ends, jobs[i][0], 0, t)
        if best[k] + jobs[i][2] > best[t]:
            best[t + 1] = best[k] + jobs[i][2]
            take[t] = True
        else:
            best[t + 1] = best[t]
    chosen, t = [], len(order)
    while t > 0:
        if take[t - 1]:
            chosen.append(order[t - 1])
            t = bisect_right(ends, jobs[order[t - 1]][0], 0, t - 1)
        else:
            t -= 1
    return best[-1], set(chosen)


def schedule(jobs, rooms):
    jobs, total = list(jobs), 0
    for _ in range(rooms):
        if not jobs:
            break
        value, chosen = _pick(jobs)
        total += value
        jobs = [j for i, j in enumerate(jobs) if i not in chosen]
    return total
''',
}


class RoomSchedule(TrapFamily):
    letter, name, entry, signature = 'I', 'weighted_room_schedule', 'schedule', 'schedule(jobs, rooms)'
    reference_source, trap_sources = I_REFERENCE, I_TRAPS
    slow_traps = frozenset({'quadratic_dp'})
    level_traps = {1: ('earliest_end', 'max_profit_first', 'touching_overlap'),
                   2: ('quadratic_dp', 'earliest_end', 'touching_overlap'),
                   3: ('repeat_dp', 'max_profit_first', 'earliest_end'),
                   4: ('repeat_dp', 'max_profit_first', 'earliest_end'),
                   5: ('repeat_dp', 'max_profit_first')}
    SIZES = {1: (12, 100, 50), 2: (50000, 10 ** 9, 10 ** 6), 3: (60, 1000, 1000), 4: (300, 10 ** 6, 10 ** 6),
             5: (1500, 10 ** 9, 10 ** 6)}
    ROOMS = {1: (1, 1), 2: (1, 1), 3: (2, 2), 4: (1, 4), 5: (1, 6)}

    def rules(self):
        return [
            '`jobs` is a list of jobs `[start, end, profit]` of integers with 0 <= start < end and profit >= 1. '
            'A job occupies the half-open time interval [start, end).',
            'There are `rooms` identical rooms (`rooms` >= 1). Choose any subset of the jobs and put every chosen '
            'job into one of the rooms so that two jobs in the same room never overlap. Jobs [s1, e1) and [s2, e2) '
            'overlap when s1 < e2 and s2 < e1; in particular a job that ends at time t and a job that starts at '
            'time t may use the same room. Jobs that are not chosen earn nothing.',
            'Return the maximum possible total profit of the chosen jobs. If `jobs` is empty, return 0.',
        ]

    def limits(self, level):
        n, t, p = self.SIZES[level]
        lo, hi = self.ROOMS[level]
        rooms = f'`rooms` is always {lo}' if lo == hi else f'{lo} <= rooms <= {hi}'
        return [f'{rooms}; len(jobs) <= {n}; end <= {t}; profit <= {p}.']

    def _jobs(self, rng, n, horizon, longest, profit):
        jobs = []
        for _ in range(n):
            s = rng.randint(0, horizon - 1)
            jobs.append([s, min(horizon, s + rng.randint(1, longest)), rng.randint(1, profit)])
        return jobs

    def _rooms(self, rng, level):
        lo, hi = self.ROOMS[level]
        return rng.randint(lo, min(hi, 3)) if level == 5 else rng.randint(lo, hi)

    def public_case(self, rng, level):
        rooms = self._rooms(rng, level)
        return [self._jobs(rng, rng.randint(4, 6) + 2 * (rooms - 1), 20, 8, 20), rooms]

    def plain_enough(self, level, args, expected):
        return 0 < expected < sum(j[2] for j in args[0])

    def random_case(self, rng, level):
        rooms = self._rooms(rng, level)
        n = rng.randint(1, 8 if level <= 2 else 11)
        horizon = rng.choice((10, 15, 25))
        return [self._jobs(rng, n, horizon, rng.choice((3, 6, 10)), rng.choice((5, 20, 50))), rooms]

    def large_cases(self, rng, level):
        n, horizon, profit = self.SIZES[level]
        lo, hi = self.ROOMS[level]
        out = []
        for k in range(3):
            rooms = hi if k == 0 else rng.randint(lo, hi)
            starts = _fast_ints(rng, n, 0, horizon - 2)
            spans = _fast_ints(rng, n, 1, max(1, horizon // max(4, n // (4 * rooms))))
            profits = _fast_ints(rng, n, 1, profit)
            out.append([[[s, min(horizon, s + d), p] for s, d, p in zip(starts, spans, profits)], rooms])
        return out


# ---- J：有工期與截止期限的接案（依期限貪婪、依利潤貪婪都錯）----

J_REFERENCE = r'''
import heapq


def max_profit(jobs):
    if not jobs:
        return 0
    if all(d == 1 for d, _, _ in jobs):
        # 工期都是 1：依期限排，堆積只留利潤最高的 deadline 件。
        heap = []
        for _, deadline, profit in sorted(jobs, key=lambda j: j[1]):
            heapq.heappush(heap, profit)
            if len(heap) > deadline:
                heapq.heappop(heap)
        return sum(heap)
    if len({p for _, _, p in jobs}) == 1:
        # 利潤都一樣：Moore-Hodgson，超過期限就丟掉目前最長的一件。
        heap, t = [], 0
        for d, deadline, _ in sorted(jobs, key=lambda j: j[1]):
            heapq.heappush(heap, -d)
            t += d
            if t > deadline:
                t += heapq.heappop(heap)
        return len(heap) * jobs[0][2]
    # 一般情況：依期限排序後，對「總加工時間」做背包。
    jobs = sorted(jobs, key=lambda j: j[1])
    horizon = min(jobs[-1][1], sum(j[0] for j in jobs))
    dp = [0] + [-1] * horizon
    for d, deadline, p in jobs:
        for t in range(min(deadline, horizon), d - 1, -1):
            if dp[t - d] >= 0 and dp[t - d] + p > dp[t]:
                dp[t] = dp[t - d] + p
    return max(dp)
'''

_J_FEASIBLE = r'''
def _feasible(chosen):
    t = 0
    for d, deadline, _ in sorted(chosen, key=lambda j: j[1]):
        t += d
        if t > deadline:
            return False
    return True


def _greedy(jobs, key):
    chosen = []
    for job in sorted(jobs, key=key):
        if _feasible(chosen + [job]):
            chosen.append(job)
    return sum(j[2] for j in chosen)
'''

J_TRAPS = {
    # 依期限排，放得下就放、放不下就跳過（不回頭換掉長的）。
    'edd_skip': r'''
def max_profit(jobs):
    t = total = 0
    for d, deadline, p in sorted(jobs, key=lambda j: (j[1], j[0])):
        if t + d <= deadline:
            t += d
            total += p
    return total
''',
    # 短的先做，依這個順序一件接一件排，趕不上期限就跳過（不重新排順序）。
    'spt_skip': r'''
def max_profit(jobs):
    t = total = 0
    for d, deadline, p in sorted(jobs, key=lambda j: (j[0], j[1])):
        if t + d <= deadline:
            t += d
            total += p
    return total
''',
    # 經典「有期限的工作排序」：利潤高的先收，排得完就收（只對工期 1 正確）。
    'profit_first': _J_FEASIBLE + r'''
def max_profit(jobs):
    return _greedy(jobs, lambda j: (-j[2], j[1]))
''',
    # 單位時間利潤高的先收。
    'ratio_first': _J_FEASIBLE + r'''
def max_profit(jobs):
    return _greedy(jobs, lambda j: (-j[2] / j[0], j[1]))
''',
    # Moore-Hodgson 套到帶權：超過期限就丟掉利潤最低的。
    'moore_profit': r'''
import heapq


def max_profit(jobs):
    heap, t = [], 0
    for d, deadline, p in sorted(jobs, key=lambda j: j[1]):
        heapq.heappush(heap, (p, d))
        t += d
        while t > deadline:
            t -= heapq.heappop(heap)[1]
    return sum(p for p, _ in heap)
''',
    # 正確的 Moore-Hodgson，但每次用 max + remove 找最長的：O(n^2)。
    'moore_list': r'''
def max_profit(jobs):
    chosen, t = [], 0
    for d, deadline, _ in sorted(jobs, key=lambda j: j[1]):
        chosen.append(d)
        t += d
        if t > deadline:
            longest = max(chosen)
            chosen.remove(longest)
            t -= longest
    return len(chosen) * (jobs[0][2] if jobs else 0)
''',
    # 工期 1 的經典解：利潤高的先排進期限前最晚的空檔，往前線性找空檔：O(n^2)。
    'slot_scan': r'''
def max_profit(jobs):
    n = len(jobs)
    free = [True] * (n + 1)
    total = 0
    for _, deadline, p in sorted(jobs, key=lambda j: -j[2]):
        t = min(deadline, n)
        while t > 0 and not free[t]:
            t -= 1
        if t > 0:
            free[t] = False
            total += p
    return total
''',
}


class DeadlineJobs(TrapFamily):
    letter, name, entry, signature = 'J', 'deadline_jobs', 'max_profit', 'max_profit(jobs)'
    reference_source, trap_sources = J_REFERENCE, J_TRAPS
    slow_traps = frozenset({'moore_list', 'slot_scan'})
    level_traps = {1: ('edd_skip', 'spt_skip'),
                   2: ('moore_list', 'edd_skip', 'spt_skip'),
                   3: ('profit_first', 'moore_profit', 'edd_skip'),
                   4: ('profit_first', 'ratio_first', 'moore_profit'),
                   5: ('slot_scan', 'edd_skip')}

    def rules(self):
        return [
            '`jobs` is a list of jobs `[duration, deadline, profit]` of positive integers.',
            'A single machine is available from time 0. It processes at most one job at a time, and a job that '
            'has been started runs for `duration` consecutive time units without interruption. The machine may '
            'stay idle between jobs.',
            'Choose any subset of the jobs and an order in which to process them. Every chosen job must be '
            'completed no later than its deadline (completion time <= deadline). Jobs that are not chosen are '
            'skipped and earn nothing.',
            'Return the maximum possible total profit of the chosen jobs (0 if `jobs` is empty or no job can be '
            'completed in time).',
        ]

    def limits(self, level):
        return {1: ['Every profit is 1. len(jobs) <= 12; duration <= 10; deadline <= 60.'],
                2: ['Every profit is 1. len(jobs) <= 100000; duration <= 10000; deadline <= 10**9.'],
                3: ['len(jobs) <= 12; duration <= 10; deadline <= 60; profit <= 1000.'],
                4: ['len(jobs) <= 100; duration <= 50; deadline <= 10**9; profit <= 10**6.'],
                5: ['Every duration is 1. len(jobs) <= 100000; deadline <= 10**9; profit <= 10**9.']}[level]

    def _job(self, rng, level, horizon=None):
        if level in (1, 2):
            d = rng.randint(1, 10)
            return [d, rng.randint(1, horizon or 40), 1]
        if level == 5:
            return [1, rng.randint(1, horizon or 8), rng.randint(1, 50)]
        d = rng.randint(1, 10)
        return [d, rng.randint(1, horizon or 40), rng.randint(1, 60)]

    def public_case(self, rng, level):
        return [[self._job(rng, level, 30 if level != 5 else 6) for _ in range(rng.randint(4, 6))]]

    def plain_enough(self, level, args, expected):
        if level == 5:
            best = sum(sorted((j[2] for j in args[0]), reverse=True)[:len(args[0])])
            return 0 < expected < best
        return 0 < expected < sum(j[2] for j in args[0])

    def random_case(self, rng, level):
        n = rng.randint(1, 10)
        horizon = rng.choice((15, 25, 40)) if level != 5 else rng.choice((3, 5, 8))
        return [[self._job(rng, level, horizon) for _ in range(n)]]

    def large_cases(self, rng, level):
        if level == 1 or level == 3:
            return [[[self._job(rng, level, 60) for _ in range(12)]] for _ in range(3)]
        if level == 4:
            # 期限可到 10**9，但總工期最多 5000：用「最大期限」開陣列的 DP 會爆，要以總工期為上限。
            out = []
            for k in range(3):
                durations = _fast_ints(rng, 100, 1, 50)
                total = sum(durations)
                deadlines = _fast_ints(rng, 100, 1, total * 2 // 3)
                deadlines[k::7] = [10 ** 9 - j for j in range(len(deadlines[k::7]))]
                profits = _fast_ints(rng, 100, 1, 10 ** 6)
                out.append([[list(j) for j in zip(durations, deadlines, profits)]])
            return out
        n = 100000
        out = []
        for k in range(3):
            if level == 2:
                durations = _fast_ints(rng, n, 1, 10000)
                # 期限只夠做大約一半：Moore-Hodgson 要丟掉很多件，max+remove 的寫法就會很慢。
                deadlines = _fast_ints(rng, n, 1, n * 2500 + k * n * 500)
                out.append([[[d, dl, 1] for d, dl in zip(durations, deadlines)]])
            else:
                half = n // 2
                deadlines = _fast_ints(rng, half, 1, half) + _fast_ints(rng, n - half, n, 10 ** 9)
                profits = _fast_ints(rng, n, 1, 10 ** 9)
                out.append([[[1, dl, p] for dl, p in zip(deadlines, profits)]])
        return out


# ---- K：字串切割的最低成本與方法數（最長匹配貪婪、去重字典、遞迴都會出事）----

K_REFERENCE = r'''
MOD = 1_000_000_007


def segment(s, words):
    best = {}
    for word, cost in words:
        entry = best.get(word)
        if entry is None or cost < entry[0]:
            best[word] = [cost, 1]
        elif cost == entry[0]:
            entry[1] += 1
    lengths = sorted({len(w) for w in best})
    n = len(s)
    inf = float('inf')
    costs = [inf] * (n + 1)
    ways = [0] * (n + 1)
    costs[0], ways[0] = 0, 1
    for i in range(1, n + 1):
        bc, bw = inf, 0
        for size in lengths:
            if size > i:
                break
            j = i - size
            cj = costs[j]
            if cj == inf:
                continue
            entry = best.get(s[j:i])
            if entry is None:
                continue
            c = cj + entry[0]
            if c < bc:
                bc, bw = c, ways[j] * entry[1]
            elif c == bc:
                bw += ways[j] * entry[1]
        costs[i], ways[i] = bc, bw % MOD
    return [-1, 0] if costs[n] == inf else [costs[n], ways[n]]
'''

_K_TABLE = r'''
MOD = 1_000_000_007


def _table(words):
    best = {}
    for word, cost in words:
        entry = best.get(word)
        if entry is None or cost < entry[0]:
            best[word] = [cost, 1]
        elif cost == entry[0]:
            entry[1] += 1
    return best
'''

K_TRAPS = {
    # 每一步取最長的可用字（同字取最便宜的），方法數當成 1。
    'greedy_longest': r'''
def segment(s, words):
    best = {}
    for w, c in words:
        best[w] = min(c, best.get(w, c))
    longest = max((len(w) for w in best), default=0)
    i = total = 0
    while i < len(s):
        for size in range(min(longest, len(s) - i), 0, -1):
            if s[i:i + size] in best:
                total += best[s[i:i + size]]
                i += size
                break
        else:
            return [-1, 0]
    return [total, 1]
''',
    # 最低成本對，但方法數算成「所有切法」而不是「最低成本的切法」。
    'count_all': r'''
MOD = 1_000_000_007


def segment(s, words):
    cheapest, copies = {}, {}
    for w, c in words:
        cheapest[w] = min(c, cheapest.get(w, c))
        copies[w] = copies.get(w, 0) + 1
    n = len(s)
    inf = float('inf')
    cost, ways = [0] + [inf] * n, [1] + [0] * n
    for i in range(1, n + 1):
        for w in cheapest:
            j = i - len(w)
            if j >= 0 and s[j:i] == w and cost[j] < inf:
                cost[i] = min(cost[i], cost[j] + cheapest[w])
                ways[i] = (ways[i] + ways[j] * copies[w]) % MOD
    return [-1, 0] if cost[n] == inf else [cost[n], ways[n]]
''',
    # 字典用 {word: cost} 建：重複的字只留最後一筆，也不再算有幾筆。
    'dedupe': r'''
MOD = 1_000_000_007


def segment(s, words):
    table = {w: c for w, c in words}
    lengths = sorted({len(w) for w in table})
    n = len(s)
    inf = float('inf')
    cost, ways = [0] + [inf] * n, [1] + [0] * n
    for i in range(1, n + 1):
        for size in lengths:
            j = i - size
            if j < 0:
                break
            w = s[j:i]
            if w in table and cost[j] < inf:
                c = cost[j] + table[w]
                if c < cost[i]:
                    cost[i], ways[i] = c, ways[j]
                elif c == cost[i]:
                    ways[i] = (ways[i] + ways[j]) % MOD
    return [-1, 0] if cost[n] == inf else [cost[n], ways[n]]
''',
    # 正確，但對每個切點往回試所有起點：O(n^2) 次切片。
    'quadratic': _K_TABLE + r'''
def segment(s, words):
    best = _table(words)
    n = len(s)
    inf = float('inf')
    cost, ways = [0] + [inf] * n, [1] + [0] * n
    for i in range(1, n + 1):
        for j in range(i):
            entry = best.get(s[j:i])
            if entry is not None and cost[j] < inf:
                c = cost[j] + entry[0]
                if c < cost[i]:
                    cost[i], ways[i] = c, ways[j] * entry[1] % MOD
                elif c == cost[i]:
                    ways[i] = (ways[i] + ways[j] * entry[1]) % MOD
    return [-1, 0] if cost[n] == inf else [cost[n], ways[n]]
''',
    # 正確，但用遞迴加快取：長字串超過遞迴深度上限。
    'recursive': _K_TABLE + r'''
from functools import lru_cache


def segment(s, words):
    best = _table(words)
    lengths = sorted({len(w) for w in best})
    inf = float('inf')

    @lru_cache(maxsize=None)
    def go(i):
        if i == len(s):
            return 0, 1
        bc, bw = inf, 0
        for size in lengths:
            entry = best.get(s[i:i + size]) if i + size <= len(s) else None
            if entry is None:
                continue
            c, w = go(i + size)
            if c + entry[0] < bc:
                bc, bw = c + entry[0], w * entry[1]
            elif c + entry[0] == bc:
                bw += w * entry[1]
        return bc, bw % MOD
    c, w = go(0)
    return [-1, 0] if c == inf else [c, w]
''',
    # 正確，但每個位置都把整本字典掃一遍（startswith）：O(n * len(words))。
    'scan_all': r'''
MOD = 1_000_000_007


def segment(s, words):
    n = len(s)
    inf = float('inf')
    cost, ways = [0] + [inf] * n, [1] + [0] * n
    for i in range(n):
        if cost[i] == inf:
            continue
        for w, c in words:
            if s.startswith(w, i):
                j = i + len(w)
                if cost[i] + c < cost[j]:
                    cost[j], ways[j] = cost[i] + c, ways[i]
                elif cost[i] + c == cost[j]:
                    ways[j] = (ways[j] + ways[i]) % MOD
    return [-1, 0] if cost[n] == inf else [cost[n], ways[n]]
''',
}


class Segmentation(TrapFamily):
    letter, name, entry, signature = 'K', 'word_segmentation', 'segment', 'segment(s, words)'
    reference_source, trap_sources = K_REFERENCE, K_TRAPS
    slow_traps = frozenset({'quadratic', 'recursive', 'scan_all'})
    level_traps = {1: ('greedy_longest', 'count_all'),
                   2: ('quadratic', 'recursive', 'greedy_longest'),
                   3: ('dedupe', 'count_all', 'greedy_longest'),
                   4: ('recursive', 'quadratic', 'dedupe'),
                   5: ('scan_all', 'recursive', 'dedupe')}
    SIZES = {1: (20, 8, 6), 2: (30000, 200, 10), 3: (20, 10, 6), 4: (20000, 500, 20), 5: (20000, 5000, 12)}

    def rules(self):
        return [
            '`s` is a string of lowercase letters. `words` is a list of entries `[word, cost]`, where word is a '
            'non-empty string of lowercase letters and cost is a positive integer.',
            'A segmentation of s cuts s into consecutive non-empty pieces and assigns to every piece one entry of '
            '`words` whose word equals that piece. Its cost is the sum of the costs of the assigned entries. Two '
            'segmentations are different if they cut s at different positions or if they assign a different entry '
            '(a different index in `words`) to some piece.',
            'Return `[best, ways]`, where best is the minimum cost of a segmentation of s and ways is the number of '
            'segmentations whose cost equals best, modulo 1000000007. If s has no segmentation, return [-1, 0]. '
            'The empty string has exactly one segmentation (with no pieces), so it gives [0, 1].',
        ]

    def limits(self, level):
        n, count, longest = self.SIZES[level]
        unique = ('All words in `words` are distinct. ' if level in (1, 2) else
                  'The same word may occur in several entries, possibly with different costs. ')
        return [unique + f'len(s) <= {n}; len(words) <= {count}; every word has at most {longest} letters; '
                f'cost <= 1000.']

    def _words(self, rng, level, count, alphabet, longest, costs=9):
        words, seen = [], set()
        for _ in range(count * 4):
            if len(words) >= count:
                break
            w = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, longest)))
            if w in seen and (level in (1, 2) or rng.random() < .5):
                continue
            seen.add(w)
            words.append([w, rng.randint(1, costs)])
        return words

    def _text(self, rng, words, pieces):
        return ''.join(rng.choice(words)[0] for _ in range(pieces))

    def public_case(self, rng, level):
        words = self._words(rng, 1, rng.randint(3, 5), 'abc', 4)
        return [self._text(rng, words, rng.randint(2, 4)), words]

    def plain_enough(self, level, args, expected):
        return expected[0] > 0 and len(args[0]) >= 4

    def random_case(self, rng, level):
        alphabet = rng.choice(('ab', 'ab', 'abc'))
        words = self._words(rng, level, rng.randint(2, 7), alphabet, rng.choice((2, 3, 4)), rng.choice((3, 9)))
        if level >= 3 and words and rng.random() < .6:
            for _ in range(rng.randint(1, 3)):
                words.append([rng.choice(words)[0], rng.randint(1, 9)])
            rng.shuffle(words)
        s = self._text(rng, words, rng.randint(0, 6))
        if s and rng.random() < .15:
            s = s[:-1] if rng.random() < .5 else s + rng.choice(alphabet)
        return [s[:self.SIZES[level][0]], words]

    def large_cases(self, rng, level):
        n, count, longest = self.SIZES[level]
        out = []
        for k in range(3):
            if level in (1, 3):
                words = self._words(rng, level, count - 2, 'ab', 3)
                words += [[rng.choice(words)[0], rng.randint(1, 9)] for _ in range(2 if level == 3 else 0)]
                text = ''
                while len(text) < n - 3:
                    text += rng.choice(words)[0]
                out.append([text[:n], words])
                continue
            alphabet = 'ab' if k < 2 else 'abc'
            words, seen = [], set()
            while len(words) < count:
                size = rng.randint(1, longest)
                w = ''.join(rng.choice(alphabet) for _ in range(size))
                if w in seen and level == 2:
                    continue
                seen.add(w)
                words.append([w, rng.randint(1, 1000)])
            short = [w for w in words if len(w[0]) <= 3] or words
            pieces = []
            total = 0
            while total < n - longest:
                w = rng.choice(short)[0] if k == 0 else rng.choice(words)[0]
                pieces.append(w)
                total += len(w)
            out.append([''.join(pieces), words])
        return out


# ---- L：套盒（二維嚴格遞增鏈；同寬的排序、旋轉、O(n^2) 都是陷阱）----

L_REFERENCE = r'''
from bisect import bisect_left


def nest(boxes, rotate):
    items = [(min(w, h), max(w, h), v) if rotate else (w, h, v) for w, h, v in boxes]
    if not items:
        return 0
    if all(v == 1 for _, _, v in items):
        # 同寬的盒子依高度遞減排，嚴格 LIS 才不會把同寬的串起來。
        items.sort(key=lambda b: (b[0], -b[1]))
        tails = []
        for _, h, _ in items:
            k = bisect_left(tails, h)
            if k == len(tails):
                tails.append(h)
            else:
                tails[k] = h
        return len(tails)
    heights = sorted({b[1] for b in items})
    rank = {h: i + 1 for i, h in enumerate(heights)}
    size = len(heights)
    tree = [0] * (size + 1)
    items.sort(key=lambda b: b[0])
    best, i, n = 0, 0, len(items)
    while i < n:
        j = i
        while j < n and items[j][0] == items[i][0]:
            j += 1
        group = []
        for _, h, v in items[i:j]:
            k, q = rank[h] - 1, 0
            while k > 0:
                if tree[k] > q:
                    q = tree[k]
                k &= k - 1
            group.append((rank[h], q + v))
        for k, value in group:
            if value > best:
                best = value
            while k <= size:
                if tree[k] < value:
                    tree[k] = value
                k += k & -k
        i = j
    return best
'''

_L_BIT = r'''
def _chain(items, inclusive=False):
    heights = sorted({b[1] for b in items})
    rank = {h: i + 1 for i, h in enumerate(heights)}
    size = len(heights)
    tree = [0] * (size + 1)
    best = 0
    for _, h, v in sorted(items, key=lambda b: (b[0], b[1])):
        k, q = rank[h] - (0 if inclusive else 1), 0
        while k > 0:
            q = max(q, tree[k])
            k &= k - 1
        value = q + v
        best = max(best, value)
        k = rank[h]
        while k <= size:
            tree[k] = max(tree[k], value)
            k += k & -k
    return best
'''

L_TRAPS = {
    # 依 (寬, 高) 遞增排序後對高度做嚴格 LIS：同寬不同高的盒子會被串起來。
    'sort_asc': r'''
from bisect import bisect_left


def nest(boxes, rotate):
    items = sorted((min(w, h), max(w, h)) if rotate else (w, h) for w, h, _ in boxes)
    tails = []
    for _, h in items:
        k = bisect_left(tails, h)
        if k == len(tails):
            tails.append(h)
        else:
            tails[k] = h
    return len(tails)
''',
    # 排序對了，但 LIS 用 bisect_right（允許高度相等）。
    'nonstrict': r'''
from bisect import bisect_right


def nest(boxes, rotate):
    items = sorted(((min(w, h), max(w, h)) if rotate else (w, h) for w, h, _ in boxes), key=lambda b: (b[0], -b[1]))
    tails = []
    for _, h in items:
        k = bisect_right(tails, h)
        if k == len(tails):
            tails.append(h)
        else:
            tails[k] = h
    return len(tails)
''',
    # 正確但 O(n^2) 的 DP。
    'quadratic': r'''
def nest(boxes, rotate):
    items = sorted((min(w, h), max(w, h), v) if rotate else (w, h, v) for w, h, v in boxes)
    best = []
    for i, (w, h, v) in enumerate(items):
        b = v
        for j in range(i):
            if items[j][0] < w and items[j][1] < h and best[j] + v > b:
                b = best[j] + v
        best.append(b)
    return max(best, default=0)
''',
    # 忽略 rotate。
    'no_rotate': r'''
from bisect import bisect_left


def nest(boxes, rotate):
    items = [tuple(b) for b in boxes]
    if all(v == 1 for _, _, v in items):
        items.sort(key=lambda b: (b[0], -b[1]))
        tails = []
        for _, h, _ in items:
            k = bisect_left(tails, h)
            if k == len(tails):
                tails.append(h)
            else:
                tails[k] = h
        return len(tails)
    best = {}
    for w, h, v in sorted(items):
        best[(w, h, len(best))] = v + max([b for (w2, h2, _), b in best.items() if w2 < w and h2 < h], default=0)
    return max(best.values(), default=0)
''',
    # 帶權樹狀陣列，但依 (寬, 高) 逐一查詢後立刻更新：同寬的盒子會互相串起來。
    'weighted_tie': _L_BIT + r'''
def nest(boxes, rotate):
    items = [(min(w, h), max(w, h), v) if rotate else (w, h, v) for w, h, v in boxes]
    return _chain(items) if items else 0
''',
    # 帶權樹狀陣列，但查詢包含相同高度（<= 而不是 <）。
    'inclusive_query': r'''
def nest(boxes, rotate):
    items = [(min(w, h), max(w, h), v) if rotate else (w, h, v) for w, h, v in boxes]
    if not items:
        return 0
    heights = sorted({b[1] for b in items})
    rank = {h: i + 1 for i, h in enumerate(heights)}
    size = len(heights)
    tree = [0] * (size + 1)
    items.sort(key=lambda b: b[0])
    best, i = 0, 0
    while i < len(items):
        j = i
        while j < len(items) and items[j][0] == items[i][0]:
            j += 1
        group = []
        for _, h, v in items[i:j]:
            k, q = rank[h], 0
            while k > 0:
                q = max(q, tree[k])
                k &= k - 1
            group.append((rank[h], q + v))
        for k, value in group:
            best = max(best, value)
            while k <= size:
                tree[k] = max(tree[k], value)
                k += k & -k
        i = j
    return best
''',
}


class NestedBoxes(TrapFamily):
    letter, name, entry, signature = 'L', 'nested_boxes', 'nest', 'nest(boxes, rotate)'
    reference_source, trap_sources = L_REFERENCE, L_TRAPS
    slow_traps = frozenset({'quadratic'})
    level_traps = {1: ('sort_asc', 'nonstrict'),
                   2: ('quadratic', 'sort_asc', 'nonstrict'),
                   3: ('quadratic', 'no_rotate', 'sort_asc'),
                   4: ('quadratic', 'weighted_tie', 'inclusive_query'),
                   5: ('quadratic', 'weighted_tie', 'no_rotate')}
    SIZES = {1: 12, 2: 30000, 3: 30000, 4: 30000, 5: 30000}

    def rules(self):
        return [
            '`boxes` is a list of boxes `[width, height, value]` of positive integers; `rotate` is a boolean.',
            "Box A fits into box B when A's width is strictly less than B's width and A's height is strictly less "
            "than B's height. If `rotate` is true, every box may first be turned by 90 degrees (swapping its width "
            'and height); each box is used in a single orientation. If `rotate` is false, boxes are never turned.',
            'A chain is a sequence of distinct boxes (different items of `boxes`) in which every box fits into the '
            'next one. Two boxes with identical dimensions are different boxes, but they can never both be in one '
            'chain.',
            'Return the maximum total value of a chain. A single box is a chain; an empty list gives 0.',
        ]

    def limits(self, level):
        n = self.SIZES[level]
        rotate = 'rotate is always false' if level in (1, 2, 4) else 'rotate may be true or false'
        value = 'every value is 1' if level <= 3 else 'value <= 10**6'
        return [f'{rotate}; {value}; len(boxes) <= {n}; width and height <= 10**9.']

    def _rotate(self, rng, level):
        return level in (3, 5) and rng.random() < .7

    def _box(self, rng, level, side):
        return [rng.randint(1, side), rng.randint(1, side), 1 if level <= 3 else rng.randint(1, 20)]

    def public_case(self, rng, level):
        boxes = [self._box(rng, level, 30) for _ in range(rng.randint(5, 7))]
        return [boxes, self._rotate(rng, level)]

    def plain_enough(self, level, args, expected):
        return 1 < expected < sum(b[2] for b in args[0])

    def random_case(self, rng, level):
        side = rng.choice((4, 6, 10))
        return [[self._box(rng, level, side) for _ in range(rng.randint(0, 10))], self._rotate(rng, level)]

    def large_cases(self, rng, level):
        n = self.SIZES[level]
        out = []
        for k in range(3):
            side = (10 ** 9, 1000, 50000)[k] if n > 100 else 20
            ws, hs = _fast_ints(rng, n, 1, side), _fast_ints(rng, n, 1, side)
            vs = [1] * n if level <= 3 else _fast_ints(rng, n, 1, 10 ** 6)
            if n > 100 and k == 2:
                # 大部分盒子幾乎排成一條長鏈：O(n^2) 的內層迴圈跑滿。
                order = sorted(range(n), key=ws.__getitem__)
                hs = [0] * n
                for rank, i in enumerate(order):
                    hs[i] = rank + 1
            out.append([[list(b) for b in zip(ws, hs, vs)], level in (3, 5) and k != 1])
        return out


# ---- M：拆邊與裝回後的連通塊數（只拆不裝時是反向並查集；會裝回時要離線動態連通）----

M_REFERENCE = r'''
from bisect import bisect_left


def _removals_only(n, edges, toggles, checkpoints):
    # 每條邊最多切換一次＝只拆不裝：倒著把邊加回去，用並查集算塊數。
    parent, size, count = list(range(n)), [1] * n, n

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(u, v):
        nonlocal count
        a, b = find(u), find(v)
        if a != b:
            if size[a] < size[b]:
                a, b = b, a
            parent[b] = a
            size[a] += size[b]
            count -= 1
    removed = set(toggles)
    for i, (u, v) in enumerate(edges):
        if i not in removed:
            union(u, v)
    at = [0] * (len(toggles) + 1)
    at[len(toggles)] = count
    for k in range(len(toggles) - 1, -1, -1):
        union(*edges[toggles[k]])
        at[k] = count
    return [at[k] for k in checkpoints]


def components(n, edges, toggles, checkpoints):
    if len(set(toggles)) == len(toggles):
        return _removals_only(n, edges, toggles, checkpoints)
    # 會裝回：把每條邊「存在的檢查點區間」放進線段樹，深度優先走一遍，用可撤銷的並查集。
    keys = sorted(set(checkpoints))
    t = len(keys)
    size = 1
    while size < t:
        size *= 2
    nodes = [[] for _ in range(2 * size)]

    def insert(lo, hi, edge):
        lo, hi = bisect_left(keys, lo) + size, bisect_left(keys, hi) + size
        while lo < hi:
            if lo & 1:
                nodes[lo].append(edge)
                lo += 1
            if hi & 1:
                hi -= 1
                nodes[hi].append(edge)
            lo //= 2
            hi //= 2
    flips = [[] for _ in edges]
    for k, i in enumerate(toggles):
        flips[i].append(k + 1)
    end = len(toggles) + 1
    for i, edge in enumerate(edges):
        marks = [0] + flips[i] + [end]
        for j in range(0, len(marks) - 1, 2):
            insert(marks[j], marks[j + 1], edge)
    parent, weight = list(range(n)), [1] * n
    history, count = [], n
    answer = {}
    stack = [(1, False)]
    while stack:
        value, leaving = stack.pop()
        if leaving:
            while len(history) > value:
                b = history.pop()
                a = parent[b]
                parent[b] = b
                weight[a] -= weight[b]
                count += 1
            continue
        node = value
        stack.append((len(history), True))
        for u, v in nodes[node]:
            while parent[u] != u:
                u = parent[u]
            while parent[v] != v:
                v = parent[v]
            if u != v:
                if weight[u] < weight[v]:
                    u, v = v, u
                parent[v] = u
                weight[u] += weight[v]
                history.append(v)
                count -= 1
        if node >= size:
            if node - size < t:
                answer[keys[node - size]] = count
        else:
            stack.append((2 * node + 1, False))
            stack.append((2 * node, False))
    return [answer[k] for k in checkpoints]
'''

_M_BFS = r'''
def _count(n, adjacency):
    seen = [False] * n
    count = 0
    for start in range(n):
        if seen[start]:
            continue
        count += 1
        seen[start] = True
        stack = [start]
        while stack:
            u = stack.pop()
            for v in adjacency[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
    return count


def _present(m, toggles, k):
    present = [True] * m
    for i in toggles[:k]:
        present[i] = not present[i]
    return present
'''

M_TRAPS = {
    # 先在原圖找橋；之後每拆掉一座「原圖的橋」就多一塊（忽略拆邊後別的邊變成橋）。
    'bridge_once': r'''
def components(n, edges, toggles, checkpoints):
    adjacency = [[] for _ in range(n)]
    for i, (u, v) in enumerate(edges):
        adjacency[u].append((v, i))
        adjacency[v].append((u, i))
    order, low, bridges, count = [-1] * n, [0] * n, set(), 0
    clock = 0
    for root in range(n):
        if order[root] >= 0:
            continue
        count += 1
        order[root] = low[root] = clock
        clock += 1
        stack = [(root, -1, iter(adjacency[root]))]
        while stack:
            u, via, it = stack[-1]
            for v, i in it:
                if i == via:
                    continue
                if order[v] < 0:
                    order[v] = low[v] = clock
                    clock += 1
                    stack.append((v, i, iter(adjacency[v])))
                    break
                low[u] = min(low[u], order[v])
            else:
                stack.pop()
                if stack:
                    p = stack[-1][0]
                    low[p] = min(low[p], low[u])
                    if low[u] > order[p]:
                        bridges.add(via)
    at = [count]
    for i in toggles:
        at.append(at[-1] + (i in bridges))
    return [at[k] for k in checkpoints]
''',
    # 用「鄰居集合」存圖：拆掉一條平行邊就把兩點之間的所有邊都拆了。
    'simple_graph': _M_BFS + r'''
def components(n, edges, toggles, checkpoints):
    answers = {}
    for k in sorted(set(checkpoints)):
        present = _present(len(edges), toggles, k)
        adjacency = [set() for _ in range(n)]
        for i, (u, v) in enumerate(edges):
            if present[i]:
                adjacency[u].add(v)
                adjacency[v].add(u)
        for i in toggles[:k]:
            u, v = edges[i]
            if not present[i]:
                adjacency[u].discard(v)
                adjacency[v].discard(u)
        answers[k] = _count(n, adjacency)
    return [answers[k] for k in checkpoints]
''',
    # 只拆不裝時的標準解（倒著加回去的並查集），把每次切換都當成拆邊。
    'reverse_dsu': r'''
def components(n, edges, toggles, checkpoints):
    parent = list(range(n))
    count = n

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(u, v):
        nonlocal count
        a, b = find(u), find(v)
        if a != b:
            parent[a] = b
            count -= 1
    removed = set(toggles)
    for i, (u, v) in enumerate(edges):
        if i not in removed:
            union(u, v)
    at = [0] * (len(toggles) + 1)
    at[len(toggles)] = count
    for k in range(len(toggles) - 1, -1, -1):
        union(*edges[toggles[k]])
        at[k] = count
    return [at[k] for k in checkpoints]
''',
    # 正確：每個檢查點重新 BFS 一次，O(檢查點數 * (n + m))。
    'bfs_each': _M_BFS + r'''
def components(n, edges, toggles, checkpoints):
    answers = {}
    for k in sorted(set(checkpoints)):
        present = _present(len(edges), toggles, k)
        adjacency = [[] for _ in range(n)]
        for i, (u, v) in enumerate(edges):
            if present[i]:
                adjacency[u].append(v)
                adjacency[v].append(u)
        answers[k] = _count(n, adjacency)
    return [answers[k] for k in checkpoints]
''',
    # 正確：每個檢查點重建一次並查集。
    'dsu_rebuild': _M_BFS + r'''
def components(n, edges, toggles, checkpoints):
    answers = {}
    for k in set(checkpoints):
        present = _present(len(edges), toggles, k)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        count = n
        for i, (u, v) in enumerate(edges):
            if present[i]:
                a, b = find(u), find(v)
                if a != b:
                    parent[a] = b
                    count -= 1
        answers[k] = count
    return [answers[k] for k in checkpoints]
''',
}


class EdgeToggles(TrapFamily):
    letter, name, entry, signature = 'M', 'edge_toggles', 'components', 'components(n, edges, toggles, checkpoints)'
    reference_source, trap_sources = M_REFERENCE, M_TRAPS
    slow_traps = frozenset({'bfs_each', 'dsu_rebuild'})
    level_traps = {1: ('bridge_once', 'simple_graph'),
                   2: ('bridge_once', 'simple_graph'),
                   3: ('bfs_each', 'bridge_once', 'simple_graph'),
                   4: ('reverse_dsu', 'simple_graph'),
                   5: ('dsu_rebuild', 'bfs_each', 'reverse_dsu')}
    SIZES = {1: (8, 12, 10, 12), 2: (10, 16, 20, 16), 3: (30000, 40000, 300, 40000), 4: (10, 16, 20, 30),
             5: (5000, 5000, 2000, 10000)}

    def rules(self):
        return [
            'The graph is undirected and has the vertices 0, 1, ..., n-1. `edges` is a list of pairs `[u, v]` with '
            '0 <= u, v < n; edge i is edges[i]. Several edges may join the same two vertices, and every item of '
            '`edges` is a separate edge. At the start every edge is present.',
            '`toggles` is a list of edge indices that are processed one after another: toggling edge i removes it '
            'if it is currently present and adds it back if it is currently absent. Toggling edge i affects only '
            'that one item of `edges`.',
            '`checkpoints` is a list of integers k with 0 <= k <= len(toggles). The answer for a checkpoint k is '
            'the number of connected components of the graph after the first k toggles have been processed (every '
            'isolated vertex is a component of its own).',
            'Return the list of answers, one per checkpoint, in the order of `checkpoints` (checkpoints may repeat '
            'and need not be sorted).',
        ]

    def limits(self, level):
        n, m, c, t = self.SIZES[level]
        loops = 'There are no self-loops (u != v always). ' if level == 1 else \
            'Self-loops (u == v) may occur; a self-loop never connects two different vertices. '
        repeat = ('Every index occurs at most once in `toggles` (so every toggle removes an edge). ' if level <= 3 else
                  'An index may occur several times in `toggles`. ')
        return [loops + repeat + f'1 <= n <= {n}; len(edges) <= {m}; len(toggles) <= {t}; '
                f'len(checkpoints) <= {c}.']

    def _graph(self, rng, level, n, m, parallel):
        edges = []
        for _ in range(m):
            if edges and rng.random() < parallel:
                edges.append(list(rng.choice(edges)) if rng.random() < .5 else rng.choice(edges)[::-1])
                continue
            u = rng.randrange(n)
            others = [x for x in range(n) if x != u]
            v = rng.randrange(n) if level >= 2 and rng.random() < .1 or not others else rng.choice(others)
            if level == 1 and u == v:
                continue
            edges.append([u, v])
        return edges

    def _toggles(self, rng, level, m):
        if m == 0:
            return []
        if level <= 3:
            return rng.sample(range(m), rng.randint(0, m))
        return [rng.randrange(m) for _ in range(rng.randint(0, 2 * m))]

    def public_case(self, rng, level):
        n = rng.randint(4, 6)
        edges = self._graph(rng, 1, n, rng.randint(n, n + 2), 0)
        toggles = rng.sample(range(len(edges)), rng.randint(2, len(edges)))
        return [n, edges, toggles, list(range(len(toggles) + 1))]

    def plain_enough(self, level, args, expected):
        pairs = [tuple(sorted(e)) for e in args[1]]
        return len(set(pairs)) == len(pairs) and len(set(expected)) >= 2 and expected[-1] < args[0]

    def random_case(self, rng, level):
        limit_n, limit_m, limit_c, limit_t = self.SIZES[level]
        n = rng.randint(1, min(limit_n, 8))
        edges = self._graph(rng, level, n, rng.randint(0, min(2 * n, limit_m)), rng.choice((0, .2, .4)))
        toggles = self._toggles(rng, level, len(edges))[:limit_t]
        checkpoints = [rng.randint(0, len(toggles)) for _ in range(rng.randint(1, 6))]
        if rng.random() < .5:
            checkpoints = list(range(len(toggles) + 1))[:limit_c]
        return [n, edges, toggles, checkpoints]

    def large_cases(self, rng, level):
        n, m, c, t = self.SIZES[level]
        out = []
        for k in range(3):
            if n <= 10:
                edges = self._graph(rng, level, n, m, .2)
                toggles = self._toggles(rng, level, len(edges))[:t]
                out.append([n, edges, toggles, list(range(len(toggles) + 1))[:c]])
                continue
            size = n if k == 0 else n // 2
            if k == 2:
                # 一條長環：拆邊順序決定什麼時候真正斷開。
                us, vs = [i % size for i in range(m)], [(i + 1) % size for i in range(m)]
            else:
                us, vs = _fast_ints(rng, m, 0, size - 1), _fast_ints(rng, m, 0, size - 1)
            edges = [[u, v] for u, v in zip(us, vs)]
            if level <= 3:
                toggles = list(range(m))
                rng.shuffle(toggles)
                toggles = toggles[:m * 3 // 4]
            else:
                toggles = _fast_ints(rng, t, 0, m - 1)
            checkpoints = sorted(_fast_ints(rng, c, 0, len(toggles)))
            if k == 1:
                rng.shuffle(checkpoints)
            out.append([n, edges, toggles, checkpoints])
        return out


# ---- N：樹上每個點的最遠距離（邊權可為負：兩次 BFS 找直徑端點的捷徑失效；換根要排除自己的子樹）----

N_REFERENCE = r'''
def farthest(n, edges, queries):
    adjacency = [[] for _ in range(n)]
    for u, v, w in edges:
        adjacency[u].append((v, w))
        adjacency[v].append((u, w))
    parent, up_weight = [-1] * n, [0] * n
    seen = [False] * n
    seen[0] = True
    order = [0]
    for u in order:
        for v, w in adjacency[u]:
            if not seen[v]:
                seen[v] = True
                parent[v] = u
                up_weight[v] = w
                order.append(v)
    # down1/down2：往子樹走的最遠與次遠（0 代表停在自己）；via：最遠那條經過哪個孩子。
    down1, down2, via = [0] * n, [0] * n, [-1] * n
    for u in reversed(order):
        p = parent[u]
        if p >= 0:
            c = down1[u] + up_weight[u]
            if c > down1[p]:
                down2[p], down1[p], via[p] = down1[p], c, u
            elif c > down2[p]:
                down2[p] = c
    up = [0] * n
    for u in order:
        p = parent[u]
        if p >= 0:
            other = down2[p] if via[p] == u else down1[p]
            up[u] = up_weight[u] + max(up[p], other)
    return [max(down1[q], up[q]) for q in queries]
'''

_N_TREE = r'''
def _rooted(n, edges):
    adjacency = [[] for _ in range(n)]
    for u, v, w in edges:
        adjacency[u].append((v, w))
        adjacency[v].append((u, w))
    parent, weight, seen, order = [-1] * n, [0] * n, [False] * n, [0]
    seen[0] = True
    for u in order:
        for v, w in adjacency[u]:
            if not seen[v]:
                seen[v] = True
                parent[v], weight[v] = u, w
                order.append(v)
    return adjacency, parent, weight, order


def _distances(n, adjacency, start):
    dist = [None] * n
    dist[start] = 0
    stack = [start]
    while stack:
        u = stack.pop()
        for v, w in adjacency[u]:
            if dist[v] is None:
                dist[v] = dist[u] + w
                stack.append(v)
    return dist
'''

N_TRAPS = {
    # 換根時直接拿父節點最遠的那條，沒排除自己所在的子樹。
    'reroot_top1': _N_TREE + r'''
def farthest(n, edges, queries):
    adjacency, parent, weight, order = _rooted(n, edges)
    down = [0] * n
    for u in reversed(order):
        if parent[u] >= 0:
            down[parent[u]] = max(down[parent[u]], down[u] + weight[u])
    up = [0] * n
    for u in order:
        if parent[u] >= 0:
            up[u] = weight[u] + max(up[parent[u]], down[parent[u]])
    return [max(down[q], up[q]) for q in queries]
''',
    # 換根時只看父節點的其他子樹，忘了再往上（祖先那一側）。
    'reroot_no_up': _N_TREE + r'''
def farthest(n, edges, queries):
    adjacency, parent, weight, order = _rooted(n, edges)
    down1, down2, via = [0] * n, [0] * n, [-1] * n
    for u in reversed(order):
        p = parent[u]
        if p >= 0:
            c = down1[u] + weight[u]
            if c > down1[p]:
                down2[p], down1[p], via[p] = down1[p], c, u
            elif c > down2[p]:
                down2[p] = c
    up = [0] * n
    for u in order:
        p = parent[u]
        if p >= 0:
            up[u] = weight[u] + (down2[p] if via[p] == u else down1[p])
    return [max(down1[q], up[q]) for q in queries]
''',
    # 兩次掃描找直徑端點 a、b，最遠距離取 max(dist(a, x), dist(b, x))：只在邊權非負時成立。
    'double_sweep': _N_TREE + r'''
def farthest(n, edges, queries):
    adjacency = _rooted(n, edges)[0]
    d0 = _distances(n, adjacency, 0)
    a = max(range(n), key=d0.__getitem__)
    da = _distances(n, adjacency, a)
    b = max(range(n), key=da.__getitem__)
    db = _distances(n, adjacency, b)
    return [max(da[q], db[q]) for q in queries]
''',
    # 只算到「別的點」的最遠距離（漏了自己這個距離 0 的點）。
    'others_only': _N_TREE + r'''
def farthest(n, edges, queries):
    adjacency, parent, weight, order = _rooted(n, edges)
    neg = float('-inf')
    down1, down2, via = [neg] * n, [neg] * n, [-1] * n
    for u in reversed(order):
        p = parent[u]
        if p >= 0:
            c = max(down1[u], 0) + weight[u]
            if c > down1[p]:
                down2[p], down1[p], via[p] = down1[p], c, u
            elif c > down2[p]:
                down2[p] = c
    up = [neg] * n
    for u in order:
        p = parent[u]
        if p >= 0:
            other = down2[p] if via[p] == u else down1[p]
            up[u] = weight[u] + max(up[p], other, 0)
    out = []
    for q in queries:
        best = max(down1[q], up[q])
        out.append(best if best != neg else 0)
    return out
''',
    # 正確：每個查詢從該點走一遍整棵樹。
    'bfs_each': _N_TREE + r'''
def farthest(n, edges, queries):
    adjacency = _rooted(n, edges)[0]
    return [max(_distances(n, adjacency, q)) for q in queries]
''',
    # 正確的換根，但用遞迴：深的樹超過遞迴深度上限。
    'recursive': r'''
def farthest(n, edges, queries):
    adjacency = [[] for _ in range(n)]
    for u, v, w in edges:
        adjacency[u].append((v, w))
        adjacency[v].append((u, w))
    down1, down2, via, up = [0] * n, [0] * n, [-1] * n, [0] * n

    def down(u, p):
        for v, w in adjacency[u]:
            if v != p:
                down(v, u)
                c = down1[v] + w
                if c > down1[u]:
                    down2[u], down1[u], via[u] = down1[u], c, v
                elif c > down2[u]:
                    down2[u] = c

    def spread(u, p):
        for v, w in adjacency[u]:
            if v != p:
                up[v] = w + max(up[u], down2[u] if via[u] == v else down1[u])
                spread(v, u)
    down(0, -1)
    spread(0, -1)
    return [max(down1[q], up[q]) for q in queries]
''',
}


class TreeEccentricity(TrapFamily):
    letter, name, entry, signature = 'N', 'tree_eccentricity', 'farthest', 'farthest(n, edges, queries)'
    reference_source, trap_sources = N_REFERENCE, N_TRAPS
    slow_traps = frozenset({'bfs_each', 'recursive'})
    level_traps = {1: ('reroot_top1', 'reroot_no_up'),
                   2: ('bfs_each', 'recursive', 'reroot_top1'),
                   3: ('double_sweep', 'others_only', 'reroot_top1'),
                   4: ('bfs_each', 'double_sweep', 'others_only'),
                   5: ('recursive', 'bfs_each', 'double_sweep')}
    SIZES = {1: (12, 1, 20), 2: (30000, 1, 10 ** 6), 3: (12, -20, 20), 4: (30000, -10 ** 6, 10 ** 6),
             5: (50000, -10 ** 6, 10 ** 6)}

    def rules(self):
        return [
            'The tree has the nodes 0, 1, ..., n-1 (n >= 1). `edges` contains its n - 1 edges `[u, v, w]`: an edge '
            'between nodes u and v with integer weight w.',
            'The distance between two nodes is the sum of the weights of the edges on the unique path between them. '
            'The distance from a node to itself is 0.',
            'The eccentricity of a node x is the maximum distance from x to any node of the tree, x itself '
            'included.',
            '`queries` is a list of nodes (values may repeat). Return the list of their eccentricities, in the '
            'same order.',
        ]

    def limits(self, level):
        n, lo, hi = self.SIZES[level]
        return [f'1 <= n <= {n}; {lo} <= w <= {hi}; len(queries) <= 1000.']

    def _tree(self, rng, level, n, lo, hi, spread=None):
        labels = list(range(n))
        rng.shuffle(labels)
        edges = []
        for i in range(1, n):
            p = rng.randint(max(0, i - spread), i - 1) if spread else rng.randrange(i)
            edges.append([labels[p], labels[i], rng.randint(lo, hi)])
        return edges

    def _weights(self, rng, level):
        return (1, 20) if level <= 2 else rng.choice(((-20, 20), (-5, 10), (-10, 3)))

    def public_case(self, rng, level):
        n = rng.randint(4, 7)
        lo, hi = (1, 20) if level <= 2 else (-5, 20)
        return [n, self._tree(rng, level, n, lo, hi), sorted(rng.sample(range(n), rng.randint(2, n)))]

    def plain_enough(self, level, args, expected):
        return len(set(expected)) >= 2

    def random_case(self, rng, level):
        n = rng.randint(1, 10)
        lo, hi = self._weights(rng, level)
        queries = list(range(n)) if rng.random() < .6 else [rng.randrange(n) for _ in range(rng.randint(1, 5))]
        return [n, self._tree(rng, level, n, lo, hi, rng.choice((None, 2))), queries]

    def large_cases(self, rng, level):
        n, lo, hi = self.SIZES[level]
        if n <= 12:
            return [[n, self._tree(rng, level, n, lo, hi, (None, 2, 1)[k]), list(range(n))] for k in range(3)]
        out = []
        for k in range(3):
            labels = list(range(n))
            rng.shuffle(labels)
            spread = (3, 1000, n)[k]
            offsets = _fast_ints(rng, n, 1, spread)
            weights = _fast_ints(rng, n, lo, hi)
            edges = [[labels[max(0, i - offsets[i])], labels[i], weights[i]] for i in range(1, n)]
            out.append([n, edges, _fast_ints(rng, 1000, 0, n - 1)])
        return out


# ---- O：調成非遞減的最小加權調整量（只往上補、只往下壓都錯；值域 DP 太慢；正解是 slope trick）----

O_REFERENCE = r'''
import heapq


def min_cost(a, weights):
    heap, total = [], 0
    for x, w in zip(a, weights):
        moved, rest = 0, w
        while rest and heap and -heap[0][0] > x:
            top = heap[0]
            take = top[1] if top[1] < rest else rest
            total += (-top[0] - x) * take
            rest -= take
            moved += take
            if take == top[1]:
                heapq.heappop(heap)
            else:
                top[1] -= take
        heapq.heappush(heap, [-x, w + moved])
    return total
'''

O_TRAPS = {
    # 由左往右，比前一個小就補到前一個的值。
    'raise_only': r'''
def min_cost(a, weights):
    current, total = None, 0
    for x, w in zip(a, weights):
        if current is not None and x < current:
            total += (current - x) * w
        else:
            current = x
    return total
''',
    # 由右往左，比後一個大就壓到後一個的值。
    'lower_only': r'''
def min_cost(a, weights):
    current, total = None, 0
    for x, w in zip(reversed(a), reversed(weights)):
        if current is not None and x > current:
            total += (x - current) * w
        else:
            current = x
    return total
''',
    # 正確：對所有出現過的值做 DP，O(n * 不同值個數)。
    'compressed_dp': r'''
def min_cost(a, weights):
    values = sorted(set(a))
    best = [0] * len(values)
    for x, w in zip(a, weights):
        running = None
        for j, v in enumerate(values):
            c = best[j] + w * abs(x - v)
            if running is None or c < running:
                running = c
            best[j] = running
    return best[-1] if values else 0
''',
    # 不帶權的 slope trick，成本再乘上權重：權重不同時答案錯。
    'unweighted': r'''
import heapq


def min_cost(a, weights):
    heap, total = [], 0
    for x, w in zip(a, weights):
        heapq.heappush(heap, -x)
        if -heap[0] > x:
            total += (-heap[0] - x) * w
            heapq.heapreplace(heap, -x)
    return total
''',
    # 正確：把權重 w 的元素展開成 w 個，再做不帶權的 slope trick；權重大時太慢。
    'expand_weights': r'''
import heapq


def min_cost(a, weights):
    heap, total = [], 0
    for x, w in zip(a, weights):
        for _ in range(w):
            heapq.heappush(heap, -x)
            if -heap[0] > x:
                total += -heap[0] - x
                heapq.heapreplace(heap, -x)
    return total
''',
}


class MonotoneFit(TrapFamily):
    letter, name, entry, signature = 'O', 'monotone_fit', 'min_cost', 'min_cost(a, weights)'
    reference_source, trap_sources = O_REFERENCE, O_TRAPS
    slow_traps = frozenset({'compressed_dp', 'expand_weights'})
    level_traps = {1: ('raise_only', 'lower_only'),
                   2: ('raise_only', 'lower_only'),
                   3: ('compressed_dp', 'raise_only', 'lower_only'),
                   4: ('unweighted', 'raise_only', 'expand_weights'),
                   5: ('unweighted', 'expand_weights', 'compressed_dp')}
    SIZES = {1: (10, 20, 1), 2: (20, 10 ** 9, 1), 3: (20000, 10 ** 9, 1), 4: (12, 10 ** 9, 10 ** 6),
             5: (20000, 10 ** 9, 10 ** 6)}

    def rules(self):
        return [
            '`a` is a list of integers and `weights` is a list of positive integers of the same length.',
            'Choose integers b[0], ..., b[n-1] with b[0] <= b[1] <= ... <= b[n-1]. The cost of this choice is the '
            'sum of weights[i] * |a[i] - b[i]| over all i.',
            'Return the minimum possible cost (0 for empty lists).',
        ]

    def limits(self, level):
        n, value, weight = self.SIZES[level]
        weights = 'every weight is 1' if weight == 1 else f'weights[i] <= {weight}'
        return [f'len(a) <= {n}; |a[i]| <= {value}; {weights}.']

    def _case(self, rng, level, n, value, weight):
        a = [rng.randint(-value, value) for _ in range(n)]
        return [a, [rng.randint(1, weight) for _ in range(n)]]

    def public_case(self, rng, level):
        return self._case(rng, level, rng.randint(5, 7), 9, 1 if level <= 3 else 4)

    def plain_enough(self, level, args, expected):
        return expected > 0

    def random_case(self, rng, level):
        n = rng.randint(0, 9)
        value = rng.choice((3, 6, 20)) if level == 1 else rng.choice((5, 100, 10 ** 9))
        weight = 1 if level <= 3 else rng.choice((3, 10, 10 ** 6))
        return self._case(rng, level, n, value, weight)

    def large_cases(self, rng, level):
        n, value, weight = self.SIZES[level]
        out = []
        for k in range(3):
            if n <= 20:
                out.append(self._case(rng, level, n, min(value, 10 ** 9), weight))
                continue
            noise = _fast_ints(rng, n, -value // 4, value // 4)
            trend = (0, value // n, -value // n)[k]
            a = [max(-value, min(value, trend * (i - n // 2) + e)) for i, e in enumerate(noise)]
            out.append([a, [1] * n if weight == 1 else _fast_ints(rng, n, weight // 2, weight)])
        return out


# ---- P：多路合併的最小成本（不補零的 k 路 Huffman、依序合併、每次重排都是陷阱）----

P_REFERENCE = r'''
import heapq


def merge_cost(piles, k):
    n = len(piles)
    if n <= 1:
        return 0
    if k >= n:
        return sum(piles)
    # 補上大小 0 的假堆，讓每次都能剛好合併 k 堆。
    heap = list(piles) + [0] * ((k - 1 - (n - 1) % (k - 1)) % (k - 1))
    heapq.heapify(heap)
    total = 0
    while len(heap) > 1:
        s = 0
        for _ in range(k):
            s += heapq.heappop(heap)
        total += s
        heapq.heappush(heap, s)
    return total
'''

P_TRAPS = {
    # 排序後由小到大依序併進同一堆。
    'sequential': r'''
def merge_cost(piles, k):
    piles = sorted(piles)
    running, total = piles[0], 0
    for x in piles[1:]:
        running += x
        total += running
    return total
''',
    # 只能合併相鄰兩堆的區間 DP（題目允許任意兩堆）。
    'adjacent': r'''
def merge_cost(piles, k):
    n = len(piles)
    prefix = [0]
    for x in piles:
        prefix.append(prefix[-1] + x)
    cost = [[0] * n for _ in range(n)]
    for length in range(2, n + 1):
        for i in range(n - length + 1):
            j = i + length - 1
            cost[i][j] = min(cost[i][m] + cost[m + 1][j] for m in range(i, j)) + prefix[j + 1] - prefix[i]
    return cost[0][n - 1]
''',
    # 正確，但每次合併前都重新排序整個清單。
    'resort': r'''
def merge_cost(piles, k):
    piles = list(piles)
    total = 0
    first = True
    while len(piles) > 1:
        piles.sort()
        take = min(k, len(piles)) if not first or k == 2 else (len(piles) - 2) % (k - 1) + 2
        first = False
        merged = sum(piles[:take])
        total += merged
        piles = piles[take:] + [merged]
    return total
''',
    # 每次合併最小的 min(k, 剩下堆數) 堆，不補假堆：最後一次可能浪費在大堆上。
    'no_pad': r'''
import heapq


def merge_cost(piles, k):
    heap = list(piles)
    heapq.heapify(heap)
    total = 0
    while len(heap) > 1:
        merged = sum(heapq.heappop(heap) for _ in range(min(k, len(heap))))
        total += merged
        heapq.heappush(heap, merged)
    return total
''',
    # 有補假堆，但補到 (堆數 - 1) 是 k 的倍數（應該是 k - 1）。
    'pad_wrong': r'''
import heapq


def merge_cost(piles, k):
    heap = list(piles)
    while len(heap) > 1 and (len(heap) - 1) % k:
        heap.append(0)
    heapq.heapify(heap)
    total = 0
    while len(heap) > 1:
        merged = sum(heapq.heappop(heap) for _ in range(min(k, len(heap))))
        total += merged
        heapq.heappush(heap, merged)
    return total
''',
}


class MergePiles(TrapFamily):
    letter, name, entry, signature = 'P', 'merge_piles', 'merge_cost', 'merge_cost(piles, k)'
    reference_source, trap_sources = P_REFERENCE, P_TRAPS
    slow_traps = frozenset({'resort'})
    level_traps = {1: ('sequential', 'adjacent'),
                   2: ('resort', 'sequential'),
                   3: ('no_pad', 'pad_wrong'),
                   4: ('resort', 'no_pad'),
                   5: ('resort', 'no_pad', 'pad_wrong')}
    SIZES = {1: (10, 100, 2, 2), 2: (50000, 10 ** 9, 2, 2), 3: (10, 100, 3, 5), 4: (50000, 10 ** 9, 2, 10),
             5: (50000, 10 ** 9, 2, 100000)}

    def rules(self):
        return [
            '`piles` is a non-empty list of positive integers (pile sizes) and `k` is an integer with k >= 2.',
            'One merge picks at least 2 and at most k of the current piles (any piles, not necessarily adjacent '
            'in the list) and replaces them by a single pile whose size is the sum of their sizes. The merge costs '
            'that sum.',
            'Merges are repeated until exactly one pile is left. Return the minimum possible total cost of all '
            'merges (0 if there is only one pile).',
        ]

    def limits(self, level):
        n, size, lo, hi = self.SIZES[level]
        k = f'k is always {lo}' if lo == hi else f'{lo} <= k <= {hi}'
        return [f'{k}; len(piles) <= {n}; every pile size is at most {size}.']

    def _k(self, rng, level, n):
        lo, hi = self.SIZES[level][2:]
        if level == 5 and rng.random() < .2:
            return rng.randint(max(2, n - 1), n + 3)
        return rng.randint(lo, min(hi, 6))

    def public_case(self, rng, level):
        lo, hi = self.SIZES[level][2:]
        k = 2 if hi == 2 else rng.randint(3, min(hi, 4))
        # 公開範例只用「剛好每次合併 k 堆」的堆數，看不出要補假堆。
        n = 1 + (k - 1) * rng.randint(2, 3) if k > 2 else rng.randint(4, 6)
        if level in (3, 5):
            k = 3
            n = 1 + k * (k - 1)
        piles = sorted(rng.randint(1, 30) for _ in range(n))
        return [piles if level <= 2 else rng.sample(piles, n), k]

    def plain_enough(self, level, args, expected):
        return len(args[0]) >= 4

    def random_case(self, rng, level):
        n = rng.randint(1, 9)
        k = self._k(rng, level, n)
        return [[rng.randint(1, rng.choice((5, 30, 100))) for _ in range(n)], k]

    def large_cases(self, rng, level):
        n, size, lo, hi = self.SIZES[level]
        if n <= 10:
            return [self.random_case(rng, level) for _ in range(3)]
        out = []
        for j in range(3):
            k = (lo, rng.randint(lo, min(hi, 10)), hi if hi < 100 else 3)[j] if level == 5 else (lo, lo + 1, hi)[j] \
                if level == 4 else 2
            count = n - j * 7
            out.append([_fast_ints(rng, count, 1, size if j != 1 else 1000), k])
        return out


TRAP_FAMILIES = {f.letter: f for f in (RoomSchedule(), DeadlineJobs(), Segmentation(), NestedBoxes(),
                                       EdgeToggles(), TreeEccentricity(), MonotoneFit(), MergePiles())}


# ---- 產生、凍結 ----

def _call(entry, args):
    return f'{entry}({", ".join(repr(a) for a in args)})'


def build_prompt(family, level, params, public):
    fam = FAMILIES[family]
    lines = [f'Implement the Python function `{fam.signature(level)}` exactly as specified below.', '',
             'Specification:']
    lines += [f'{k}. {rule}' for k, rule in enumerate(fam.rules(level, params), 1)]
    lines += ['', 'Examples:']
    lines += [f'- {_call(fam.entry, t["args"])} returns {t["expected"]!r}' for t in public]
    lines += ['', REPLY_RULES.format(entry=fam.entry)]
    return '\n'.join(lines)


def _rng(family, level, seed):
    return random.Random(f'cloudyfan-code-tasks-r{FAMILY_REVISION[family]}:{family}:{level}:{seed}')


def _tickets():
    """第三版題型在 code_tickets.py（它反過來引用本模組的共用函式，所以這裡延後匯入）。"""
    from . import code_tickets
    return code_tickets


def ticket_families():
    return _tickets().TICKET_FAMILIES


def _repo_tasks():
    """第四版題型在 code_repo_tasks.py（同樣反過來引用本模組，延後匯入）。"""
    from . import code_repo_tasks
    return code_repo_tasks


def repo_families():
    return _repo_tasks().REPO_FAMILIES


def build(family, level, seed):
    """產生一題（含隱藏測試與各隱藏測試碰到的邊界條款）；同樣的 (題型, 難度, 種子) 一定得到同一題。"""
    if family in TRAP_FAMILIES:
        return build_trap(family, level, seed)
    if family in FAMILY_REVISION and family not in FAMILIES:
        return _tickets().build_ticket(family, level, seed)
    fam = FAMILIES[family]
    rng = _rng(family, level, seed)
    params = fam.params(rng, level)
    public, seen = [], set()
    for _ in range(20000):
        if len(public) == PUBLIC_COUNT:
            break
        args = fam.public_case(rng, level, params)
        key = json.dumps(args, sort_keys=True)
        if key in seen or fam.edges(level, params, args):
            continue
        expected = fam.solve(level, params, args)
        if fam.plain_enough(level, params, args, expected):
            seen.add(key)
            public.append({'args': args, 'expected': expected})
    if len(public) < PUBLIC_COUNT:
        raise RuntimeError(f'Could not generate plain public examples for {family}{level}-{seed}')
    hidden, hidden_edges, counts, plain = [], [], Counter(), 0
    names = fam.edge_names(level)
    for _ in range(40000):
        covered = all(counts[e] >= EDGE_COVERAGE for e in names)
        if (len(hidden) >= HIDDEN_MIN and covered) or len(hidden) >= HIDDEN_MAX:
            break
        args = fam.random_case(rng, level, params)
        key = json.dumps(args, sort_keys=True)
        if key in seen:
            continue
        edges = fam.edges(level, params, args)
        needed = any(counts[e] < EDGE_COVERAGE for e in edges)
        # 以碰到邊界條款的測試為主，留少量一般情況；接近下限時只收還沒覆蓋夠的條款。
        if not edges and plain >= max(8, len(hidden) // 4):
            continue
        if not needed and not covered and len(hidden) >= HIDDEN_MIN - 15:
            continue
        seen.add(key)
        counts.update(edges)
        plain += not edges
        hidden.append({'args': args, 'expected': fam.solve(level, params, args)})
        hidden_edges.append(sorted(edges))
    missing = [e for e in names if counts[e] < EDGE_COVERAGE]
    if missing or len(hidden) < HIDDEN_MIN:
        raise RuntimeError(f'Hidden tests of {family}{level}-{seed} miss edge coverage: {missing}')
    task = {'id': f'{family}{level}-{seed}', 'family': family, 'level': level, 'seed': seed,
            'prompt': build_prompt(family, level, params, public), 'entry_point': fam.entry,
            'public_tests': public, 'hidden_tests': hidden, 'kind': 'code'}
    return task, {'params': params, 'hidden_edges': hidden_edges, 'edge_counts': dict(counts)}


def build_trap_prompt(fam, level, public):
    lines = [f'Implement the Python function `{fam.signature}` exactly as specified below.', '', 'Specification:']
    lines += [f'{k}. {rule}' for k, rule in enumerate(fam.rules(), 1)]
    lines += ['', 'Limits:'] + [f'- {rule}' for rule in fam.limits(level) + [TIME_LIMIT_RULE]]
    lines += ['', 'Examples:']
    lines += [f'- {_call(fam.entry, t["args"])} returns {t["expected"]!r}' for t in public]
    lines += ['', REPLY_RULES.format(entry=fam.entry)]
    return '\n'.join(lines)


def build_trap(family, level, seed):
    """第二版的一題。大輸入產生較慢，同一行程內快取 JSON 文字，每次呼叫回傳新的複本。"""
    task, meta = _trap_json(family, level, seed)
    return json.loads(task), json.loads(meta)


@lru_cache(maxsize=None)
def _trap_json(family, level, seed):
    return tuple(json.dumps(part, ensure_ascii=False) for part in _build_trap(family, level, seed))


def _build_trap(family, level, seed):
    """公開範例只收所有錯誤解都答對的小輸入；隱藏測試讓每個 wrong 型錯誤解至少答錯
    TRAP_COVERAGE 題，最後接上幾個大輸入（slow 型錯誤解在這裡逾時）。"""
    fam = TRAP_FAMILIES[family]
    rng = _rng(family, level, seed)
    traps, wrong = fam.traps(level), fam.wrong_traps(level)
    public, seen = [], set()
    for _ in range(20000):
        if len(public) == PUBLIC_COUNT:
            break
        args = fam.public_case(rng, level)
        key = json.dumps(args, sort_keys=True)
        if key in seen:
            continue
        expected = fam.solve(args)
        if fam.plain_enough(level, args, expected) and all(_agrees(fam.function(t), args, expected) for t in traps):
            seen.add(key)
            public.append({'args': args, 'expected': expected})
    if len(public) < PUBLIC_COUNT:
        raise RuntimeError(f'Could not generate trap-free public examples for {family}{level}-{seed}')
    hidden, failing, counts, plain = [], [], Counter(), 0
    for _ in range(40000):
        covered = all(counts[t] >= TRAP_COVERAGE for t in wrong)
        if (len(hidden) >= TRAP_SMALL_HIDDEN and covered) or len(hidden) >= TRAP_SMALL_MAX:
            break
        args = fam.random_case(rng, level)
        key = json.dumps(args, sort_keys=True)
        if key in seen:
            continue
        expected = fam.solve(args)
        fails = {t for t in wrong if not _agrees(fam.function(t), args, expected)}
        needed = any(counts[t] < TRAP_COVERAGE for t in fails)
        # 以打得掉錯誤解的反例為主，留約四分之一一般情況；接近下限時只收還沒覆蓋夠的錯誤解。
        if not fails and plain >= max(8, len(hidden) // 4):
            continue
        if not needed and not covered and len(hidden) >= TRAP_SMALL_HIDDEN - 15:
            continue
        seen.add(key)
        counts.update(fails)
        plain += not fails
        hidden.append({'args': args, 'expected': expected})
        failing.append(sorted(fails))
    missing = [t for t in wrong if counts[t] < TRAP_COVERAGE]
    if missing:
        raise RuntimeError(f'Hidden tests of {family}{level}-{seed} miss trap coverage: {missing}')
    large_from = len(hidden)
    for args in fam.large_cases(rng, level):
        key = json.dumps(args, sort_keys=True)
        if key not in seen:
            seen.add(key)
            hidden.append({'args': args, 'expected': fam.solve(args)})
    task = {'id': f'{family}{level}-{seed}', 'family': family, 'level': level, 'seed': seed,
            'prompt': build_trap_prompt(fam, level, public), 'entry_point': fam.entry,
            'public_tests': public, 'hidden_tests': hidden, 'kind': 'code'}
    return task, {'trap_failures': failing, 'trap_counts': dict(counts), 'large_from': large_from}


def make(family, level, seed):
    return build(family, level, seed)[0]


def generate(families=None, levels=LEVELS, seeds=(0,)):
    """產生題目清單：每個題型、難度、種子各一題。"""
    return [make(f, level, seed) for f in (families or FAMILIES) for level in levels for seed in seeds]


def _digest(items):
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def interleave(tasks):
    """依題型輪流排序，只跑前幾題也能涵蓋各題型；同一題型內依原順序。"""
    queues = {}
    for task in tasks:
        queues.setdefault(task['family'], []).append(task)
    ordered = []
    while any(queues.values()):
        for family in list(queues):
            if queues[family]:
                ordered.append(queues[family].pop(0))
    return ordered


def _set_version(families):
    if families and families <= set(TRAP_FAMILIES):
        return TRAP_VERSION
    if families and families <= set(FAMILIES):
        return VERSION
    if families and families <= set(ticket_families()):
        return _tickets().TICKET_VERSION
    if families and families <= set(repo_families()):
        return _repo_tasks().REPO_VERSION
    raise ValueError('A frozen code task set must contain families of exactly one version (v1, v2, v3 or v4)')


def _record(task):
    record = {'id': task['id'], 'family': task['family'], 'level': task['level'], 'seed': task['seed'],
              'prompt_sha256': _digest(task['prompt']),
              'tests_sha256': _digest([task['public_tests'], task['hidden_tests']])}
    if 'implicit_requirements' in task:
        record['requirements_sha256'] = _digest(task['implicit_requirements'])
    return record


def freeze(path, tasks):
    """凍結題目：存身分、每題 prompt 與測試的雜湊；題目本身由產生器重建後核對。"""
    tasks = interleave(sorted(tasks, key=lambda t: (t['family'], t['level'], t['seed'])))
    frozen = {'version': _set_version({t['family'] for t in tasks}),
              'family_revision': {f: FAMILY_REVISION[f] for f in sorted({t['family'] for t in tasks})},
              'tasks': [_record(t) for t in tasks],
              'prompts_sha256': _digest([t['prompt'] for t in tasks]),
              'tests_sha256': _digest([[t['public_tests'], t['hidden_tests']] for t in tasks])}
    Path(path).write_text(json.dumps(frozen, ensure_ascii=False, indent=1) + '\n')
    return frozen


def load_frozen(frozen):
    version = frozen.get('version')
    if version not in (VERSION, TRAP_VERSION, _tickets().TICKET_VERSION, _repo_tasks().REPO_VERSION) \
            or _set_version({t['family'] for t in frozen['tasks']}) != version:
        raise ValueError('Code task set version changed')
    for family, revision in frozen.get('family_revision', {}).items():
        if FAMILY_REVISION.get(family) != revision:
            raise ValueError('Code task family revision changed: ' + family)
    tasks = [make(t['family'], t['level'], t['seed']) for t in frozen['tasks']]
    for task, record in zip(tasks, frozen['tasks']):
        if _record(task) != record:
            raise ValueError('Frozen code task changed: ' + record['id'])
    if _digest([t['prompt'] for t in tasks]) != frozen['prompts_sha256']:
        raise ValueError('Frozen code task prompts changed')
    return tasks


def frozen_code_tasks(path=FROZEN_CODE_TASKS, limit=None):
    """讀凍結檔並重建題目（凍結檔已是題型交錯的順序）。預設是第三版；舊版用 FROZEN_CODE_TASK_SETS['v1'／'v2']。"""
    tasks = load_frozen(json.loads(Path(path).read_text()))
    return tasks[:limit] if limit else tasks


# ---- 取程式碼、盲測環境、事後計分 ----

CODE_BLOCK = re.compile(r'```[ \t]*(?:python3?|py)[ \t]*\r?\n(.*?)```', re.DOTALL | re.IGNORECASE)


def extract_code(text):
    """取回覆中最後一個 ```python 程式區塊；沒有就回 None。"""
    if not isinstance(text, str):
        return None
    blocks = CODE_BLOCK.findall(text)
    if not blocks or not blocks[-1].strip():
        return None
    return blocks[-1]


def solution_code(task, text):
    """要送進隔離執行器的程式：一般題就是回覆裡的程式區塊；程式庫情境題要把其他檔案一起包進去。"""
    code = extract_code(text)
    if code is None or 'repo' not in task:
        return code
    return _repo_tasks().solution_code(task, code)


def _clip(text):
    return text if len(text) <= CHECK_CHARS else text[:CHECK_CHARS - 3] + '...'


def public_checks(task, outcome):
    """把公開範例的執行結果轉成短代碼；只會引用公開範例（本來就寫在題目裡）。"""
    status = outcome['status']
    if status == 'no_code':
        return ['code_block_missing']
    if status in ('syntax_error', 'sandbox_unavailable', 'crashed'):
        return [status]
    if status == 'timeout' and not outcome['failures']:
        return ['timeout']
    failures = outcome['failures']
    loads = {f['detail'] for f in failures if f['kind'] == 'error' and (
        f['detail'].startswith('load_') or f['detail'] == 'entry_point_missing')}
    if loads:
        return [_clip(sorted(loads)[0])]
    checks = ['timeout'] if status == 'timeout' else []
    if failures:
        checks.append('public_test_failed')
    for failure in failures:
        test = task['public_tests'][failure['index']]
        call = _call(task['entry_point'], test['args'])
        if failure['kind'] == 'wrong':
            detail = f'{call} {failure["detail"]}'
        elif failure['detail'] == 'timeout':
            detail = f'{call} timed out'
        else:
            detail = f'{call} raised {failure["detail"]}'
        checks.append(_clip(f'public_case_{failure["index"] + 1}: {detail}'))
    while checks and len(json.dumps({'passed': False, 'checks': checks}, ensure_ascii=False).encode()) > CHECKS_MAX_BYTES:
        checks.pop()
    return checks


class BlindCodeEnvironment:
    """接上 Core 的盲測環境：沒有工具；``validate`` 只跑公開範例，隱藏測試一律不碰。

    ``require_sandbox=True``（預設）而本機沒有 sandbox-exec 時，``validate`` 回
    ``sandbox_unavailable``；付費試跑前應先確認 ``code_sandbox.sandbox_available()``。
    """
    replayable_tools = ()

    def __init__(self, task, *, require_sandbox=True):
        self.task = task
        self.require_sandbox = require_sandbox

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        return {'ok': False, 'error': {'code': 'unknown_tool', 'category': 'content'}}

    def validate(self, final_text):
        outcome = code_sandbox.run_tests(solution_code(self.task, final_text), self.task['entry_point'],
                                         self.task['public_tests'], require_sandbox=self.require_sandbox)
        passed = outcome['status'] == 'ok' and outcome['passed'] == outcome['total']
        return {'passed': passed, 'checks': [] if passed else public_checks(self.task, outcome)}


def grade_hidden(task, final_text, *, require_sandbox=True):
    """事後計分：隱藏測試全過才算 passed。結果只寫進計分檔，不可回給作答中的模型。"""
    # 第二版有大輸入，整體上限放寬到 HIDDEN_TIMEOUT_S；每個測試仍是 1 秒。
    outcome = code_sandbox.run_tests(solution_code(task, final_text), task['entry_point'], task['hidden_tests'],
                                     timeout_s=HIDDEN_TIMEOUT_S, require_sandbox=require_sandbox)
    total = len(task['hidden_tests'])
    return {'passed': outcome['status'] == 'ok' and total > 0 and outcome['passed'] == total,
            'hidden_passed': outcome['passed'], 'hidden_total': total, 'status': outcome['status']}
