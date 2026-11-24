"""第四版盲測程式題（ZA～ZH，程式庫情境題）：公平性、錯誤解、獨立核對、多檔案評分與凍結。全部離線。

核對方式（全部寫在這個檔案，和 code_repo_tasks 的參考解不同）：
- CHECKERS：每類不經程式庫、用另一種寫法重算全部期望值。
- ALTERNATIVES：寫法不同但合理的作答（自己重寫輔助邏輯、換用其他既有函式、相對或絕對匯入），必須過全部測試。
錯誤解：公開範例全對、隱藏測試至少錯 TICKET_COVERAGE 題，且每個失敗都能歸到它違反的要求；
另在隔離環境確認整條評分路徑（其他檔案＋作答一起載入）對參考解、錯誤解、語法錯、延遲匯入都正確。
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from fractions import Fraction
from functools import lru_cache
import json
import math
import re
import sys

import pytest

from cloudyfan_jev import code_repo_tasks as rt
from cloudyfan_jev import code_sandbox as sandbox
from cloudyfan_jev import code_tasks as ct
from cloudyfan_jev import code_tickets as tk

needs_sandbox = pytest.mark.skipif(not sandbox.sandbox_available(), reason='sandbox-exec is only available on macOS')
FAMILIES = rt.REPO_FAMILIES
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


def email(text):
    user, domain = text.strip().lower().split('@')
    if domain in {'gmail.com', 'googlemail.com'}:
        return re.sub(r'\+.*$', '', user).replace('.', '') + '@gmail.com'
    return user + '@' + domain


def stamp(ts):
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ')


def biz_day(ts):
    return (stamp(ts) + timedelta(hours=4)).date().isoformat()   # +8 小時到台北、再 -4 小時到營業日


def total(order):
    return max(0, sum(l['qty'] * l['unit_cents'] for l in order['lines']) - order['discount_cents'])


def za(orders, products):
    cat = {p['sku']: p['category'] for p in products}
    out = Counter()
    for o in orders:
        if o['status'] in REVENUE and not o['customer_id'].startswith('qa-'):
            for l in o['lines']:
                out[cat[sku(l['sku'])]] += l['qty'] * l['unit_cents']
    return dict(out)


def zb(customers):
    groups = {}
    for c in customers:
        if c['deleted_at'] is None:
            groups.setdefault(email(c['email']), set()).add(c['id'])
    return sorted(sorted(g) for g in groups.values() if len(g) >= 2)


def zc(orders, day):
    out = [0] * 24
    for o in orders:
        if not o['customer_id'].startswith('qa-') and biz_day(o['placed_at']) == day:
            out[(stamp(o['placed_at']).hour + 8) % 24] += 1
    return out


def zd(order, percent):
    exact = Fraction(sum(l['qty'] * l['unit_cents'] for l in order['lines'])) * Fraction(str(percent)) / 100
    return order['discount_cents'] + math.floor(exact + Fraction(1, 2))


def ze(orders):
    out = Counter()
    for o in orders:
        if o['status'] in REVENUE and not o['customer_id'].startswith('qa-'):
            year, week, _ = datetime.fromisoformat(biz_day(o['placed_at'])).isocalendar()
            out['%d-W%02d' % (year, week)] += total(o)
    return dict(sorted(out.items()))


def zf(customers, query):
    hits = [c['id'] for c in customers if c['deleted_at'] is None and email(c['email']) == email(query)]
    assert len(hits) <= 1
    return hits[0] if hits else None


def zg(stock, products, threshold):
    have = Counter()
    for row in stock:
        have[sku(row['sku'])] += row['on_hand']
    return sorted(p['sku'] for p in products if not p['archived'] and have[p['sku']] < threshold)


def zh(orders, n):
    spent = Counter()
    for o in orders:
        if o['status'] in REVENUE and not o['customer_id'].startswith('qa-'):
            spent[o['customer_id']] += total(o)
    return [[c, v] for c, v in sorted(spent.items(), key=lambda kv: (-kv[1], kv[0]))][:n]


CHECKERS = {'ZA': za, 'ZB': zb, 'ZC': zc, 'ZD': zd, 'ZE': ze, 'ZF': zf, 'ZG': zg, 'ZH': zh}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_expected_values_match_an_independent_implementation(key):
    task, _ = built(*key)
    for test in task['public_tests'] + task['hidden_tests']:
        assert sandbox.strict_equal(CHECKERS[key[0]](*args_of(test)), test['expected']), (task['id'], test['args'])


# ---- 寫法不同但合理的作答：必須全對 ----

ALTERNATIVES = {
    'ZA': ['''"""Numbers."""
from .catalog import find_product
from .orders import counts_as_revenue


def revenue_by_category(orders, products):
    result = {}
    for order in filter(counts_as_revenue, orders):
        for line in order["lines"]:
            category = find_product(products, line["sku"])["category"]
            result[category] = result.get(category, 0) + line["qty"] * line["unit_cents"]
    return result
'''],
    'ZB': [rt.CUSTOMERS + '''
from collections import defaultdict


def duplicate_accounts(customers):
    seen = defaultdict(list)
    for c in active(customers):
        seen[normalize_email(c["email"])].append(c["id"])
    groups = [sorted(v) for v in seen.values() if len(v) > 1]
    groups.sort(key=lambda g: g[0])
    return groups
''', '''def _norm(address):
    local, domain = address.strip().lower().rsplit("@", 1)
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
        domain = "gmail.com"
    return local + "@" + domain


def duplicate_accounts(customers):
    groups = {}
    for c in customers:
        if c["deleted_at"]:
            continue
        groups.setdefault(_norm(c["email"]), []).append(c["id"])
    return sorted(sorted(g) for g in groups.values() if len(g) >= 2)
'''],
    'ZC': ['''from datetime import timedelta

from .clock import BUSINESS_DAY_CUTOFF_HOUR, local_time
from .orders import is_internal


def orders_per_hour(orders, day):
    hours = [0] * 24
    for order in orders:
        if is_internal(order):
            continue
        when = local_time(order["placed_at"])
        if (when - timedelta(hours=BUSINESS_DAY_CUTOFF_HOUR)).strftime("%Y-%m-%d") == day:
            hours[when.hour] += 1
    return hours
'''],
    'ZD': ['''from fractions import Fraction
import math

REVENUE_STATUSES = ("paid", "fulfilled")


def subtotal(order):
    return sum(line["qty"] * line["unit_cents"] for line in order["lines"])


def add_coupon(order, percent):
    off = Fraction(subtotal(order)) * Fraction(str(percent)) / 100
    return order["discount_cents"] + math.floor(off + Fraction(1, 2))
''', '''def subtotal(order):
    return sum(line["qty"] * line["unit_cents"] for line in order["lines"])


def add_coupon(order, percent):
    from shop.money import percent_of   # 延遲匯入也要能用
    return order["discount_cents"] + percent_of(subtotal(order), percent)
'''],
    'ZE': ['''from datetime import datetime

from .clock import business_date
from .orders import counts_as_revenue, order_total


def weekly_revenue(orders):
    totals = {}
    for order in orders:
        if counts_as_revenue(order):
            key = datetime.strptime(business_date(order["placed_at"]), "%Y-%m-%d").strftime("%G-W%V")
            totals[key] = totals.get(key, 0) + order_total(order)
    return dict(sorted(totals.items()))
'''],
    'ZF': [rt.CUSTOMERS + '''


def find_customer(customers, email):
    wanted = normalize_email(email)
    matches = [c["id"] for c in active(customers) if normalize_email(c["email"]) == wanted]
    return matches[0] if matches else None
'''],
    'ZG': [rt.CATALOG + '''
from collections import Counter


def restock_list(stock, products, threshold):
    counts = Counter()
    for row in stock:
        counts[canonical_sku(row["sku"])] += row["on_hand"]
    return sorted(p["sku"] for p in listed(products) if counts[p["sku"]] < threshold)
''', rt.CATALOG + '''

def restock_list(stock, products, threshold):
    on_hand = {p["sku"]: 0 for p in products if not p["archived"]}
    for row in stock:
        product = find_product(products, row["sku"])
        if product is not None and product["sku"] in on_hand:
            on_hand[product["sku"]] += row["on_hand"]
    return sorted(s for s, n in on_hand.items() if n < threshold)
'''],
    'ZH': ['''from .orders import counts_as_revenue, order_total


def top_customers(orders, n):
    """The `n` biggest spenders as [[customer_id, cents], ...]; ties go to the smaller id."""
    spent = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        spent[order["customer_id"]] = spent.get(order["customer_id"], 0) + order_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]
'''],
}


@pytest.mark.parametrize('key', KEYS, ids=ids)
def test_reasonable_alternatives_pass_every_test(key):
    task, _ = built(*key)
    fam = FAMILIES[key[0]]
    for source in ALTERNATIVES[key[0]]:
        if 'from shop.money import percent_of   #' in source:
            continue   # 延遲匯入在行程內無法用（模組已拿掉），在隔離環境另外驗
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
    for path, source in fam.files(key[1]).items():
        assert f'### {path}\n' in prompt and source.strip('\n') in prompt
    assert ('### CONVENTIONS.md' in prompt) == (key[1] == 3)
    assert 'CONVENTIONS' not in prompt.split('### shop/__init__.py')[1]   # 第 5 級沒有任何地方提到慣例檔
    for test in task['public_tests']:
        assert f'- {ct._call(fam.entry, test["args"])} returns {test["expected"]!r}' in prompt
    assert task['repo'] == {'files': {p: s for p, s in fam.files(key[1]).items()
                                      if p.endswith('.py') and p != fam.target}, 'target': fam.target}
    if fam.kind == 'bugfix':
        assert fam.original_target.strip('\n') in prompt and task['public_tests'][0]['args'] == \
            fam.levels[key[1]]['repro']
        assert f'def {fam.entry}(' not in rt.BASE_FILES[fam.target]
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
        files = rt.REPO_FAMILIES[letter].files(3)
        assert list(files)[0] == 'CONVENTIONS.md' and rt.REPO_FAMILIES[letter].files(5) == \
            {k: v for k, v in files.items() if k != 'CONVENTIONS.md'}


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


def test_every_convention_the_tests_rely_on_is_stated_in_a_docstring_as_well():
    # 第 5 級沒有 CONVENTIONS.md：每條慣例都要在程式庫的 docstring 或註解裡看得到。
    code = '\n'.join(rt.BASE_FILES.values())
    for phrase in ('rounded half-up', 'Use this for every percentage', 'business day', '04:00 to 03:59',
                   'only these count as revenue', 'Reports leave them out', 'only\n    compare SKUs',
                   'used for every comparison', 'Closed accounts never show up', 'Archived products only appear'):
        assert phrase in code, phrase


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


def test_family_inventory():
    assert list(FAMILIES) == ['ZA', 'ZB', 'ZC', 'ZD', 'ZE', 'ZF', 'ZG', 'ZH']
    assert [f.kind for f in FAMILIES.values()].count('bugfix') == 3
    assert set(FAMILIES) <= set(ct.FAMILY_REVISION) and not set(FAMILIES) & set(tk.TICKET_FAMILIES)
    assert {f.target for f in FAMILIES.values()} <= set(rt.BASE_FILES)
    for fam in FAMILIES.values():
        assert set(fam.levels) == set(rt.LEVELS)
        assert {t for c in fam.levels.values() for t in c['traps']} == set(fam.trap_targets)
        assert (tk.REGRESSION in fam.requirements) == (fam.kind == 'bugfix')


def test_in_process_loading_leaves_no_trace():
    before_path, before_modules = list(sys.meta_path), set(sys.modules)
    fam = FAMILIES['ZA']
    rt._repo_namespace(rt.repo_program(rt.BASE_FILES, fam.target, fam.trap_targets['raw_sku'] + '\n# fresh\n',
                                       fam.entry))
    assert sys.meta_path == before_path
    assert not {m for m in set(sys.modules) - before_modules if m == 'shop' or m.startswith('shop.')}


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
def test_grading_path_reports_original_bugs_syntax_errors_and_missing_entry_points():
    for letter in ('ZE', 'ZF', 'ZH'):
        task = built(letter, 5, 0)[0]
        checks = ct.BlindCodeEnvironment(task).validate(reply(FAMILIES[letter].original_target))
        assert checks['passed'] is False and checks['checks'][0] == 'public_test_failed'
    task = built('ZA', 3, 0)[0]
    env = ct.BlindCodeEnvironment(task)
    assert env.validate(reply('def revenue_by_category(orders, products):\n    return {')) == \
        {'passed': False, 'checks': ['syntax_error']}
    assert env.validate('no code here') == {'passed': False, 'checks': ['code_block_missing']}
    missing = env.validate(reply('from .orders import order_total\n'))
    assert missing['passed'] is False and missing['checks'][0].startswith('load_')
    assert ct.solution_code(task, reply('x = 1')) != 'x = 1' and ct.solution_code({'entry_point': 'f'}, reply('x')).strip() == 'x'


# ---- 凍結 ----

def test_committed_v4_frozen_set_matches_the_generator():
    frozen = json.loads(ct.FROZEN_CODE_TASKS_V4.read_text())
    assert frozen['version'] == rt.REPO_VERSION and set(frozen['family_revision']) == set(FAMILIES)
    tasks = ct.frozen_code_tasks(ct.FROZEN_CODE_TASK_SETS['v4'])
    assert [(t['family'], t['level'], t['seed']) for t in tasks] == \
        [(t['family'], t['level'], t['seed']) for t in ct.interleave(sorted(
            (built(*k)[0] for k in KEYS), key=lambda t: (t['family'], t['level'], t['seed'])))]
    assert [t['id'] for t in tasks[:8]] == [f'{f}3-0' for f in FAMILIES]
    assert all(t == built(t['family'], t['level'], t['seed'])[0] for t in tasks)
    assert ct.FROZEN_CODE_TASKS == ct.FROZEN_CODE_TASKS_V3   # 預設仍是第三版；第四版要明確指定 --task-set v4


def test_frozen_sets_refuse_mixing_v3_and_v4(tmp_path):
    with pytest.raises(ValueError, match='exactly one version'):
        ct.freeze(tmp_path / 'mixed.json', [built('ZA', 3, 0)[0], ct.make('Q', 1, 0)])
