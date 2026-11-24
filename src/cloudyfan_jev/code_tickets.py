"""第三版盲測改程式題：工單式題目（Q～X）。設計見 plans/mrc-blind-validation-experiment-2026-11-24.md。

前兩版的規格把邊界和陷阱都寫明了，連 Haiku 5.5 低強度都全對。第三版模擬真實工作「規格不完整」：
- 新功能題（Q、S、U、W）：一張 PM 寫的簡短工單加 3 個範例，不逐條列邊界。
- 修 bug 題（R、T、V、X）：一段既有程式（含 bug）加一則使用者回報；第一個範例就是回報的重現例子，
  另外兩個是目前就正確、修完也要維持的行為。

隱藏測試只檢查「工單沒寫出來、但一般工程師都會同意」的隱含要求。公平性的做法：
- 每條隱含要求是一個「類別」。參考解 ``_solve(..., skip)`` 可以單獨關掉一條（探針）；
  某個輸入讓探針的答案改變，就代表這個輸入「碰到」那條要求。
- 每一級列出這級要測的要求（``reqs``，寫進 task 的 ``implicit_requirements`` 給審查用，絕不進 prompt），
  以及題目本身已寫明的情況（``explicit``，例如回報的重現情況）。隱藏測試只收「碰到的要求都在這兩組內」的輸入，
  所以同一張工單在不同級數只差在隱藏測試檢查幾條隱含要求。
- 合理工程師可能有不同做法的點（同分順序、四捨五入方向、2 月 29 日生日、email 本地部分大小寫……），
  產生器根本不產生；tests/test_code_tickets.py 另用幾種「做法不同但合理」的寫法確認它們全過。
- 修 bug 題的隱藏測試至少有 REGRESSION_MIN 個回歸測試（既有程式本來就答對的輸入）。
每級至少兩個典型錯誤解（只照字面做、只修症狀、忽略領域慣例、或改壞原有行為），都要過全部公開範例、
並在至少 TICKET_COVERAGE 個隱藏測試上失敗；``trap_breaks`` 記它違反哪幾條要求，測試確認每個失敗都能歸到其中一條。
"""
from collections import Counter
import calendar
from datetime import date, timedelta
from functools import lru_cache
import json

from . import code_sandbox
from .code_tasks import PUBLIC_COUNT, REPLY_RULES, _call, _rng

TICKET_VERSION = 'cloudyfan-code-tasks-v3'
TICKET_COVERAGE = 3
TICKET_HIDDEN_MIN = 40
TICKET_HIDDEN_MAX = 52
REGRESSION_MIN = 6
REGRESSION = 'regression'

FEATURE_INTRO = "You are picking up this ticket from the team's issue tracker."
BUG_INTRO = 'A user filed the bug report below against this function from our codebase. Fix the bug.'
BUG_EXAMPLES = ('After the fix, these calls must return the values shown. The first one is the case from the report; '
                'the others already work today.')


def _json_copy(value):
    return json.loads(json.dumps(value))


def _same(function, args, expected):
    try:
        return code_sandbox.strict_equal(_json_copy(function(*_json_copy(args))), expected)
    except Exception:
        return False


@lru_cache(maxsize=None)
def _namespace(source):
    namespace = {'__name__': 'cloudyfan_ticket'}
    exec(compile(source, '<ticket>', 'exec'), namespace)
    return namespace


def _fill(template, **parts):
    for name, text in parts.items():
        template = template.replace(f'<<{name}>>', text)
    return template


class TicketFamily:
    """一類工單題。同一類的 5 級共用參考解；級數決定工單版本或回報、要測的隱含要求與錯誤解。"""
    letter = name = kind = entry = ''
    reference_source = ''
    tickets = {}        # 新功能題：variant -> (標題, 內文)
    signatures = {}     # 新功能題：variant -> 給模型看的函式簽名
    originals = {}      # 修 bug 題：variant -> 既有程式（含 bug）
    reports = {}        # 修 bug 題：level -> 回報；{call}、{got}、{expected} 由產生器填入
    trap_sources = {}
    trap_breaks = {}    # 錯誤解 -> 它違反的要求
    trap_variants = {}  # 錯誤解 -> 適用的 variant（簽名不同時才需要；沒列就是全部適用）
    requirements = {}   # 要求 -> (要求內容, 為什麼一般工程師會同意, 依據)
    probe_skips = {}    # 要求 -> 參考解裡對應的 skip 名稱（預設就是要求本身）
    levels = {}         # level -> {'variant', 'reqs', 'explicit', 'traps', 'repro'}

    def reference(self):
        return _namespace(self.reference_source)[self.entry]

    def trap(self, name):
        return _namespace(self.trap_sources[name])[self.entry]

    def fits(self, trap, level):
        return self.levels[level]['variant'] in self.trap_variants.get(trap, (self.levels[level]['variant'],))

    def original(self, level):
        return _namespace(self.originals[self.levels[level]['variant']])[self.entry]

    def solve(self, args, skip=()):
        return _namespace(self.reference_source)['_solve'](*_json_copy(args), skip=frozenset(skip))

    def categories(self):
        return [c for c in self.requirements if c != REGRESSION]

    def touches(self, args, expected):
        """這個輸入碰到哪些隱含要求：關掉該要求的探針答案和參考解不同（或丟例外）。"""
        touched = set()
        for category in self.categories():
            for skip in self.probe_skips.get(category, (category,)):
                try:
                    same = code_sandbox.strict_equal(_json_copy(self.solve(args, {skip})), expected)
                except Exception:
                    same = False
                if not same:
                    touched.add(category)
                    break
        return touched

    def requirement_records(self, level):
        ids = list(self.levels[level]['reqs']) + ([REGRESSION] if self.kind == 'bugfix' else [])
        return [dict(zip(('id', 'requirement', 'why', 'basis'), (c,) + self.requirements[c])) for c in ids]

    def public_case(self, rng, level, index):
        """index 是第幾個公開範例（修 bug 題的第 0 個是回報的重現例子），讓各範例呈現不同的一般寫法。"""
        raise NotImplementedError

    def random_case(self, rng, level):
        raise NotImplementedError

    def plain_enough(self, level, args, expected):
        return True


# ---- Q：會議空檔（新功能）----

Q_REFERENCE = r'''
def _minutes(hhmm):
    hours, minutes = hhmm.split(':')
    return int(hours) * 60 + int(minutes)


def _hhmm(total):
    return '%02d:%02d' % divmod(total, 60)


def _solve(busy, day_start, day_end, min_minutes, skip=frozenset()):
    start, end = _minutes(day_start), _minutes(day_end)
    blocks = [(_minutes(s), _minutes(e)) for s, e in busy]
    if 'q_unsorted' not in skip:
        blocks.sort()
    shortest = min_minutes + 1 if 'q_at_least' in skip else min_minutes
    slots, cursor = [], start
    for s, e in blocks:
        if 'q_outside_day' not in skip:
            s, e = max(s, start), min(e, end)
            if s >= e:
                continue
        if s - cursor >= shortest:
            slots.append([_hhmm(cursor), _hhmm(s)])
        cursor = e if 'q_overlap' in skip else max(cursor, e)
    if end - cursor >= shortest:
        slots.append([_hhmm(cursor), _hhmm(end)])
    return slots


def free_slots(busy, day_start, day_end, min_minutes):
    return _solve(busy, day_start, day_end, min_minutes)
'''

_Q_TRAP = r'''
def free_slots(busy, day_start, day_end, min_minutes):
    def to_min(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    def to_text(x):
        return "%02d:%02d" % divmod(x, 60)

    start, end = to_min(day_start), to_min(day_end)
    meetings = <<order>>
    free, cursor = [], start
    for s, e in meetings:
<<clip>>        if s - cursor <<compare>> min_minutes:
            free.append([to_text(cursor), to_text(s)])
        cursor = <<advance>>
    if end - cursor <<compare>> min_minutes:
        free.append([to_text(cursor), to_text(end)])
    return free
'''
_Q_SORTED = 'sorted((to_min(s), to_min(e)) for s, e in busy)'
_Q_CLIP = '        s, e = max(s, start), min(e, end)\n        if s >= e:\n            continue\n'
Q_TRAPS = {
    # 照收到的順序處理（以為已排序）。
    'unsorted': _fill(_Q_TRAP, order='[(to_min(s), to_min(e)) for s, e in busy]', clip=_Q_CLIP, compare='>=',
                      advance='max(cursor, e)'),
    # 游標跟著「上一個會議」的結束時間走，被包住的短會議會把游標拉回來。
    'last_end': _fill(_Q_TRAP, order=_Q_SORTED, clip=_Q_CLIP, compare='>=', advance='e'),
    # 沒把忙碌時段裁到工作時間內，下班後的會議前面會冒出超出工作時間的空檔。
    'no_clip': _fill(_Q_TRAP, order=_Q_SORTED, clip='', compare='>=', advance='max(cursor, e)'),
    # 「at least」寫成大於。
    'strict_min': _fill(_Q_TRAP, order=_Q_SORTED, clip=_Q_CLIP, compare='>', advance='max(cursor, e)'),
}


def _q_text(total):
    return '%02d:%02d' % divmod(total, 60)


