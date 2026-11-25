"""第五版盲測程式題（ZI～ZP，慣例只寫在程式碼裡的程式庫情境題）：公平性、錯誤解、獨立核對、評分路徑與凍結。全部離線。

核對方式和第四版（tests/test_code_repo_tasks.py）相同：
- CHECKERS：每類不經程式庫、用另一種寫法重算全部期望值。
- ALTERNATIVES：寫法不同但合理的作答，必須過全部測試。
- 錯誤解：公開範例全對、隱藏測試至少錯 TICKET_COVERAGE 題，且每個失敗都能歸到它違反的要求。
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import json
import re

import pytest

from cloudyfan_jev import code_repo_tasks as rt
from cloudyfan_jev import code_repo_tasks_v5 as v5
from cloudyfan_jev import code_sandbox as sandbox
from cloudyfan_jev import code_tasks as ct
from cloudyfan_jev import code_tickets as tk

needs_sandbox = pytest.mark.skipif(not sandbox.sandbox_available(), reason='sandbox-exec is only available on macOS')
FAMILIES = v5.REPO_FAMILIES
KEYS = [(f, level, seed) for f in FAMILIES for level in rt.LEVELS for seed in (0, 1, 2)]   # 凍結檔的 48 題
SEED0 = [k for k in KEYS if k[2] == 0]


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


# ---- 獨立核對（不經程式庫）----

REVENUE = {'paid', 'fulfilled'}


def sku(raw):
    return re.sub(r'[\s_-]+', '-', raw.strip()).upper()


def biz_day(ts):
    return (datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ') + timedelta(hours=4)).date().isoformat()


def sold(order):
    return [l for l in order['lines'] if l['void'] is False]


def revenue(order):
    goods = sum(l['qty'] * l['unit_cents'] for l in sold(order) if not sku(l['sku']).startswith('GFT-'))
    return max(0, goods - order['discount_cents'])


def counted(order):
    return order['status'] in REVENUE and not order['customer_id'].startswith('qa-')


def a_customer(order):
    return order['customer_id'] != 'guest' and not order['customer_id'].startswith('staff-')


def owner(customers, cid):
    merged = {c['id']: c['merged_into'] for c in customers}
    while merged.get(cid):
        cid = merged[cid]
    return cid


def is_open(c):
    return c['deleted_at'] is None and c['merged_into'] is None


def zi(orders):
    totals, counts = Counter(), Counter()
    for o in filter(counted, orders):
        day = o['pickup_on'] or biz_day(o['placed_at'])
        totals[day] += revenue(o)
        counts[day] += 1
    return {d: int((Decimal(totals[d]) / counts[d]).quantize(Decimal(1), ROUND_HALF_UP)) for d in sorted(totals)}


def zj(orders):
    days = {}
    for o in orders:
        if counted(o) and a_customer(o):
            days.setdefault(biz_day(o['placed_at']), set()).add(o['customer_id'])
    return {d: len(v) for d, v in sorted(days.items())}


def zk(orders, customers):
    out = {c['id']: 0 for c in customers if is_open(c)}
    for o in filter(counted, orders):
        who = owner(customers, o['customer_id'])
        if who in out:
            out[who] += revenue(o)
    return out


def zl(customers):
    return sorted(c['id'] for c in customers if c['marketing_opt_in'] and c['email_verified_at'] and is_open(c))


def zm(orders):
    out = Counter()
    for o in orders:
        for l in sold(o):
            out[sku(l['sku'])] += l['qty']
    return dict(out)


def zn(orders):
    out = Counter()
    for o in filter(counted, orders):
        out[(o['pickup_on'] or biz_day(o['placed_at']))[:7]] += revenue(o)
    return dict(out)


def zo(orders, customers, n):
    closed = {c['id'] for c in customers if c['deleted_at']}
    spent = Counter()
    for o in orders:
        if counted(o) and a_customer(o) and o['customer_id'] not in closed:
            spent[owner(customers, o['customer_id'])] += revenue(o)
    return [[c, v] for c, v in sorted(spent.items(), key=lambda kv: (-kv[1], kv[0]))][:n]


def zp(orders, products):
    cat = {p['sku']: p['category'] for p in products}
    out = Counter()
    for o in filter(counted, orders):
        for l in sold(o):
            if not sku(l['sku']).startswith('GFT-'):
                out[cat[sku(l['sku'])]] += l['qty'] * l['unit_cents']
    return dict(out)


CHECKERS = {'ZI': zi, 'ZJ': zj, 'ZK': zk, 'ZL': zl, 'ZM': zm, 'ZN': zn, 'ZO': zo, 'ZP': zp}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_expected_values_match_an_independent_implementation(key):
    task, _ = built(*key)
    for test in task['public_tests'] + task['hidden_tests']:
        assert sandbox.strict_equal(CHECKERS[key[0]](*args_of(test)), test['expected']), (task['id'], test['args'])


# ---- 寫法不同但合理的作答：必須全對 ----

ALTERNATIVES = {
    'ZI': ['''from collections import defaultdict

from shop.money import average_cents
from shop.orders import counts_as_revenue, revenue_day, revenue_total


def average_order_value(orders):
    per_day = defaultdict(list)
    for order in filter(counts_as_revenue, orders):
        per_day[revenue_day(order)].append(revenue_total(order))
    return {day: average_cents(sum(v), len(v)) for day, v in sorted(per_day.items())}
''', '''from decimal import Decimal, ROUND_HALF_UP

from .clock import business_date
from .orders import is_gift_card, is_internal, live_lines


def average_order_value(orders):
    sums, counts = {}, {}
    for order in orders:
        if order["status"] not in ("paid", "fulfilled") or is_internal(order):
            continue
        goods = sum(l["qty"] * l["unit_cents"] for l in live_lines(order) if not is_gift_card(l))
        day = order["pickup_on"] or business_date(order["placed_at"])
        sums[day] = sums.get(day, 0) + max(0, goods - order["discount_cents"])
        counts[day] = counts.get(day, 0) + 1
    return {d: int((Decimal(sums[d]) / counts[d]).quantize(Decimal(1), rounding=ROUND_HALF_UP)) for d in sorted(sums)}
'''],
    'ZJ': ['''from .clock import business_date
from .orders import counts_as_revenue


def customers_per_day(orders):
    days = {}
    for order in orders:
        cid = order["customer_id"]
        if not counts_as_revenue(order) or cid == "guest" or cid.startswith("staff-"):
            continue
        days.setdefault(business_date(order["placed_at"]), set()).add(cid)
    return {day: len(people) for day, people in sorted(days.items())}
'''],
    'ZK': ['''from .customers import active, resolve_id
from .orders import counts_as_revenue, revenue_total


def lifetime_spend(orders, customers):
    result = {c["id"]: 0 for c in active(customers)}
    for order in orders:
        if counts_as_revenue(order):
            who = resolve_id(customers, order["customer_id"])
            if who in result:
                result[who] += revenue_total(order)
    return result
''', '''from .orders import REVENUE_STATUSES, is_internal, order_total


def lifetime_spend(orders, customers):
    parent = {c["id"]: c["merged_into"] for c in customers}

    def root(cid):
        while parent.get(cid):
            cid = parent[cid]
        return cid
    spend = {c["id"]: 0 for c in customers if not c["deleted_at"] and not c["merged_into"]}
    for order in orders:
        if order["status"] in REVENUE_STATUSES and not is_internal(order) and root(order["customer_id"]) in spend:
            spend[root(order["customer_id"])] += order_total(order)
    return spend
'''],
    'ZL': [v5.CUSTOMERS + '''

def newsletter_recipients(customers):
    return sorted(c["id"] for c in customers if can_email(c))
''', '''def newsletter_recipients(customers):
    ok = []
    for c in customers:
        if c["marketing_opt_in"] and c["email_verified_at"] and not c["deleted_at"] and not c["merged_into"]:
            ok.append(c["id"])
    ok.sort()
    return ok
'''],
    'ZM': ['''from collections import Counter

from .catalog import canonical_sku
from .orders import live_lines


def units_sold(orders):
    units = Counter()
    for order in orders:
        for line in live_lines(order):
            units[canonical_sku(line["sku"])] += line["qty"]
    return dict(units)
'''],
    'ZN': ['''from .orders import counts_as_revenue, revenue_day, revenue_total


def monthly_revenue(orders):
    """Revenue per month, e.g. {"2026-03": 1285000}."""
    totals = {}
    for order in orders:
        if counts_as_revenue(order):
            month = revenue_day(order)[:7]
            totals[month] = totals.get(month, 0) + revenue_total(order)
    return dict(sorted(totals.items()))
'''],
    'ZO': ['''from .customers import resolve_id
from .orders import counts_as_revenue, is_guest, is_staff, revenue_total


def top_customers(orders, customers, n):
    """The `n` biggest spenders as [[customer_id, cents], ...]; ties go to the smaller id."""
    closed = {c["id"] for c in customers if c["deleted_at"] is not None}
    spent = {}
    for order in orders:
        if (not counts_as_revenue(order) or is_guest(order) or is_staff(order)
                or order["customer_id"] in closed):
            continue
        who = resolve_id(customers, order["customer_id"])
        spent[who] = spent.get(who, 0) + revenue_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[who, cents] for who, cents in ranked[:n]]
'''],
    'ZP': ['''from .catalog import find_product
from .orders import counts_as_revenue, is_gift_card, live_lines


def revenue_by_category(orders, products):
    """Revenue per product category before discounts, e.g. {"bread": 12850}."""
    totals = {}
    for order in filter(counts_as_revenue, orders):
        for line in live_lines(order):
            if is_gift_card(line):
                continue
            name = find_product(products, line["sku"])["category"]
            totals[name] = totals.get(name, 0) + line["qty"] * line["unit_cents"]
    return totals
'''],
}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_reasonable_alternatives_pass_every_test(key):
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    for source in ALTERNATIVES[key[0]]:
        function = fam._module(source)[fam.entry]
        for test in task['public_tests'] + task['hidden_tests']:
            assert agrees(function, test), (task['id'], source[:60], test['args'])


# ---- 題目形狀、prompt 與程式庫 ----

CJK = re.compile(r'[　-鿿＀-￯]')


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_task_shape_prompt_and_determinism(key):
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    assert ct.make(*key) == task
    assert set(task) == {'id', 'family', 'level', 'seed', 'prompt', 'entry_point', 'public_tests', 'hidden_tests',
                         'kind', 'implicit_requirements', 'repo'}
    assert task['id'] == ids(key) and task['entry_point'] == fam.entry
    assert len(task['public_tests']) == ct.PUBLIC_COUNT
    assert tk.TICKET_HIDDEN_MIN <= len(task['hidden_tests']) <= tk.TICKET_HIDDEN_MAX
    prompt = task['prompt']
    assert prompt.startswith(rt.REPO_INTRO) and not CJK.search(prompt)
    assert prompt.rstrip().endswith(rt.REPLY.format(target=fam.target))
    assert len(json.dumps(prompt)) < 19000   # 審查重播要把 prompt 和作答一起送出，上限 24576 位元組
    for path, source in fam.files(key[1]).items():
        assert f'### {path}\n' in prompt and source.strip('\n') in prompt
    assert fam.files(key[1])['shop/orders.py'] == v5.ORDERS   # 用的是第五版程式庫
    assert ('### CONVENTIONS.md' in prompt) == (key[1] == 3)
    assert 'CONVENTIONS' not in prompt.split('### shop/__init__.py')[1]
    for test in task['public_tests']:
        assert f'- {ct._call(fam.entry, test["args"])} returns {test["expected"]!r}' in prompt
    assert task['repo'] == {'files': {p: s for p, s in fam.files(key[1]).items()
                                      if p.endswith('.py') and p != fam.target}, 'target': fam.target}
    if fam.kind == 'bugfix':
        assert fam.original_target.strip('\n') in prompt and task['public_tests'][0]['args'] == \
            fam.levels[key[1]]['repro']
        assert f'def {fam.entry}(' not in v5.BASE_FILES[fam.target]
    else:
        title, body = fam.ticket
        assert f'Ticket: {title}' in prompt and body in prompt and f'def {fam.entry}(' not in prompt
    keys = [json.dumps(t['args'], sort_keys=True) for t in task['public_tests'] + task['hidden_tests']]
    assert len(set(keys)) == len(keys)


def test_levels_differ_only_in_the_conventions_file():
    for letter in FAMILIES:
        three, five = built(letter, 3, 0)[0], built(letter, 5, 0)[0]
        assert three['implicit_requirements'] == five['implicit_requirements']
        assert three['repo'] == five['repo']
        files = FAMILIES[letter].files(3)
        assert files['CONVENTIONS.md'] == v5.CONVENTIONS and list(files)[0] == 'CONVENTIONS.md'
        assert FAMILIES[letter].files(5) == {k: v for k, v in files.items() if k != 'CONVENTIONS.md'}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_implicit_requirements_are_documented_in_the_repo_and_kept_out_of_the_prompt(key):
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    records = task['implicit_requirements']
    assert [r['id'] for r in records] == list(fam.levels[key[1]]['reqs']) + \
        ([tk.REGRESSION] if fam.kind == 'bugfix' else [])
    for record in records:
        assert CJK.search(record['requirement']) and CJK.search(record['why'])
        assert record['requirement'] not in task['prompt'] and record['id'] not in task['prompt']
        if record['id'] != tk.REGRESSION:
            assert record['basis'].startswith(rt._DOC_BASIS)
    assert 'implicit' not in task['prompt'].lower() and 'hidden test' not in task['prompt'].lower()


def test_every_new_convention_is_stated_in_a_docstring_as_well():
    # 第 5 級沒有 CONVENTIONS.md：新規則都要在程式庫的 docstring 或註解裡看得到。
    code = '\n'.join(v5.BASE_FILES.values())
    for phrase in ('Use this for every average', 'Every per-day, per-week and per-month\n    number groups by this date',
                   'they are never revenue', 'Every revenue number uses this', 'staff are not customers',
                   'it never counts as a customer', 'was never sold', 'per-customer numbers group by this id',
                   'Closed and merged-away accounts never show up', 'opted in\n    and has a verified address',
                   'the pickup day for a\n    preorder', 'only these count as revenue', 'used for every comparison'):
        assert phrase in code, phrase


def test_v4_tasks_are_unchanged_by_the_v5_codebase():
    assert rt.BASES['v4'] == (rt.BASE_FILES, rt.CONVENTIONS) and rt.BASES['v5'] == (v5.BASE_FILES, v5.CONVENTIONS)
    assert ct.frozen_code_tasks(ct.FROZEN_CODE_TASK_SETS['v4'])   # 第四版凍結檔照樣重建得出來


# ---- 公平性：隱藏測試只碰列出的要求、錯誤解的失敗都歸得到原因 ----

@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_hidden_tests_only_touch_listed_requirements_and_cover_each_one(key):
    task, meta = built(*key)
    fam = FAMILIES[key[0]]
    config = fam.levels[key[1]]
    reqs, explicit = set(config['reqs']), set(config['explicit'])
    counts = Counter()
    for test, row in zip(task['hidden_tests'], meta['hidden']):
        touched = fam.touches(test['args'], test['expected'])
        assert touched == set(row['touched']) and touched <= reqs | explicit, (task['id'], test['args'], touched)
        counts.update(touched & reqs)
    assert all(counts[c] >= tk.TICKET_COVERAGE for c in reqs), (task['id'], counts)
    for test in task['public_tests'][1 if fam.kind == 'bugfix' else 0:]:
        assert fam.touches(test['args'], test['expected']) == set(), (task['id'], test['args'])


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_every_trap_passes_the_public_examples_and_fails_hidden_tests_for_a_listed_reason(key):
    task, meta = built(*key)
    fam = FAMILIES[key[0]]
    config = fam.levels[key[1]]
    assert len(config['traps']) >= 2 and set(fam.trap_breaks) == set(fam.trap_targets)
    for trap in config['traps']:
        function = fam.trap(trap)
        assert all(agrees(function, test) for test in task['public_tests']), (task['id'], trap)
        credited = 0
        for test, row in zip(task['hidden_tests'], meta['hidden']):
            if agrees(function, test):
                continue
            touched = set(row['touched'])
            assert touched & fam.trap_breaks[trap] or tk.REGRESSION in fam.trap_breaks[trap], \
                (task['id'], trap, test['args'])
            credited += tk._attributable(fam, trap, touched, row['original_ok'])
        assert credited >= tk.TICKET_COVERAGE, (task['id'], trap, credited)


@pytest.mark.parametrize('key', [k for k in KEYS if FAMILIES[k[0]].kind == 'bugfix'], ids=ids)
def test_bug_report_reproduces_and_hidden_tests_include_regression_tests(key):
    task, meta = built(*key)
    original = FAMILIES[key[0]].original(key[1])
    repro, *others = task['public_tests']
    assert not agrees(original, repro) and all(agrees(original, t) for t in others)
    regression = [t for t in task['hidden_tests'] if agrees(original, t)]
    assert len(regression) >= tk.REGRESSION_MIN and len(regression) == meta['regression']


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_integer_truncation_is_caught_by_the_public_examples_of_the_average(key):
    if key[0] != 'ZI':
        return
    task, _ = built(*key)
    floor = FAMILIES['ZI']._module(v5.ZI_TRAPS['gift_in'].replace('average_cents(totals[day], counts[day])',
                                                                   'totals[day] // counts[day]')
                                   .replace('order_total', 'revenue_total'))['average_order_value']
    assert not all(agrees(floor, t) for t in task['public_tests'])   # 題目寫了「取整到分」，// 在公開範例就會錯


def test_family_inventory():
    assert list(FAMILIES) == ['ZI', 'ZJ', 'ZK', 'ZL', 'ZM', 'ZN', 'ZO', 'ZP']
    assert [f.kind for f in FAMILIES.values()].count('bugfix') == 3
    assert set(FAMILIES) <= set(ct.FAMILY_REVISION) and not set(FAMILIES) & set(rt.REPO_FAMILIES)
    assert {f.target for f in FAMILIES.values()} <= set(v5.BASE_FILES)
    for fam in FAMILIES.values():
        assert fam.base == 'v5' and set(fam.levels) == set(rt.LEVELS)
        assert {t for c in fam.levels.values() for t in c['traps']} == set(fam.trap_targets)
        assert (tk.REGRESSION in fam.requirements) == (fam.kind == 'bugfix')


# ---- 隔離環境：整條評分路徑 ----

def reply(code):
    return 'Here is the change.\n\n```python\n' + code + '\n```\n'


@needs_sandbox
def test_reference_alternatives_and_traps_through_the_real_grading_path():
    jobs = []
    for key in SEED0:
        task, _ = built(*key)
        fam = FAMILIES[key[0]]
        jobs.append((task, None, fam.reference_target))
        jobs.extend((task, None, source) for source in ALTERNATIVES[key[0]])
        jobs.extend((task, trap, fam.trap_targets[trap]) for trap in fam.levels[key[1]]['traps'])

    def run(job):
        task, trap, source = job
        return task['id'], trap, ct.BlindCodeEnvironment(task).validate(reply(source)), \
            ct.grade_hidden(task, reply(source))
    with ThreadPoolExecutor(4) as pool:
        for task_id, trap, public, hidden in pool.map(run, jobs):
            assert public == {'passed': True, 'checks': []}, (task_id, trap, public)
            assert hidden['status'] == 'ok' and hidden['passed'] is (trap is None), (task_id, trap, hidden)


@needs_sandbox
def test_grading_path_reports_original_bugs():
    for letter in ('ZN', 'ZO', 'ZP'):
        task = built(letter, 5, 0)[0]
        checks = ct.BlindCodeEnvironment(task).validate(reply(FAMILIES[letter].original_target))
        assert checks['passed'] is False and checks['checks'][0] == 'public_test_failed'


# ---- 凍結 ----

def test_committed_v5_frozen_set_matches_the_generator():
    frozen = json.loads(ct.FROZEN_CODE_TASKS_V5.read_text())
    assert frozen['version'] == v5.REPO_VERSION and set(frozen['family_revision']) == set(FAMILIES)
    tasks = ct.frozen_code_tasks(ct.FROZEN_CODE_TASK_SETS['v5'])
    assert [(t['family'], t['level'], t['seed']) for t in tasks] == \
        [(t['family'], t['level'], t['seed']) for t in ct.interleave(sorted(
            (built(*k)[0] for k in KEYS), key=lambda t: (t['family'], t['level'], t['seed'])))]
    assert [t['id'] for t in tasks[:8]] == [f'{f}3-0' for f in FAMILIES]
    assert all(t == built(t['family'], t['level'], t['seed'])[0] for t in tasks)


def test_frozen_sets_refuse_mixing_v4_and_v5(tmp_path):
    with pytest.raises(ValueError, match='exactly one version'):
        ct.freeze(tmp_path / 'mixed.json', [built('ZI', 3, 0)[0], tk.build_ticket('ZA', 3, 0)[0]])
