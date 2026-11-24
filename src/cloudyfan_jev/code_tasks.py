"""盲測改程式題：題目產生器、凍結、取程式碼與盲測環境（設計見 plans/mrc-blind-validation-experiment-2026-11-24.md）。

每題要求寫一個純函式。題目文字列出完整規格與 3 個公開範例；公開範例刻意只碰一般情況，
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
from functools import cmp_to_key
import hashlib
import json
from pathlib import Path
import random
import re

from . import code_sandbox

VERSION = 'cloudyfan-code-tasks-v1'
LEVELS = (1, 2, 3, 4, 5)
# 各題型產生器版本；校準後改題時只升該題型的版本，其餘題目不變。
FAMILY_REVISION = {'A': 1, 'B': 1, 'C': 1, 'D': 1, 'E': 1, 'F': 1, 'G': 1, 'H': 1}
FROZEN_CODE_TASKS = Path(__file__).resolve().parents[2] / 'docs/evidence/code-tasks-20261124.json'
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


def build(family, level, seed):
    """產生一題（含隱藏測試與各隱藏測試碰到的邊界條款）；同樣的 (題型, 難度, 種子) 一定得到同一題。"""
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


def freeze(path, tasks):
    """凍結題目：存身分、每題 prompt 與測試的雜湊；題目本身由產生器重建後核對。"""
    tasks = interleave(sorted(tasks, key=lambda t: (t['family'], t['level'], t['seed'])))
    frozen = {'version': VERSION, 'family_revision': {f: FAMILY_REVISION[f] for f in sorted({t['family'] for t in tasks})},
              'tasks': [{'id': t['id'], 'family': t['family'], 'level': t['level'], 'seed': t['seed'],
                         'prompt_sha256': _digest(t['prompt']),
                         'tests_sha256': _digest([t['public_tests'], t['hidden_tests']])} for t in tasks],
              'prompts_sha256': _digest([t['prompt'] for t in tasks]),
              'tests_sha256': _digest([[t['public_tests'], t['hidden_tests']] for t in tasks])}
    Path(path).write_text(json.dumps(frozen, ensure_ascii=False, indent=1) + '\n')
    return frozen


def load_frozen(frozen):
    if frozen.get('version') != VERSION:
        raise ValueError('Code task set version changed')
    for family, revision in frozen.get('family_revision', {}).items():
        if FAMILY_REVISION.get(family) != revision:
            raise ValueError('Code task family revision changed: ' + family)
    tasks = [make(t['family'], t['level'], t['seed']) for t in frozen['tasks']]
    for task, record in zip(tasks, frozen['tasks']):
        if task['id'] != record['id'] or _digest(task['prompt']) != record['prompt_sha256'] \
                or _digest([task['public_tests'], task['hidden_tests']]) != record['tests_sha256']:
            raise ValueError('Frozen code task changed: ' + record['id'])
    if _digest([t['prompt'] for t in tasks]) != frozen['prompts_sha256']:
        raise ValueError('Frozen code task prompts changed')
    return tasks


def frozen_code_tasks(path=FROZEN_CODE_TASKS, limit=None):
    """讀凍結檔並重建題目（凍結檔已是題型交錯的順序）。"""
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
        outcome = code_sandbox.run_tests(extract_code(final_text), self.task['entry_point'],
                                         self.task['public_tests'], require_sandbox=self.require_sandbox)
        passed = outcome['status'] == 'ok' and outcome['passed'] == outcome['total']
        return {'passed': passed, 'checks': [] if passed else public_checks(self.task, outcome)}


def grade_hidden(task, final_text, *, require_sandbox=True):
    """事後計分：隱藏測試全過才算 passed。結果只寫進計分檔，不可回給作答中的模型。"""
    outcome = code_sandbox.run_tests(extract_code(final_text), task['entry_point'], task['hidden_tests'],
                                     require_sandbox=require_sandbox)
    total = len(task['hidden_tests'])
    return {'passed': outcome['status'] == 'ok' and total > 0 and outcome['passed'] == total,
            'hidden_passed': outcome['passed'], 'hidden_total': total, 'status': outcome['status']}