class FreeSlots(TicketFamily):
    letter, name, kind, entry = 'Q', 'free_slots', 'feature', 'free_slots'
    reference_source, trap_sources = Q_REFERENCE, Q_TRAPS
    tickets = {'ticket': (
        'Suggest free meeting slots',
        "The scheduling assistant should suggest times when everyone invited to a meeting is free. Please add "
        "`free_slots(busy, day_start, day_end, min_minutes)`. `busy` holds the busy periods pulled from the "
        "attendees' calendars for that day; each one is a `[start, end]` pair of 24-hour \"HH:MM\" strings. Return "
        "the free periods between `day_start` and `day_end` (also \"HH:MM\") as `[start, end]` pairs in "
        "chronological order, keeping only the ones that are at least `min_minutes` long.")}
    signatures = {'ticket': 'free_slots(busy, day_start, day_end, min_minutes)'}
    trap_breaks = {'unsorted': {'q_unsorted'}, 'last_end': {'q_overlap'}, 'no_clip': {'q_outside_day'},
                   'strict_min': {'q_at_least'}}
    requirements = {
        'q_unsorted': ('busy 不保證依時間排序，要自己排序。',
                       '資料是從多位與會者的行事曆合併來的，工單沒有說排過序。', 'convention'),
        'q_overlap': ('不同人的忙碌時段會重疊，或一段整個包住另一段；要等所有重疊的時段都結束才算空檔。',
                      '工單的目的是找「每個人都有空」的時間，被任何一個人占用就不是空檔。', 'ticket'),
        'q_outside_day': ('超出 day_start～day_end 的忙碌時段只算工作時間內的部分；回傳的空檔不可超出工作時間。',
                          '工單寫明只回傳 day_start 到 day_end 之間的空檔；行事曆本來就會有上班前或下班後的行程。',
                          'ticket'),
        'q_at_least': ('長度剛好等於 min_minutes 的空檔要保留。', '工單寫的是「at least min_minutes」。', 'ticket'),
    }
    levels = {
        1: {'variant': 'ticket', 'reqs': ('q_overlap', 'q_at_least'), 'traps': ('last_end', 'strict_min')},
        2: {'variant': 'ticket', 'reqs': ('q_unsorted', 'q_at_least'), 'traps': ('unsorted', 'strict_min')},
        3: {'variant': 'ticket', 'reqs': ('q_unsorted', 'q_overlap'), 'traps': ('unsorted', 'last_end')},
        4: {'variant': 'ticket', 'reqs': ('q_overlap', 'q_outside_day', 'q_at_least'),
            'traps': ('last_end', 'no_clip', 'strict_min')},
        5: {'variant': 'ticket', 'reqs': ('q_unsorted', 'q_overlap', 'q_outside_day', 'q_at_least'),
            'traps': ('unsorted', 'last_end', 'no_clip', 'strict_min')},
    }

    def _day(self, rng):
        start = rng.choice((7 * 60, 8 * 60, 8 * 60 + 30, 9 * 60, 10 * 60))
        end = rng.choice((16 * 60, 17 * 60, 17 * 60 + 30, 18 * 60, 19 * 60))
        return start, end, rng.choice((15, 20, 30, 45, 60))

    @staticmethod
    def _args(blocks, start, end, minimum):
        return [[[_q_text(s), _q_text(e)] for s, e in blocks], _q_text(start), _q_text(end), minimum]

    def public_case(self, rng, level, index):
        start, end, minimum = self._day(rng)
        blocks, cursor = [], start + 5 * rng.randint(0, 6)
        for _ in range(rng.randint(2, 4)):
            s = cursor + 5 * rng.choice((0, rng.randint(2, 30)))
            e = s + 5 * rng.randint(3, 18)
            if e > end:
                break
            blocks.append((s, e))
            cursor = e
        return self._args(blocks, start, end, minimum)

    def random_case(self, rng, level):
        start, end, minimum = self._day(rng)
        blocks, cursor = [], start + 5 * rng.choice((0, 0, rng.randint(-24, 12)))
        for _ in range(rng.randint(0, 5)):
            gap = rng.choice((0, minimum, minimum, minimum - 5, minimum + 5, 5 * rng.randint(1, 24)))
            s = cursor + gap
            e = min(s + 5 * rng.randint(2, 24), 23 * 60 + 55)
            if s >= e:
                break
            blocks.append((s, e))
            cursor = e
        extra = []
        for s, e in blocks:
            roll = rng.random()
            if roll < .25 and e - s >= 20:
                extra.append((s + 5 * rng.randint(1, (e - s) // 10), s + 5 * rng.randint((e - s) // 10 + 1,
                                                                                        (e - s) // 5 - 1)))
            elif roll < .35:
                extra.append((s + 5, min(e + 5 * rng.randint(1, 12), 23 * 60 + 55)))
        if rng.random() < .35:
            after = end + 5 * rng.randint(0, 24)
            extra.append((after, min(after + 5 * rng.randint(3, 18), 23 * 60 + 55)))
        if rng.random() < .25:
            before = max(0, start - 5 * rng.randint(6, 30))
            extra.append((before, before + 5 * rng.randint(2, 6)))
        blocks = [b for b in blocks + extra if 0 <= b[0] < b[1] <= 23 * 60 + 55]
        blocks = list({b[0]: b for b in blocks}.values())
        if rng.random() < .5:
            rng.shuffle(blocks)
        else:
            blocks.sort()
        return self._args(blocks, start, end, minimum)

    def plain_enough(self, level, args, expected):
        return len(args[0]) >= 2 and len(expected) >= 1


# ---- R：日期加月份會當掉（修 bug）----

R_ORIGINAL = r'''from datetime import date, timedelta

UNITS = ("days", "weeks", "months", "years")


def add_period(day, amount, unit):
    """Shift an ISO date ("YYYY-MM-DD") by `amount` units; a negative amount goes back.

    Billing uses this for renewal dates, trial ends and grace periods.
    """
    if unit not in UNITS:
        raise ValueError(f"unknown unit: {unit}")
    start = date.fromisoformat(day)
    if unit == "days":
        return (start + timedelta(days=amount)).isoformat()
    if unit == "weeks":
        return (start + timedelta(weeks=amount)).isoformat()
    if unit == "months":
        # zero-based month index, so that the year rolls over correctly
        index = start.year * 12 + start.month - 1 + amount
        year, month0 = divmod(index, 12)
        return start.replace(year=year, month=month0 + 1).isoformat()
    return start.replace(year=start.year + amount).isoformat()
'''

R_REFERENCE = r'''
import calendar
from datetime import date, timedelta


def _solve(day, amount, unit, skip=frozenset()):
    start = date.fromisoformat(day)
    if unit == 'days':
        return (start + timedelta(days=amount)).isoformat()
    if unit == 'weeks':
        return (start + timedelta(weeks=amount)).isoformat()
    if unit == 'months':
        year, month0 = divmod(start.year * 12 + start.month - 1 + amount, 12)
        month = month0 + 1
    elif unit == 'years':
        year, month = start.year + amount, start.month
    else:
        raise ValueError('unknown unit: ' + str(unit))
    last = calendar.monthrange(year, month)[1]
    if start.day <= last:
        return start.replace(year=year, month=month).isoformat()
    if unit == 'years':
        kind = 'r_years'
    elif month == 2:
        kind = 'r_feb_leap' if last == 29 else 'r_feb_common'
    else:
        kind = 'r_thirty'
    if kind in skip or (unit == 'months' and start.day < 31 and 'r_late_day' in skip):
        raise ValueError('day is out of range for month')
    return date(year, month, last).isoformat()


def add_period(day, amount, unit):
    return _solve(day, amount, unit)
'''

_R_TRAP = r'''import calendar
from datetime import date, timedelta

UNITS = ("days", "weeks", "months", "years")


def add_period(day, amount, unit):
    if unit not in UNITS:
        raise ValueError(f"unknown unit: {unit}")
    start = date.fromisoformat(day)
    if unit == "days":
        return (start + timedelta(days=amount)).isoformat()
    if unit == "weeks":
        return (start + timedelta(weeks=amount)).isoformat()
    if unit == "months":
<<months>>
<<years>>
'''
_R_INDEX = '        index = start.year * 12 + start.month - 1 + amount\n        year, month0 = divmod(index, 12)\n'
_R_MONTHS = {
    'original': _R_INDEX + '        return start.replace(year=year, month=month0 + 1).isoformat()',
    'clamp': _R_INDEX + '        last = calendar.monthrange(year, month0 + 1)[1]\n'
                        '        return start.replace(year=year, month=month0 + 1, day=min(start.day, last)).isoformat()',
    'feb_only': _R_INDEX + '        if month0 + 1 == 2:\n'
                           '            last = 29 if calendar.isleap(year) else 28\n'
                           '            return start.replace(year=year, month=2, day=min(start.day, last)).isoformat()\n'
                           '        return start.replace(year=year, month=month0 + 1).isoformat()',
    'divmod_rewrite': '        total = start.month + amount\n'
                      '        year = start.year + total // 12\n'
                      '        month = total % 12 or 12\n'
                      '        last = calendar.monthrange(year, month)[1]\n'
                      '        return start.replace(year=year, month=month, day=min(start.day, last)).isoformat()',
    'thirty_only': _R_INDEX + '        if start.day == 31 and month0 + 1 in (4, 6, 9, 11):\n'
                              '            return start.replace(year=year, month=month0 + 1, day=30).isoformat()\n'
                              '        return start.replace(year=year, month=month0 + 1).isoformat()',
    'minus_one_retry': _R_INDEX + '        try:\n'
                                  '            return start.replace(year=year, month=month0 + 1).isoformat()\n'
                                  '        except ValueError:\n'
                                  '            return start.replace(year=year, month=month0 + 1, '
                                  'day=start.day - 1).isoformat()',
    'feb28_only': _R_INDEX + '        if month0 + 1 == 2:\n'
                             '            return start.replace(year=year, month=2, day=min(start.day, 28)).isoformat()\n'
                             '        return start.replace(year=year, month=month0 + 1).isoformat()',
    'only_31': _R_INDEX + '        if start.day == 31:\n'
                          '            last = calendar.monthrange(year, month0 + 1)[1]\n'
                          '            return start.replace(year=year, month=month0 + 1, day=last).isoformat()\n'
                          '        return start.replace(year=year, month=month0 + 1).isoformat()',
    'day28': _R_INDEX + '        try:\n'
                        '            return start.replace(year=year, month=month0 + 1).isoformat()\n'
                        '        except ValueError:\n'
                        '            return start.replace(year=year, month=month0 + 1, day=28).isoformat()',
}
_R_YEARS = {
    'original': '    return start.replace(year=start.year + amount).isoformat()',
    'clamp': '    last = calendar.monthrange(start.year + amount, start.month)[1]\n'
             '    return start.replace(year=start.year + amount, day=min(start.day, last)).isoformat()',
    'day28': '    try:\n'
             '        return start.replace(year=start.year + amount).isoformat()\n'
             '    except ValueError:\n'
             '        return start.replace(year=start.year + amount, day=28).isoformat()',
}


def _r_trap(months, years='original'):
    return _fill(_R_TRAP, months=_R_MONTHS[months], years=_R_YEARS[years])


R_TRAPS = {
    'feb_only': _r_trap('feb_only'),               # 只處理回報裡的 2 月
    'rewrite_divmod': _r_trap('divmod_rewrite'),   # 修好月底，卻把跨年算錯（目標是 12 月時年份錯）
    'thirty_only': _r_trap('thirty_only'),         # 只處理 31 日 → 30 天月份
    'minus_one_retry': _r_trap('minus_one_retry'),  # 出錯就往前一天再試一次
    'feb28_only': _r_trap('feb28_only'),           # 2 月一律最多 28 日
    'months_only': _r_trap('clamp'),               # 月的分支修好，年的分支沒修
    'only_31': _r_trap('only_31'),                 # 只處理 31 日起始
    'years_only': _r_trap('original', 'clamp'),    # 年的分支修好，月的分支沒修
    'except_day28': _r_trap('day28', 'day28'),     # 出錯就改成 28 日
}


class AddPeriod(TicketFamily):
    letter, name, kind, entry = 'R', 'add_period', 'bugfix', 'add_period'
    reference_source, trap_sources = R_REFERENCE, R_TRAPS
    originals = {'period': R_ORIGINAL}
    trap_breaks = {
        'feb_only': {'r_thirty', 'r_years'},
        'rewrite_divmod': {REGRESSION, 'r_years'},
        'thirty_only': {'r_feb_leap', 'r_feb_common', 'r_late_day', 'r_years'},
        'minus_one_retry': {'r_feb_leap', 'r_feb_common', 'r_late_day', 'r_years'},
        'feb28_only': {'r_feb_leap', 'r_thirty', 'r_years', REGRESSION},  # 也把閏年 1/29 → 2/29 改壞
        'months_only': {'r_years'},
        'only_31': {'r_late_day', 'r_years'},
        'years_only': {'r_feb_leap', 'r_feb_common', 'r_thirty', 'r_late_day'},
        'except_day28': {'r_feb_leap', 'r_thirty', 'r_late_day'},
    }
    _why = '同一個根本原因（目標月份沒有起始日那一天）；回報要的規則是「落在該月最後一天」，所有主流日期函式庫也都這樣處理。'
    requirements = {
        'r_feb_leap': ('目標月份是閏年 2 月、起始日超過 29 日時，結果是 2 月 29 日。', _why, 'convention'),
        'r_feb_common': ('目標月份是平年 2 月、起始日超過 28 日時，結果是 2 月 28 日。', _why, 'convention'),
        'r_thirty': ('目標月份是 4、6、9、11 月（30 天）且起始日是 31 日時，結果是 30 日。', _why, 'convention'),
        'r_late_day': ('起始日是 29 或 30 日、目標月份沒有這一天時也落在月底（不只 31 日會出事）。', _why,
                       'convention'),
        'r_years': ('以年為單位時，2 月 29 日加減到平年，結果是 2 月 28 日。',
                    '年的分支有同一個根本原因；dateutil、Java、.NET、Excel EDATE 都這樣處理。', 'convention'),
        REGRESSION: ('原本正常的輸入結果不變：目標月份有那一天、跨年（含目標是 12 月）、負數、以日或週為單位。',
                     '修 bug 不能改壞既有行為；這些輸入舊程式本來就答對。', 'existing_code'),
    }
    reports = {
        1: 'Monthly renewals crash for customers who signed up on January 31st. `{call}` {got}. February has no '
           '31st, so the renewal should fall on the last day of February: expected {expected}.',
        2: "Last night's job that schedules next month's invoices failed. `{call}` {got}. There is no April 31st, so "
           'the invoice date should be the last day of April: expected {expected}.',
        3: 'A trial that started on January 29th, 2025 broke the trial-end email. `{call}` {got}. 2025 is not a leap '
           'year, so the trial should end on the last day of February: expected {expected}.',
        4: "The 'previous billing date' lookup crashes at the end of March. `{call}` {got}. The previous billing date "
           'should be the last day of February: expected {expected}.',
        5: 'Annual plans bought on a leap day cannot be renewed. `{call}` {got}. 2025 has no February 29th, so the '
           'renewal should fall on the last day of February: expected {expected}.',
    }
    levels = {
        1: {'variant': 'period', 'reqs': ('r_feb_common', 'r_thirty'), 'explicit': ('r_feb_leap',),
            'traps': ('feb_only', 'rewrite_divmod'), 'repro': ['2024-01-31', 1, 'months']},
        2: {'variant': 'period', 'reqs': ('r_feb_leap', 'r_feb_common'), 'explicit': ('r_thirty',),
            'traps': ('thirty_only', 'minus_one_retry'), 'repro': ['2025-03-31', 1, 'months']},
        3: {'variant': 'period', 'reqs': ('r_feb_leap', 'r_thirty'), 'explicit': ('r_feb_common', 'r_late_day'),
            'traps': ('feb28_only', 'feb_only'), 'repro': ['2025-01-29', 1, 'months']},
        4: {'variant': 'period', 'reqs': ('r_thirty', 'r_feb_common', 'r_late_day', 'r_years'),
            'explicit': ('r_feb_leap',), 'traps': ('months_only', 'feb_only', 'only_31'),
            'repro': ['2024-03-31', -1, 'months']},
        5: {'variant': 'period', 'reqs': ('r_feb_leap', 'r_feb_common', 'r_thirty', 'r_late_day'),
            'explicit': ('r_years',), 'traps': ('years_only', 'except_day28'), 'repro': ['2024-02-29', 1, 'years']},
    }

    @staticmethod
    def _date(rng, late):
        year, month = rng.randint(2019, 2031), rng.randint(1, 12)
        last = calendar.monthrange(year, month)[1]
        day = rng.randint(max(1, last - 3), last) if late else rng.randint(1, 28)
        return date(year, month, day)

    def public_case(self, rng, level, index):
        unit = rng.choice(('months', 'months', 'months', 'years', 'days', 'weeks'))
        amount = {'months': rng.choice((1, 2, 3, -1, 6, 12, 14)), 'years': rng.choice((1, 2, -1)),
                  'days': rng.choice((7, 30, 45, 90)), 'weeks': rng.choice((1, 2, 4))}[unit]
        return [self._date(rng, False).isoformat(), amount, unit]

    def random_case(self, rng, level):
        unit = rng.choices(('months', 'years', 'days', 'weeks'), weights=(6, 2, 1, 1))[0]
        start = self._date(rng, rng.random() < .7)
        if unit == 'years' and rng.random() < .6:
            start = date(rng.choice((2020, 2024, 2028)), 2, 29)
        amount = {'months': rng.choice((1, 1, 1, -1, -1, 2, 3, -3, 6, 11, 12, 13, -12, rng.randint(-30, 30))),
                  'years': rng.choice((1, 1, 2, 3, 4, -1, -3, -4)),
                  'days': rng.randint(-400, 400), 'weeks': rng.randint(-60, 60)}[unit]
        return [start.isoformat(), amount, unit]


# ---- S：結帳金額（新功能）----

S_REFERENCE = r'''
def _solve(text, skip=frozenset()):
    cleaned = text if 's_spaces' in skip else text.strip()
    negative = cleaned.startswith('-')
    if negative:
        cleaned = cleaned[1:]
    if cleaned.startswith('$'):
        cleaned = cleaned[1:]
    cleaned = cleaned.replace(',', '')
    if 's_exact' in skip:
        cents = int(float(cleaned) * 100)
        return -cents if negative else cents
    dollars, _, fraction = cleaned.partition('.')
    cents = int(fraction or 0) if 's_one_decimal' in skip else int((fraction + '00')[:2])
    if negative and 's_negative' in skip:
        return -int(dollars) * 100 + cents
    total = int(dollars) * 100 + cents
    return -total if negative else total


def parse_amount(text):
    return _solve(text)
'''

S_TRAPS = {
    # 轉成浮點數乘 100 再截斷：0.29 → 28。
    'float_trunc': r'''
def parse_amount(text):
    cleaned = text.strip().replace("$", "").replace(",", "")
    return int(float(cleaned) * 100)
''',
    # 小數點後直接當分：7.5 → 705。
    'split_frac': r'''
def parse_amount(text):
    cleaned = text.strip().replace("$", "").replace(",", "")
    negative = cleaned.startswith("-")
    if negative:
        cleaned = cleaned[1:]
    dollars, _, cents = cleaned.partition(".")
    value = int(dollars) * 100 + int(cents or 0)
    return -value if negative else value
''',
    # 負號只作用在元：-7.50 → -650。
    'sign_on_dollars': r'''
def parse_amount(text):
    cleaned = text.strip().replace("$", "").replace(",", "")
    dollars, _, cents = cleaned.partition(".")
    return int(dollars) * 100 + int(cents.ljust(2, "0"))
''',
    # 只拿掉開頭的錢號，沒去掉前後空白。
    'dollar_prefix_only': r'''
def parse_amount(text):
    if text.startswith("-"):
        return -parse_amount(text[1:])
    if text.startswith("$"):
        text = text[1:]
    return round(float(text.replace(",", "")) * 100)
''',
}


class ParseAmount(TicketFamily):
    letter, name, kind, entry = 'S', 'parse_amount', 'feature', 'parse_amount'
    reference_source, trap_sources = S_REFERENCE, S_TRAPS
    _body = ('The "Amount (USD)" box on the pay-what-you-want page is a free-text field. Please add '
             '`parse_amount(text)` that turns what the customer typed into an integer number of cents for the payment '
             'API. People type the amount with or without a dollar sign and with or without thousands separators, for '
             'example `$1,250.00` or `1250`. The form already rejects anything that is not a dollar amount, but it '
             'passes the text on exactly as typed.')
    tickets = {'checkout': ('Parse the amount customers type at checkout', _body),
               'refunds': ('Parse the amount customers type at checkout',
                           _body + ' Support agents will use the same function on the refund screen, where a refund '
                                   'is typed with a leading minus sign, for example `-$5.00` or `-5`.')}
    signatures = {'checkout': 'parse_amount(text)', 'refunds': 'parse_amount(text)'}
    trap_breaks = {'float_trunc': {'s_exact'}, 'split_frac': {'s_one_decimal'}, 'sign_on_dollars': {'s_negative'},
                   'dollar_prefix_only': {'s_spaces'}}
    requirements = {
        's_exact': ('結果要精確到分，不可因浮點數誤差少一分（例如 "0.29" 要得到 29，不是 28）。',
                    '金額要拿去扣款，差一分錢就是錯帳；處理金錢不能把浮點數直接截斷是通則。', 'convention'),
        's_one_decimal': ('只有一位小數時代表幾角（"7.5" 是 750 分，不是 705）。',
                          '工單說輸入是美元金額；$7.5 就是 7 美元 50 分，是一般人的讀法。', 'convention'),
        's_spaces': ('輸入前後的空白要忽略（例如 " $12.00"）。',
                     '工單寫明表單「照使用者打的原樣」傳進來；自由輸入欄位前後多打空白很常見，一般都先去掉。', 'ticket'),
        's_negative': ('退款的負號作用在整個金額，包含角分（"-$7.50" 是 -750，"-0.50" 是 -50）。',
                       '工單寫明退款用前置負號表示；負號代表整個金額為負，是一般算術。', 'ticket'),
    }
    levels = {
        1: {'variant': 'checkout', 'reqs': ('s_exact', 's_one_decimal'), 'traps': ('float_trunc', 'split_frac')},
        2: {'variant': 'checkout', 'reqs': ('s_exact', 's_spaces'), 'traps': ('float_trunc', 'dollar_prefix_only')},
        3: {'variant': 'refunds', 'reqs': ('s_one_decimal', 's_negative'), 'traps': ('split_frac', 'sign_on_dollars')},
        4: {'variant': 'refunds', 'reqs': ('s_exact', 's_negative', 's_spaces'),
            'traps': ('float_trunc', 'sign_on_dollars', 'dollar_prefix_only')},
        5: {'variant': 'refunds', 'reqs': ('s_exact', 's_one_decimal', 's_spaces', 's_negative'),
            'traps': ('float_trunc', 'split_frac', 'sign_on_dollars', 'dollar_prefix_only')},
    }

    @staticmethod
    def _text(rng, cents, negative, spaces):
        dollars, rest = divmod(cents, 100)
        digits = f'{dollars:,}' if rng.random() < .5 else str(dollars)
        if rest == 0 and rng.random() < .4:
            fraction = ''
        elif rest % 10 == 0 and rng.random() < .5:
            fraction = '.' + str(rest // 10)
        else:
            fraction = f'.{rest:02d}'
        text = ('-' if negative else '') + ('$' if rng.random() < .6 else '') + digits + fraction
        if spaces:
            left, right = rng.choice(((' ', ''), ('  ', ''), ('', ' '), (' ', ' '), (' ', '  ')))
            text = left + text + right
        return text

    @staticmethod
    def _cents(rng):
        return rng.choice((rng.randint(1, 999), rng.randint(100, 99999), rng.randint(10 ** 5, 10 ** 9)))

    def public_case(self, rng, level, index):
        dollars, cents = rng.randint(5, 900), rng.choice((0, 0, 50, 99, 25, rng.randint(1, 99)))
        if index == 0:
            return [f'${rng.randint(1, 30)},{rng.randint(0, 999):03d}.{cents:02d}']
        if index == 1:
            return [str(dollars) if cents == 0 else f'{dollars}.{cents:02d}']
        if self.levels[level]['variant'] == 'refunds':
            return [rng.choice((f'-${dollars}.00', f'-{dollars}'))]
        return [f'${dollars}.{cents:02d}']

    def random_case(self, rng, level):
        negative = self.levels[level]['variant'] == 'refunds' and rng.random() < .4
        return [self._text(rng, self._cents(rng), negative, rng.random() < .3)]


# ---- T：側邊欄排序，數字被當文字比（修 bug）----

T_ORIGINAL = r'''def sidebar_order(items):
    """Order library items for the sidebar.

    `items` is a list of dicts such as {"title": "Episode 3", "pinned": True};
    "pinned" may be missing, which means not pinned. Pinned items come first,
    then the rest; each group is sorted alphabetically, ignoring case.
    Returns the titles in display order.
    """
    pinned = [item for item in items if item.get("pinned")]
    others = [item for item in items if not item.get("pinned")]

    def by_title(item):
        return item["title"].lower()

    ordered = sorted(pinned, key=by_title) + sorted(others, key=by_title)
    return [item["title"] for item in ordered]


def sidebar_sections(items):
    """Split the ordered titles into the "Pinned" and "Library" sections."""
    pinned_count = sum(1 for item in items if item.get("pinned"))
    titles = sidebar_order(items)
    return {"Pinned": titles[:pinned_count], "Library": titles[pinned_count:]}
'''

T_REFERENCE = r'''
import re


def _key(title, skip):
    parts = re.split(r'(\d+)', title.lower())
    count = len(parts) // 2
    key = []
    for i, part in enumerate(parts):
        k = i // 2
        if i % 2 == 0:
            key.append(part)
        elif ('t_later_numbers' in skip and k > 0) or ('t_earlier_numbers' in skip and k < count - 1):
            key.append(part)
        elif 't_long_numbers' in skip:
            key.append(part.zfill(3))
        else:
            key.append(part.zfill(40))
    return key


def _solve(items, skip=frozenset()):
    pinned = [item['title'] for item in items if item.get('pinned')]
    others = [item['title'] for item in items if not item.get('pinned')]
    return sorted(pinned, key=lambda t: _key(t, skip)) + sorted(others, key=lambda t: _key(t, skip))


def sidebar_order(items):
    return _solve(items)
'''

_T_TRAP = r'''
import re


def sidebar_order(items):
    def by_title(item):
<<key>>

    pinned = [item for item in items if item.get("pinned")]
    others = [item for item in items if not item.get("pinned")]
    ordered = sorted(pinned, key=by_title) + sorted(others, key=by_title)
    return [item["title"] for item in ordered]
'''
_T_NATURAL = ('        parts = re.split(r"(\\d+)", item["title"].lower())\n'
              '        return [int(p) if p.isdigit() else p for p in parts]')
T_TRAPS = {
    # 只把最後一段數字當數值。
    'last_number': _fill(_T_TRAP, key='        title = item["title"].lower()\n'
                                      '        numbers = list(re.finditer(r"\\d+", title))\n'
                                      '        if not numbers:\n'
                                      '            return (title, -1, "")\n'
                                      '        last = numbers[-1]\n'
                                      '        return (title[:last.start()], int(last.group()), title[last.end():])'),
    # 只把第一段數字當數值。
    'first_number': _fill(_T_TRAP, key='        title = item["title"].lower()\n'
                                       '        match = re.search(r"\\d+", title)\n'
                                       '        if not match:\n'
                                       '            return (title, -1, "")\n'
                                       '        return (title[:match.start()], int(match.group()), title[match.end():])'),
    # 數字補零到 3 位再比字串。
    'pad3': _fill(_T_TRAP, key='        return re.sub(r"\\d+", lambda m: m.group().zfill(3), item["title"].lower())'),
    # 自然排序寫對了，卻拿掉了不分大小寫。
    'drops_lower': _fill(_T_TRAP, key='        parts = re.split(r"(\\d+)", item["title"])\n'
                                      '        return [int(p) if p.isdigit() else p for p in parts]'),
    # 自然排序寫對了，卻把釘選分組弄丟。
    'drops_pinned': r'''
import re


def sidebar_order(items):
    def by_title(item):
''' + _T_NATURAL + r'''

    return [item["title"] for item in sorted(items, key=by_title)]
''',
}

T_PLAIN_TITLES = ('Bonus', 'credits', 'Extras', 'about this course', 'Trailer', 'notes')


class SidebarOrder(TicketFamily):
    letter, name, kind, entry = 'T', 'sidebar_order', 'bugfix', 'sidebar_order'
    reference_source, trap_sources = T_REFERENCE, T_TRAPS
    originals = {'sidebar': T_ORIGINAL}
    trap_breaks = {'last_number': {'t_earlier_numbers'}, 'first_number': {'t_later_numbers'},
                   'pad3': {'t_long_numbers'}, 'drops_lower': {REGRESSION}, 'drops_pinned': {REGRESSION}}
    _why = '回報要的是「標題裡的數字依數值排序，像人數數那樣」；同一個根本原因（數字被當成文字比）會出現在標題的任何位置。'
    requirements = {
        't_later_numbers': ('第一段之後的數字也要依數值比較（"Season 1 Episode 10" 排在 "Season 1 Episode 9" 之後）。',
                            _why, 'convention'),
        't_earlier_numbers': ('最後一段之前的數字也要依數值比較（"Season 10 Episode 1" 排在 "Season 9 Episode 2" 之後）。',
                              _why, 'convention'),
        't_long_numbers': ('數字位數不限（"IMG_10000.jpg" 排在 "IMG_9999.jpg" 之後），不能只補零到固定位數。',
                           '依數值比較不受位數影響；檔名、編號常有四、五位數。', 'convention'),
        REGRESSION: ('釘選項目仍排在前面；字母仍不分大小寫排序（"apple" 排在 "Banana" 前）；"pinned" 欄位可能不存在。',
                     '既有程式與說明文字明訂的行為，修數字排序不應改變它。', 'existing_code'),
    }
    _rule = ' Numbers in titles should be ordered by their value, the way people count.'
    reports = {
        1: 'Episodes are in the wrong order in the sidebar: Episode 10 sits between Episode 1 and Episode 2. `{call}` '
           '{got}; expected {expected}.' + _rule,
        2: 'After a photo import, IMG_100.jpg is listed before IMG_20.jpg. `{call}` {got}; expected {expected}.'
           + _rule,
        3: 'My pinned chapters are out of order: Chapter 10 comes before Chapter 9. `{call}` {got}; expected '
           '{expected}.' + _rule,
        4: 'In the music library, Track 10 and Track 11 are listed before Track 9. `{call}` {got}; expected '
           '{expected}.' + _rule,
        5: 'Course lessons show up as Lesson 12, Lesson 3, Lesson 7. `{call}` {got}; expected {expected}.' + _rule,
    }
    levels = {
        1: {'variant': 'sidebar', 'reqs': ('t_later_numbers', 't_earlier_numbers'),
            'traps': ('last_number', 'first_number'),
            'repro': [[{'title': 'Episode 2'}, {'title': 'Episode 10'}, {'title': 'Episode 1'}]]},
        2: {'variant': 'sidebar', 'reqs': ('t_long_numbers', 't_later_numbers'), 'traps': ('pad3', 'first_number'),
            'repro': [[{'title': 'IMG_20.jpg'}, {'title': 'IMG_100.jpg'}, {'title': 'IMG_3.jpg'}]]},
        3: {'variant': 'sidebar', 'reqs': ('t_earlier_numbers', 't_long_numbers'),
            'traps': ('last_number', 'pad3', 'drops_lower'),
            'repro': [[{'title': 'Chapter 10', 'pinned': True}, {'title': 'Notes'},
                       {'title': 'Chapter 9', 'pinned': True}]]},
        4: {'variant': 'sidebar', 'reqs': ('t_later_numbers', 't_earlier_numbers', 't_long_numbers'),
            'traps': ('first_number', 'last_number', 'drops_pinned'),
            'repro': [[{'title': 'Track 10', 'pinned': False}, {'title': 'Track 9', 'pinned': False},
                       {'title': 'Track 11', 'pinned': False}]]},
        5: {'variant': 'sidebar', 'reqs': ('t_later_numbers', 't_earlier_numbers', 't_long_numbers'),
            'traps': ('pad3', 'last_number', 'first_number', 'drops_lower'),
            'repro': [[{'title': 'Lesson 12'}, {'title': 'Lesson 3'}, {'title': 'Lesson 7', 'pinned': False}]]},
    }

    @staticmethod
    def _number(rng, scale):
        if scale == 'small':
            return rng.choice((1, 2, 3, 8, 9, 10, 11, 12, 20, 21))
        if scale == 'mid':
            return rng.randint(1, 999)
        return rng.choice((rng.randint(1, 999), rng.randint(1000, 99999)))

    def _title(self, rng, template, words, scale):
        word, outer, inner = words
        a, b = self._number(rng, scale), self._number(rng, 'small')
        title = {0: f'{word} {a}', 1: f'{outer} {b} {inner} {a}', 2: f'{outer} {a} {inner} {b}',
                 3: f'IMG_{a}.jpg', 4: f'{word} {a} - {rng.choice(("Intro", "Recap", "Finale"))}'}[template]
        roll = rng.random()
        return title.lower() if roll < .15 else title.upper() if roll < .2 else title

    @staticmethod
    def _item(rng, title):
        roll = rng.random()
        if roll < .25:
            return {'title': title, 'pinned': True}
        return {'title': title, 'pinned': False} if roll < .6 else {'title': title}

    def _items(self, rng, titles):
        keys, lowered, items = set(), set(), []
        for title in titles:
            key = tuple(_namespace(T_REFERENCE)['_key'](title, frozenset()))
            if key not in keys and title.lower() not in lowered:
                keys.add(key)
                lowered.add(title.lower())
                items.append(self._item(rng, title))
        return [items]

    def public_case(self, rng, level, index):
        word = rng.choice(('Episode', 'Chapter', 'Track', 'Part', 'Lesson'))
        digits = rng.choice((1, 2))
        titles = [f'{word} {rng.randint(10 ** (digits - 1), 10 ** digits - 1)}' for _ in range(rng.randint(3, 5))]
        if rng.random() < .5:
            titles.append(rng.choice(T_PLAIN_TITLES))
        return self._items(rng, titles)

    def random_case(self, rng, level):
        words = (rng.choice(('Episode', 'Chapter', 'Track', 'Part', 'Lesson', 'Invoice', 'Scan')),
                 rng.choice(('Season', 'Volume', 'Book')), rng.choice(('Episode', 'Part', 'Chapter')))
        templates = rng.sample(range(5), rng.choice((1, 1, 2)))
        scale = rng.choice(('small', 'mid', 'long'))
        titles = [self._title(rng, rng.choice(templates), words, scale) for _ in range(rng.randint(3, 8))]
        if rng.random() < .4:
            titles.append(rng.choice(T_PLAIN_TITLES))
        return self._items(rng, titles)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 3


# ---- U：自動更新挑最新版本（新功能）----

U_REFERENCE = r'''
def _key(version, skip):
    core, _, pre = version.partition('-')
    parts = core.split('.')
    numbers = tuple(parts) if 'u_numeric' in skip else tuple(int(p) for p in parts)
    if not pre:
        return (numbers, 0 if 'u_pre_vs_release' in skip else 2, ())
    if 'u_pre_ignored' in skip:
        return (numbers, 1, ())
    label, _, number = pre.partition('.')
    return (numbers, 1, (label, number if 'u_pre_order' in skip else int(number)))


def _solve(versions, skip=frozenset()):
    if not versions:
        if 'u_empty' in skip:
            raise ValueError('max() arg is an empty sequence')
        return None
    return max(versions, key=lambda v: _key(v, skip))


def latest_release(versions):
    return _solve(versions)
'''

U_TRAPS = {
    # 直接比字串。
    'string_max': r'''
def latest_release(versions):
    if not versions:
        return None
    return max(versions)
''',
    # 拆成字串清單再比。
    'split_strings': r'''
def latest_release(versions):
    if not versions:
        return None
    return max(versions, key=lambda v: v.split("."))
''',
    # 版號比數值，但預先發行標記當字串接在後面（正式版的空字串最小）。
    'pre_after_release': r'''
def latest_release(versions):
    if not versions:
        return None

    def key(v):
        core, _, pre = v.partition("-")
        return (tuple(int(x) for x in core.split(".")), pre)

    return max(versions, key=key)
''',
    # 正式版排在預先發行版後面，但預先發行標記整串當文字比（beta.10 < beta.2）。
    'pre_text': r'''
def latest_release(versions):
    if not versions:
        return None

    def key(v):
        core, _, pre = v.partition("-")
        return (tuple(int(x) for x in core.split(".")), pre == "", pre)

    return max(versions, key=key)
''',
    # 忽略預先發行標記，同版號時取先出現的。
    'ignore_pre': r'''
def latest_release(versions):
    if not versions:
        return None
    return max(versions, key=lambda v: tuple(int(x) for x in v.split("-")[0].split(".")))
''',
}


class LatestRelease(TicketFamily):
    letter, name, kind, entry = 'U', 'latest_release', 'feature', 'latest_release'
    reference_source, trap_sources = U_REFERENCE, U_TRAPS
    tickets = {'updater': (
        'Updater should offer the newest release',
        "The desktop app's auto-updater looks at the list of published versions and offers the newest one. "
        'Beta-channel users also get pre-releases, so the list can contain versions such as `3.0.0-beta.2` next to '
        '`2.9.1`. All versions follow Semantic Versioning (MAJOR.MINOR.PATCH with an optional pre-release tag). Please '
        'add `latest_release(versions)` that returns the newest version string in the list, or None when the list is '
        'empty.')}
    signatures = {'updater': 'latest_release(versions)'}
    probe_skips = {'u_pre_order': ('u_pre_order', 'u_pre_ignored')}
    trap_breaks = {
        'string_max': {'u_numeric', 'u_pre_vs_release', 'u_pre_order'},
        'split_strings': {'u_numeric', 'u_pre_vs_release', 'u_pre_order'},
        'pre_after_release': {'u_pre_vs_release', 'u_pre_order'},
        'pre_text': {'u_pre_order'},
        'ignore_pre': {'u_pre_vs_release', 'u_pre_order'},
    }
    requirements = {
        'u_numeric': ('版本號每一段依數值比較，不是字串（1.10.0 比 1.9.3 新）。',
                      '工單寫明遵循語意化版本（SemVer），SemVer 規定各段以數值比較。', 'convention'),
        'u_pre_vs_release': ('預先發行版比同版號的正式版舊（3.0.0-rc.1 早於 3.0.0）。',
                             'SemVer 第 11 條：有預先發行標記的版本優先序較低；正式版本來就是預先發行版的下一步。',
                             'convention'),
        'u_pre_order': ('同版號的多個預先發行版之間也要分新舊：標籤依字母順序（alpha < beta < rc），'
                        '數字依數值（3.0.0-beta.10 比 3.0.0-beta.2 新）。',
                        'SemVer 第 11 條：預先發行標記逐段比較，純數字的段以數值比較。', 'convention'),
        'u_empty': ('清單是空的時回傳 None。', '工單寫明；只有一種空輸入，所以當成寫明的情況，不計入覆蓋。', 'ticket'),
    }
    levels = {
        1: {'variant': 'updater', 'reqs': ('u_numeric', 'u_pre_vs_release'), 'explicit': ('u_empty',),
            'traps': ('string_max', 'pre_after_release')},
        2: {'variant': 'updater', 'reqs': ('u_numeric', 'u_pre_order'), 'explicit': ('u_empty',),
            'traps': ('split_strings', 'pre_text')},
        3: {'variant': 'updater', 'reqs': ('u_pre_vs_release', 'u_pre_order'), 'explicit': ('u_empty',),
            'traps': ('pre_after_release', 'ignore_pre')},
        4: {'variant': 'updater', 'reqs': ('u_numeric', 'u_pre_vs_release', 'u_pre_order'), 'explicit': ('u_empty',),
            'traps': ('string_max', 'pre_text', 'ignore_pre')},
        5: {'variant': 'updater', 'reqs': ('u_numeric', 'u_pre_vs_release', 'u_pre_order'), 'explicit': ('u_empty',),
            'traps': ('split_strings', 'pre_after_release', 'pre_text', 'ignore_pre')},
    }

    @staticmethod
    def _text(core, pre=None):
        return '.'.join(map(str, core)) + (f'-{pre[0]}.{pre[1]}' if pre else '')

    def public_case(self, rng, level, index):
        major, minor, patch = rng.randint(1, 4), rng.randint(0, 3), rng.randint(0, 3)
        cores = []
        for _ in range(rng.randint(3, 5)):
            cores.append((major, minor, patch))
            if rng.random() < .6:
                patch += rng.randint(1, 2)
            else:
                minor, patch = minor + 1, 0
        versions = [self._text(c) for c in cores]
        if rng.random() < .6:
            versions.append(self._text(cores[rng.randint(0, len(cores) - 2)], (rng.choice(('beta', 'rc')),
                                                                                rng.randint(1, 4))))
        rng.shuffle(versions)
        return [versions]

    def random_case(self, rng, level):
        if rng.random() < .06:
            return [[]]
        hi = rng.choice((9, 12, 20))
        base = [rng.randint(0, hi) for _ in range(3)]
        cores = {tuple(base)}
        for _ in range(rng.randint(1, 5)):
            core = list(base)
            slot = rng.randint(0, 2)
            core[slot] = rng.choice((core[slot] + 1, core[slot] + rng.randint(1, 12), max(0, core[slot] - 1),
                                     rng.choice((9, 10, 11, 2, 1))))
            for later in range(slot + 1, 3):
                core[later] = rng.choice((0, rng.randint(0, hi)))
            cores.add(tuple(core))
        versions = {self._text(c) for c in cores}
        for _ in range(rng.choice((0, 1, 2, 3))):
            core = rng.choice(sorted(cores) + [max(cores)] * 2)
            if rng.random() < .3:
                core = (core[0], core[1], core[2] + 1)
            versions.add(self._text(core, (rng.choice(('alpha', 'beta', 'rc')),
                                           rng.choice((1, 2, 3, 9, 10, 11, 12)))))
        if rng.random() < .35:
            top = max(cores)
            label = rng.choice(('alpha', 'beta', 'rc'))
            for number in rng.sample((1, 2, 3, 9, 10, 11, 12), rng.randint(2, 3)):
                versions.add(self._text((top[0], top[1], top[2] + 1), (label, number)))
        versions = sorted(versions)
        if rng.random() < .3:
            versions = [v for v in versions if '-' not in v or rng.random() < .5] or versions
        rng.shuffle(versions)
        return [versions]

    def plain_enough(self, level, args, expected):
        return len(args[0]) >= 3


# ---- V：CSV 匯出沒加引號（修 bug）----

V_ORIGINAL_LINE = r'''COLUMNS = ("order_id", "customer", "city", "total", "note")


def _cell(value):
    """Text for one cell: None is an empty cell, numbers are written with str()."""
    if value is None:
        return ""
    return str(value)


def to_csv_line(fields):
    """Render one row of the "Download CSV" order export (one line, no line ending)."""
    return ",".join(_cell(value) for value in fields)


def export_orders(orders):
    """The whole export: a header row, then one row per order dict, one per line."""
    lines = [to_csv_line(COLUMNS)]
    for order in orders:
        lines.append(to_csv_line([order.get(column) for column in COLUMNS]))
    return "\n".join(lines)
'''

V_ORIGINAL_DELIMITER = r'''COLUMNS = ("order_id", "customer", "city", "total", "note")


def _cell(value):
    """Text for one cell: None is an empty cell, numbers are written with str()."""
    if value is None:
        return ""
    return str(value)


def to_csv_line(fields, delimiter=","):
    """Render one row of the "Download CSV" order export (one line, no line ending).

    `delimiter` is "," by default; customers whose spreadsheet expects ";" or "|"
    choose another one in their export settings.
    """
    return delimiter.join(_cell(value) for value in fields)


def export_orders(orders, delimiter=","):
    """The whole export: a header row, then one row per order dict, one per line."""
    lines = [to_csv_line(COLUMNS, delimiter)]
    for order in orders:
        lines.append(to_csv_line([order.get(column) for column in COLUMNS], delimiter))
    return "\n".join(lines)
'''

V_REFERENCE = r'''
def _solve(fields, delimiter=',', skip=frozenset()):
    cells = []
    for value in fields:
        text = '' if value is None else str(value)
        special = False
        if delimiter in text:
            special = ('v_comma' if delimiter == ',' else 'v_delimiter') not in skip
        if '"' in text and 'v_quote' not in skip:
            special = True
        if '\n' in text and 'v_newline' not in skip:
            special = True
        if special:
            text = '"' + (text if 'v_quote' in skip else text.replace('"', '""')) + '"'
        cells.append(text)
    return delimiter.join(cells)


def to_csv_line(fields, delimiter=','):
    return _solve(fields, delimiter)
'''

_V_TRAP = r'''
def to_csv_line(fields, delimiter=","):
    cells = []
    for value in fields:
        text = <<text>>
        if <<when>>:
            text = '"' + <<inner>> + '"'
        cells.append(text)
    return delimiter.join(cells)
'''


def _v_trap(when, inner='text.replace(\'"\', \'""\')', text='"" if value is None else str(value)'):
    return _fill(_V_TRAP, when=when, inner=inner, text=text)


_V_ALL = 'delimiter in text or \'"\' in text or "\\n" in text'
V_TRAPS = {
    'comma_only': _v_trap('"," in text', inner='text'),                       # 只處理回報的逗號（寫死），也沒處理引號
    'backslash_escape': _v_trap(_V_ALL, inner='text.replace(\'"\', \'\\\\"\')'),  # 引號用反斜線跳脫
    'none_str': _v_trap(_V_ALL, text='str(value)'),                            # 引號寫對了，None 變成 "None"
    'quote_only': _v_trap('\'"\' in text'),                                   # 只處理回報的雙引號
    'no_newline': _v_trap('delimiter in text or \'"\' in text'),              # 沒處理換行
    'newline_only': _v_trap('"\\n" in text', inner='text'),                   # 只處理回報的換行
    'no_double': _v_trap(_V_ALL, inner='text'),                               # 加了引號但裡面的雙引號沒寫兩次
    'hardcoded_comma': _v_trap('"," in text or \'"\' in text or "\\n" in text'),  # 寫死逗號，沒看分隔符號
    'delim_only': _v_trap('delimiter in text', inner='text'),                 # 只處理分隔符號
}

V_WORDS = ('Boston', 'In stock', 'Acme Corp', 'blue', 'Order 1042', 'paid', 'Lisbon', 'Widget XL', 'A-17')


class CsvLine(TicketFamily):
    letter, name, kind, entry = 'V', 'csv_line', 'bugfix', 'to_csv_line'
    reference_source, trap_sources = V_REFERENCE, V_TRAPS
    originals = {'line': V_ORIGINAL_LINE, 'delimiter': V_ORIGINAL_DELIMITER}
    trap_breaks = {
        'comma_only': {'v_quote', 'v_newline', 'v_delimiter'}, 'backslash_escape': {'v_quote'}, 'none_str': {REGRESSION},
        'quote_only': {'v_comma', 'v_newline', 'v_delimiter'}, 'no_newline': {'v_newline'},
        'newline_only': {'v_comma', 'v_quote', 'v_delimiter'}, 'no_double': {'v_quote'},
        'hardcoded_comma': {'v_delimiter'}, 'delim_only': {'v_quote', 'v_newline'},
    }
    _why = '回報的根本原因是沒有照 CSV 標準（RFC 4180，Excel 也照這個讀）加引號；修好要整套照標準，不只回報那一種字元。'
    requirements = {
        'v_comma': ('格子裡有逗號（分隔符號）時，整格用雙引號包起來。', _why, 'convention'),
        'v_quote': ('格子裡有雙引號時，整格用雙引號包起來，裡面的雙引號寫成兩個。', _why, 'convention'),
        'v_newline': ('格子裡有換行時，整格用雙引號包起來。', _why, 'convention'),
        'v_delimiter': ('改用分號或直線等其他分隔符號時，含該分隔符號的格子也要加引號。',
                        '回報的根本原因是「含分隔符號的格子沒加引號」；分隔符號換了，規則一樣。', 'convention'),
        REGRESSION: ('None 仍輸出空白格、數字仍用 str()、一般格子不加引號、用指定的分隔符號串接。',
                     '既有程式與說明文字明訂的行為；修引號不應改變它。', 'existing_code'),
    }
    reports = {
        1: 'Customer names that contain a comma shift every column after them when the export is opened in Excel. '
           '`{call}` {got}, which Excel reads as four columns. Expected {expected}: wrapping the cell in double quotes '
           'is how CSV keeps a comma inside one cell.',
        2: 'Product names with an inch mark break the export. `{call}` {got}, and Excel shows the wrong text in that '
           'cell. Expected {expected}: a cell that contains a double quote has to be wrapped in double quotes, with '
           'the quote itself written twice.',
        3: 'Delivery notes that contain a line break split the row in two when the export is opened in Excel. '
           '`{call}` {got}. Expected {expected}: a cell with a line break has to be wrapped in double quotes so it '
           'stays one cell.',
        4: 'Customer names that contain a comma shift every column after them when the export is opened in Excel. '
           '`{call}` {got}, which Excel reads as four columns. Expected {expected}: wrapping the cell in double quotes '
           'is how CSV keeps a comma inside one cell.',
        5: 'Our Italian customers export with a semicolon delimiter, and names that contain a semicolon shift the '
           'columns. `{call}` {got}. Expected {expected}: the cell has to be wrapped in double quotes.',
    }
    levels = {
        1: {'variant': 'line', 'reqs': ('v_quote', 'v_newline'), 'explicit': ('v_comma',),
            'traps': ('comma_only', 'backslash_escape', 'none_str'), 'repro': [['Smith, Jane', 'Boston', 3]]},
        2: {'variant': 'line', 'reqs': ('v_comma', 'v_newline'), 'explicit': ('v_quote',),
            'traps': ('quote_only', 'no_newline'), 'repro': [['15" Monitor', 'In stock', 249]]},
        3: {'variant': 'line', 'reqs': ('v_comma', 'v_quote'), 'explicit': ('v_newline',),
            'traps': ('newline_only', 'no_double', 'none_str'),
            'repro': [['A-1042', 'Left at the door.\nNobody home.', 'closed']]},
        4: {'variant': 'delimiter', 'reqs': ('v_quote', 'v_newline', 'v_delimiter'), 'explicit': ('v_comma',),
            'traps': ('comma_only', 'hardcoded_comma'), 'repro': [['Smith, Jane', 'Boston', 3]]},
        5: {'variant': 'delimiter', 'reqs': ('v_comma', 'v_quote', 'v_newline'), 'explicit': ('v_delimiter',),
            'traps': ('delim_only', 'no_newline', 'backslash_escape'), 'repro': [['Rossi; Mario', 'Milano', 12], ';']},
    }

    @staticmethod
    def _special(rng, delimiter):
        kind = rng.choice(('delimiter', 'delimiter', 'quote', 'quote', 'newline', 'both'))
        given, family = rng.choice((('Jane', 'Smith'), ('Mario', 'Rossi'), ('Ann', 'Lee'), ('Tom', 'Berg')))
        if kind == 'delimiter':
            return rng.choice((f'{family}{delimiter} {given}', f'Paid{delimiter} not shipped',
                               f'Red{delimiter} blue{delimiter} green'))
        if kind == 'quote':
            return rng.choice(('15" Monitor', 'The "Pro" plan', f'{given} "{family}" Jr', '"Quoted"'))
        if kind == 'newline':
            return rng.choice(('Left at the door.\nNobody home.', f'{given}\n{family}', 'Line one\nLine two\nLine 3'))
        return rng.choice((f'{family}{delimiter} "{given}"', f'Note: "fragile"\nHandle{delimiter} with care',
                           f'Size 27"{delimiter} matte'))

    def _fields(self, rng, delimiter, special_rate, allow_none):
        fields = []
        for _ in range(rng.randint(2, 5)):
            roll = rng.random()
            if allow_none and roll < .12:
                fields.append(None)
            elif roll < .3:
                fields.append(rng.choice((rng.randint(-50, 5000), rng.choice((2.5, 19.99, 0.75, 1000.0)))))
            elif rng.random() < special_rate:
                fields.append(self._special(rng, delimiter))
            else:
                fields.append(rng.choice(V_WORDS))
        return fields

    def _delimiter(self, rng, level):
        return ',' if self.levels[level]['variant'] == 'line' else rng.choice((',', ',', ';', '|'))

    def _args(self, rng, level, fields, delimiter):
        if self.levels[level]['variant'] == 'line' or (delimiter == ',' and rng.random() < .6):
            return [fields]
        return [fields, delimiter]

    def public_case(self, rng, level, index):
        delimiter = self._delimiter(rng, level)
        return self._args(rng, level, self._fields(rng, delimiter, 0, rng.random() < .5), delimiter)

    def random_case(self, rng, level):
        delimiter = self._delimiter(rng, level)
        return self._args(rng, level, self._fields(rng, delimiter, .45, True), delimiter)


# ---- W：年齡檢查（新功能）----

W_REFERENCE = r'''
from datetime import date


def _solve(birth_date, on_date, skip=frozenset()):
    born, on = date.fromisoformat(birth_date), date.fromisoformat(on_date)
    if 'w_days_365' in skip:
        return (on - born).days // 365
    if 'w_days_36525' in skip:
        return int((on - born).days // 365.25)
    years = on.year - born.year
    if 'w_before_birthday' in skip:
        return years
    if 'w_same_month' in skip:
        before = on.month < born.month
    elif 'w_on_birthday' in skip:
        before = (on.month, on.day) <= (born.month, born.day)
    else:
        before = (on.month, on.day) < (born.month, born.day)
    return years - before


def age_on(birth_date, on_date):
    return _solve(birth_date, on_date)
'''

W_TRAPS = {
    # 只算年份差。
    'year_diff': r'''
def age_on(birth_date, on_date):
    return int(on_date[:4]) - int(birth_date[:4])
''',
    # 只比月份，沒比日期。
    'month_only': r'''
from datetime import date


def age_on(birth_date, on_date):
    born, on = date.fromisoformat(birth_date), date.fromisoformat(on_date)
    age = on.year - born.year
    if on.month < born.month:
        age -= 1
    return age
''',
    # 生日當天還不算滿歲。
    'off_by_one': r'''
from datetime import date


def age_on(birth_date, on_date):
    born, on = date.fromisoformat(birth_date), date.fromisoformat(on_date)
    age = on.year - born.year
    if (on.month, on.day) <= (born.month, born.day):
        age -= 1
    return age
''',
    # 天數除以 365。
    'days_365': r'''
from datetime import date


def age_on(birth_date, on_date):
    return (date.fromisoformat(on_date) - date.fromisoformat(birth_date)).days // 365
''',
    # 天數除以 365.25。
    'days_36525': r'''
from datetime import date


def age_on(birth_date, on_date):
    days = (date.fromisoformat(on_date) - date.fromisoformat(birth_date)).days
    return int(days / 365.25)
''',
}


class AgeOn(TicketFamily):
    letter, name, kind, entry = 'W', 'age_on', 'feature', 'age_on'
    reference_source, trap_sources = W_REFERENCE, W_TRAPS
    tickets = {'checkout': (
        'Age check for age-restricted products',
        'Checkout must block age-restricted products (alcohol, tobacco) for customers who are too young. Please add '
        "`age_on(birth_date, on_date)` that returns the customer's age in whole years on `on_date`. Both dates are ISO "
        "strings like `'1990-07-15'`, and `on_date` is never earlier than `birth_date`. Customers born on February 29 "
        'are verified manually, so they never reach this function.')}
    signatures = {'checkout': 'age_on(birth_date, on_date)'}
    probe_skips = {'w_day_count': ('w_days_365', 'w_days_36525')}
    trap_breaks = {'year_diff': {'w_before_birthday'}, 'month_only': {'w_same_month'},
                   'off_by_one': {'w_on_birthday'}, 'days_365': {'w_day_count'}, 'days_36525': {'w_day_count'}}
    requirements = {
        'w_before_birthday': ('今年生日還沒到時，歲數是年份差減一。',
                              '「幾歲」的一般定義是過了幾次生日；只算年份差會讓還沒滿齡的人通過檢查。', 'convention'),
        'w_same_month': ('生日和 on_date 同月時要再比日期（3 月 5 日當天，3 月 20 日生的人還沒過生日）。',
                         '生日是「月＋日」，不是只看月份。', 'convention'),
        'w_on_birthday': ('生日當天就算滿新的歲數。', '一般認知與各國法定年齡都是生日當天滿歲。', 'convention'),
        'w_day_count': ('不可用 365 或 365.25 天近似一年；生日前後幾天，閏年會讓近似算錯。',
                        '工單要的是整數歲數，差一天就可能讓未滿齡的人結帳；每年天數本來就不同。', 'convention'),
    }
    levels = {
        1: {'variant': 'checkout', 'reqs': ('w_before_birthday', 'w_on_birthday'), 'traps': ('year_diff',
                                                                                             'off_by_one')},
        2: {'variant': 'checkout', 'reqs': ('w_before_birthday', 'w_same_month'), 'traps': ('year_diff',
                                                                                            'month_only')},
        3: {'variant': 'checkout', 'reqs': ('w_before_birthday', 'w_on_birthday', 'w_day_count'),
            'traps': ('days_365', 'days_36525', 'off_by_one')},
        4: {'variant': 'checkout', 'reqs': ('w_before_birthday', 'w_same_month', 'w_on_birthday'),
            'traps': ('month_only', 'off_by_one', 'year_diff')},
        5: {'variant': 'checkout', 'reqs': ('w_before_birthday', 'w_same_month', 'w_on_birthday', 'w_day_count'),
            'traps': ('days_365', 'days_36525', 'month_only', 'off_by_one')},
    }

    @staticmethod
    def _birth(rng):
        while True:
            year, month = rng.randint(1940, 2015), rng.randint(1, 12)
            day = rng.randint(1, calendar.monthrange(year, month)[1])
            if (month, day) != (2, 29):
                return date(year, month, day)

    def public_case(self, rng, level, index):
        born = self._birth(rng)
        on = date(rng.randint(born.year + 1, 2030), 1, 1) + timedelta(days=rng.randint(0, 364))
        return [born.isoformat(), on.isoformat()]

    def random_case(self, rng, level):
        born = self._birth(rng)
        year = rng.randint(born.year, min(born.year + 90, 2030))
        roll = rng.random()
        if roll < .4:
            on = date(year, born.month, born.day) + timedelta(days=rng.randint(-6, 6))
        elif roll < .6:
            on = date(year, born.month, rng.randint(1, calendar.monthrange(year, born.month)[1]))
        else:
            on = date(year, 1, 1) + timedelta(days=rng.randint(0, 364))
        if on < born:
            on = born + timedelta(days=rng.choice((0, rng.randint(1, 400))))
        return [born.isoformat(), on.isoformat()]


# ---- X：文字雲同一個字算成好幾個（修 bug）----

X_ORIGINAL_PLAIN = r'''def top_words(text, n):
    """Most frequent words for the review word cloud.

    Returns up to n [word, count] pairs, most frequent first. Ties are broken
    alphabetically so the cloud does not reshuffle between page loads.
    """
    counts = {}
    for word in text.split(" "):
        if not word:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [[word, count] for word, count in ranked[:n]]


def cloud_font_sizes(text, n, smallest=12, largest=48):
    """Font size (px) for each word in the cloud, scaled by its count."""
    ranked = top_words(text, n)
    if not ranked:
        return {}
    top = ranked[0][1]
    return {word: smallest + (largest - smallest) * count // top for word, count in ranked}
'''

X_ORIGINAL_STOP = r'''def top_words(text, n, stopwords):
    """Most frequent words for the review word cloud.

    `stopwords` is a list of lowercase words to leave out, such as "the" or "and".
    Returns up to n [word, count] pairs, most frequent first. Ties are broken
    alphabetically so the cloud does not reshuffle between page loads.
    """
    skip = set(stopwords)
    counts = {}
    for word in text.split(" "):
        if not word or word in skip:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [[word, count] for word, count in ranked[:n]]


def cloud_font_sizes(text, n, stopwords, smallest=12, largest=48):
    """Font size (px) for each word in the cloud, scaled by its count."""
    ranked = top_words(text, n, stopwords)
    if not ranked:
        return {}
    top = ranked[0][1]
    return {word: smallest + (largest - smallest) * count // top for word, count in ranked}
'''

X_REFERENCE = r'''
import string


def _solve(text, n, stopwords=(), skip=frozenset()):
    stop = set(stopwords)
    pieces = text.split(' ') if 'x_whitespace' in skip else text.split()
    marks = ',!' if 'x_other_punct' in skip else string.punctuation
    counts = {}
    for piece in pieces:
        if not piece:
            continue
        if 'x_stopword_case' in skip and piece in stop:
            continue
        word = piece.strip(marks).lower()
        if not word and 'x_punct_only' not in skip:
            continue
        if 'x_stopword_case' not in skip and word in stop:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [[word, count] for word, count in ranked[:n]]


def top_words(text, n, stopwords=()):
    return _solve(text, n, stopwords)
'''

_X_TRAP = r'''
import re
import string
from collections import Counter


def top_words(text, n<<params>>):
<<setup>>    counts = {}
    for word in <<pieces>>:
<<before>>        word = word.strip(<<marks>>).lower()
<<empty>><<after>>        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [[word, count] for word, count in ranked[:n]]
'''
_X_EMPTY = '        if not word:\n            continue\n'
_X_STOP_AFTER = '        if word in skip:\n            continue\n'


def _x_trap(stop, pieces='text.split(" ")', marks='string.punctuation', empty=_X_EMPTY, before='', after=None):
    return _fill(_X_TRAP, params=', stopwords' if stop else '', setup='    skip = set(stopwords)\n' if stop else '',
                 pieces=pieces, marks=marks, empty=empty, before=before,
                 after=(_X_STOP_AFTER if stop else '') if after is None else after)


_X_MOST_COMMON = r'''
import re
from collections import Counter


def top_words(text, n<<params>>):
    words = re.findall(r"[a-z]+", text.lower())
<<filter>>    return [[word, count] for word, count in Counter(words).most_common(n)]
'''
X_TRAPS = {
    'strip_report': _x_trap(False, marks='",!"'),                          # 只去掉回報裡的逗號和驚嘆號
    'split_strip_all': _x_trap(False),                                     # 去掉所有標點，但只用空白斷字
    'most_common': _fill(_X_MOST_COMMON, params='', filter=''),            # 斷字對了，同次數順序被改掉
    'strip_report_stop': _x_trap(True, marks='",!"'),
    'split_strip_all_stop': _x_trap(True),
    'most_common_stop': _fill(_X_MOST_COMMON, params=', stopwords',
                              filter='    words = [w for w in words if w not in set(stopwords)]\n'),
    'stop_raw': _x_trap(True, pieces='text.split()', before='        if word in skip:\n            continue\n',
                        after=''),                                         # 先比排除字才正規化
    'keep_empty': _x_trap(False, pieces='text.split()', empty=''),         # 單獨的標點變成空字串
    'keep_empty_stop': _x_trap(True, pieces='text.split()', empty=''),
}

X_WORDS = ('dog', 'cat', 'great', 'fast', 'love', 'price', 'box', 'blue', 'fun', 'good', 'size', 'happy')
X_STOPWORDS = ('the', 'a', 'and', 'it', 'is', 'of')


class TopWords(TicketFamily):
    letter, name, kind, entry = 'X', 'top_words', 'bugfix', 'top_words'
    reference_source, trap_sources = X_REFERENCE, X_TRAPS
    originals = {'plain': X_ORIGINAL_PLAIN, 'stop': X_ORIGINAL_STOP}
    trap_breaks = {
        'strip_report': {'x_other_punct', 'x_whitespace', 'x_punct_only'}, 'split_strip_all': {'x_whitespace'},
        'most_common': {REGRESSION}, 'strip_report_stop': {'x_other_punct', 'x_whitespace', 'x_punct_only'},
        'split_strip_all_stop': {'x_whitespace'}, 'most_common_stop': {REGRESSION},
        'stop_raw': {'x_stopword_case'}, 'keep_empty': {'x_punct_only'}, 'keep_empty_stop': {'x_punct_only'},
    }
    trap_variants = {t: ('stop',) if t == 'stop_raw' or t.endswith('_stop') else ('plain',) for t in X_TRAPS}
    requirements = {
        'x_other_punct': ('回報以外的標點（句點、問號、分號、冒號、引號、括號）也不算字的一部分。',
                          '回報說「大小寫和標點不應讓同一個字變成好幾個」；問題出在斷字，不只回報裡出現的逗號和驚嘆號。',
                          'ticket'),
        'x_whitespace': ('換行和 Tab 也是字與字的分隔。',
                         '文字雲的輸入是評論全文，常有換行；換行隔開的兩個字不是一個字，任何斷字方式都該如此。',
                         'convention'),
        'x_punct_only': ('單獨的標點（例如 " - " 或 "..."）不算一個字，也不能變成空字串出現在結果裡。',
                         '標點本身不是字；結果裡出現空字串是明顯的錯。', 'convention'),
        'x_stopword_case': ('排除字（stopwords）要和正規化後的字比對，"The"、"the," 也要被排除。',
                            '修好之後 "The" 和 "the" 是同一個字；說明文字說 stopwords 是要排除的字，自然包含它的大寫與帶標點寫法。',
                            'existing_code'),
        REGRESSION: ('同次數依字母順序排列、最多回傳 n 個（n 比字數多就全部回傳）、回傳 [字, 次數]。',
                     '既有程式與說明文字明訂的行為（避免文字雲每次重排），修斷字不應改變它。', 'existing_code'),
    }
    _rule = ' Capital letters and punctuation should not turn one word into several.'
    reports = {
        1: 'The review word cloud shows the same word several times. `{call}` {got}; expected {expected}.' + _rule,
        2: '"Great" and "great!" are counted as two different words in the review cloud. `{call}` {got}; expected '
           '{expected}.' + _rule,
        3: 'The word cloud for a product page splits one word into three entries. `{call}` {got}; expected '
           '{expected}.' + _rule,
        4: 'The top words for a product are all the same word written differently. `{call}` {got}; expected '
           '{expected}.' + _rule,
        5: 'The word cloud shows "Blue", "blue," and "BLUE" as separate words. `{call}` {got}; expected {expected}.'
           + _rule,
    }
    levels = {
        1: {'variant': 'plain', 'reqs': ('x_other_punct', 'x_punct_only'),
            'traps': ('strip_report', 'keep_empty', 'most_common'),
            'repro': ['Dog dog, DOG! cat', 2]},
        2: {'variant': 'plain', 'reqs': ('x_whitespace', 'x_other_punct'),
            'traps': ('split_strip_all', 'strip_report'), 'repro': ['Great product! great, GREAT price', 3]},
        3: {'variant': 'stop', 'reqs': ('x_stopword_case', 'x_punct_only'), 'traps': ('stop_raw', 'keep_empty_stop'),
            'repro': ['Fast shipping! fast, FAST box', 2, ['a', 'the']]},
        4: {'variant': 'stop', 'reqs': ('x_other_punct', 'x_whitespace', 'x_stopword_case'),
            'traps': ('strip_report_stop', 'stop_raw', 'most_common_stop'),
            'repro': ['Love it love, LOVE the box!', 2, ['it', 'the']]},
        5: {'variant': 'stop', 'reqs': ('x_other_punct', 'x_whitespace', 'x_punct_only', 'x_stopword_case'),
            'traps': ('split_strip_all_stop', 'keep_empty_stop', 'stop_raw'),
            'repro': ['Blue box! blue, BLUE size', 2, ['a']]},
    }

    @staticmethod
    def _word(rng, word, punct_rate):
        roll = rng.random()
        word = word.upper() if roll < .15 else word.capitalize() if roll < .45 else word
        if rng.random() < punct_rate:
            mark = rng.choice((',', '!', '.', '?', ';', ':', '"', '(', ',', '!'))
            if mark == '"':
                word = f'"{word}"' if rng.random() < .5 else rng.choice((f'"{word}', f'{word}."'))
            elif mark == '(':
                word = f'({word})'
            else:
                word += mark
        return word

    def _case(self, rng, level, punct_rate, spaces_rate):
        stop = self.levels[level]['variant'] == 'stop'
        vocabulary = rng.sample(X_WORDS, rng.randint(2, 5))
        stopwords = sorted(rng.sample(X_STOPWORDS, rng.randint(1, 3))) if stop else []
        tokens = []
        for _ in range(rng.randint(4, 14)):
            roll = rng.random()
            if stop and roll < .25:
                tokens.append(self._word(rng, rng.choice(stopwords), punct_rate))
            elif roll < .3 and punct_rate:
                tokens.append(rng.choice(('-', '...', '--', '!')))
            else:
                tokens.append(self._word(rng, rng.choice(vocabulary), punct_rate))
        text = tokens[0]
        for token in tokens[1:]:
            text += (rng.choice(('\n', '\t', ' \n', '\n\n')) if rng.random() < spaces_rate else
                     rng.choice((' ', ' ', ' ', '  '))) + token
        args = [text, rng.choice((1, 2, 3, 4, 6, 10))]
        return args + [stopwords] if stop else args

    def public_case(self, rng, level, index):
        return self._case(rng, level, 0, 0)

    def random_case(self, rng, level):
        return self._case(rng, level, rng.choice((.2, .4, .6)), rng.choice((0, .1, .3)))

    def plain_enough(self, level, args, expected):
        return len(expected) >= 2


TICKET_FAMILIES = {f.letter: f for f in (FreeSlots(), AddPeriod(), ParseAmount(), SidebarOrder(), LatestRelease(),
                                         CsvLine(), AgeOn(), TopWords())}


# ---- 產生 ----

def _attributable(fam, trap, touched, original_ok):
    """錯誤解在這個測試失敗，能不能歸到它違反的某條要求（或改壞了既有程式本來就答對的輸入）。"""
    breaks = fam.trap_breaks[trap]
    return bool(touched & breaks) or (REGRESSION in breaks and original_ok)


def _outcome(function, args):
    try:
        value = _json_copy(function(*_json_copy(args)))
    except Exception as error:
        return f'raises {type(error).__name__}'
    return f'returns {value!r}'


def ticket_prompt(fam, level, public):
    config = fam.levels[level]
    examples = [f'- {_call(fam.entry, t["args"])} returns {t["expected"]!r}' for t in public]
    if fam.kind == 'feature':
        title, body = fam.tickets[config['variant']]
        lines = [FEATURE_INTRO, '', f'Ticket: {title}', '', body, '', 'Examples from the ticket:', *examples, '',
                 f'Implement `{fam.signatures[config["variant"]]}` in Python.']
    else:
        repro = public[0]
        report = fam.reports[level].format(call=_call(fam.entry, repro['args']),
                                           got=_outcome(fam.original(level), repro['args']),
                                           expected=repr(repro['expected']))
        lines = [BUG_INTRO, '', 'Bug report:', report, '', 'Current code:', '```python',
                 fam.originals[config['variant']].strip('\n'), '```', '', BUG_EXAMPLES, *examples]
    return '\n'.join(lines + ['', REPLY_RULES.format(entry=fam.entry)])


def build_ticket(family, level, seed):
    """第三版的一題（含 implicit_requirements）。同一行程內快取 JSON 文字，每次呼叫回傳新的複本。"""
    task, meta = _ticket_json(family, level, seed)
    return json.loads(task), json.loads(meta)


@lru_cache(maxsize=None)
def _ticket_json(family, level, seed):
    return tuple(json.dumps(part, ensure_ascii=False) for part in _build_ticket(family, level, seed))


def ticket_family(family):
    """第三版工單題或第四版程式庫情境題（code_repo_tasks 反過來引用本模組，所以延後匯入）。"""
    if family in TICKET_FAMILIES:
        return TICKET_FAMILIES[family]
    from .code_repo_tasks import REPO_FAMILIES
    return REPO_FAMILIES[family]


def _build_ticket(family, level, seed):
    fam = ticket_family(family)
    config = fam.levels[level]
    rng = _rng(family, level, seed)
    reqs, traps = set(config['reqs']), config['traps']
    allowed = reqs | set(config.get('explicit', ()))
    bug = fam.kind == 'bugfix'
    original = fam.original(level) if bug else None
    public, seen = [], set()
    if bug:
        args = config['repro']
        expected = fam.solve(args)
        if _same(original, args, expected) or not fam.touches(args, expected) <= set(config.get('explicit', ())) \
                or not all(_same(fam.trap(t), args, expected) for t in traps):
            raise RuntimeError(f'Bad bug report reproduction for {family}{level}')
        public.append({'args': args, 'expected': expected})
        seen.add(json.dumps(args, sort_keys=True))
    for _ in range(20000):
        if len(public) == PUBLIC_COUNT:
            break
        args = fam.public_case(rng, level, len(public))
        key = json.dumps(args, sort_keys=True)
        if key in seen:
            continue
        expected = fam.solve(args)
        if fam.touches(args, expected) or not fam.plain_enough(level, args, expected) \
                or (bug and not _same(original, args, expected)) \
                or not all(_same(fam.trap(t), args, expected) for t in traps):
            continue
        seen.add(key)
        public.append({'args': args, 'expected': expected})
    if len(public) < PUBLIC_COUNT:
        raise RuntimeError(f'Could not generate plain public examples for {family}{level}-{seed}')
    hidden, rows = [], []
    trap_counts, req_counts, plain, regression = Counter(), Counter(), 0, 0
    for _ in range(40000):
        covered = all(trap_counts[t] >= TICKET_COVERAGE for t in traps) \
            and all(req_counts[c] >= TICKET_COVERAGE for c in reqs) and (not bug or regression >= REGRESSION_MIN)
        if (len(hidden) >= TICKET_HIDDEN_MIN and covered) or len(hidden) >= TICKET_HIDDEN_MAX:
            break
        args = fam.random_case(rng, level)
        key = json.dumps(args, sort_keys=True)
        if key in seen:
            continue
        expected = fam.solve(args)
        touched = fam.touches(args, expected)
        if not touched <= allowed:
            continue
        original_ok = bug and _same(original, args, expected)
        fails = {t for t in traps if not _same(fam.trap(t), args, expected)}
        credited = {t for t in fails if _attributable(fam, t, touched, original_ok)}
        needed = any(trap_counts[t] < TICKET_COVERAGE for t in credited) \
            or any(req_counts[c] < TICKET_COVERAGE for c in touched & reqs) \
            or (original_ok and regression < REGRESSION_MIN)
        quiet = not fails and not touched & reqs
        # 以打得掉錯誤解或碰到隱含要求的測試為主，留約四分之一一般情況；接近下限時只收還沒覆蓋夠的。
        if not needed and quiet and plain >= max(8, len(hidden) // 4):
            continue
        if not needed and not covered and len(hidden) >= TICKET_HIDDEN_MIN - 15:
            continue
        seen.add(key)
        trap_counts.update(credited)
        req_counts.update(touched & reqs)
        plain += quiet
        regression += original_ok
        hidden.append({'args': args, 'expected': expected})
        rows.append({'touched': sorted(touched), 'fails': sorted(fails), 'original_ok': original_ok})
    missing = [t for t in traps if trap_counts[t] < TICKET_COVERAGE] \
        + [c for c in sorted(reqs) if req_counts[c] < TICKET_COVERAGE] \
        + (['regression'] if bug and regression < REGRESSION_MIN else [])
    if missing:
        raise RuntimeError(f'Hidden tests of {family}{level}-{seed} miss coverage: {missing}')
    task = {'id': f'{family}{level}-{seed}', 'family': family, 'level': level, 'seed': seed,
            'prompt': fam.task_prompt(level, public) if hasattr(fam, 'task_prompt') else ticket_prompt(fam, level, public),
            'entry_point': fam.entry,
            'public_tests': public, 'hidden_tests': hidden, 'kind': 'code',
            'implicit_requirements': fam.requirement_records(level)}
    if hasattr(fam, 'repo'):
        task['repo'] = fam.repo(level)
    return task, {'hidden': rows, 'trap_counts': dict(trap_counts), 'req_counts': dict(req_counts),
                  'regression': regression}
