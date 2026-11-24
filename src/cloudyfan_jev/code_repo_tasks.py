"""第四版盲測改程式題：程式庫情境題（ZA～ZH）。設計見 plans/mrc-blind-validation-experiment-2026-11-24.md。

第三版工單題的隱含要求都是「一般工程師的常識」，連 Haiku 5.5 低強度都會主動補齊。第四版讓正確做法取決於
**程式庫裡別的檔案**：題目附上一個小型麵包連鎖店後台的完整程式庫（7 個檔案），工單或錯誤回報只談要做的事；
金額取整、營業日、哪些狀態算營收、內部測試單、SKU 與 email 的正規化、已關閉帳號、下架商品這些規則，
寫在其他檔案的輔助函式說明與既有功能的寫法裡。

- 第 3 級：多附一份 CONVENTIONS.md，把團隊慣例條列出來。
- 第 5 級：沒有 CONVENTIONS.md，慣例只看得到輔助函式的 docstring 與相似功能的寫法。

公平性沿用第三版的做法（``code_tickets``）：每條隱含要求是一個類別，參考解 ``_solve(..., skip)`` 可單獨關掉一條；
隱藏測試只收「碰到的要求都在這級的 reqs／explicit 內」的輸入；錯誤解要過全部公開範例並在隱藏測試失敗。

作答與評分：模型回傳目標檔案（例如 ``shop/reports.py``）的完整新內容。評分時用 ``repo_program`` 把其他檔案
與作答一起包成一段程式（自訂匯入器，``import shop.xxx`` 與相對匯入都能用），在同一個隔離執行器裡跑。
"""
import json
from datetime import datetime, timedelta
from functools import lru_cache
import sys

from .code_tasks import _call
from . import code_tickets as tk

REPO_VERSION = 'cloudyfan-code-tasks-v4'
PACKAGE = 'shop'
LEVELS = (3, 5)

# ---- 程式庫 ----

INIT = r'''"""Back office for the Crumb & Co. bakery chain."""
'''

MONEY = r'''"""Money helpers. Every amount in the shop is an int number of cents."""
from decimal import Decimal, ROUND_HALF_UP


def percent_of(cents, percent):
    """`percent`% of `cents`, rounded half-up to a whole cent.

    Use this for every percentage (discounts, tax, commission) so that all
    screens and reports agree to the cent. `percent` may have decimals (12.5).
    """
    value = Decimal(cents) * Decimal(str(percent)) / Decimal(100)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
'''

CLOCK = r'''"""Time helpers. Timestamps are ISO-8601 UTC strings such as "2026-03-01T14:05:00Z"."""
from datetime import datetime, timedelta, timezone

STORE_TZ = timezone(timedelta(hours=8))  # every store is in Taipei
BUSINESS_DAY_CUTOFF_HOUR = 4  # the night shift closes at 04:00 store time


def parse_ts(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local_time(ts):
    """The store-local datetime of a UTC timestamp."""
    return parse_ts(ts).astimezone(STORE_TZ)


def business_date(ts):
    """The business day a timestamp belongs to, as "YYYY-MM-DD".

    A business day runs from 04:00 to 03:59 store time, so a sale at 01:30 on
    March 2nd is booked on March 1st.
    """
    shifted = local_time(ts) - timedelta(hours=BUSINESS_DAY_CUTOFF_HOUR)
    return shifted.date().isoformat()
'''

CATALOG = r'''"""Product catalog. A product looks like
{"sku": "BRD-001", "name": "Country loaf", "category": "bread", "archived": False}.
"""


def canonical_sku(raw):
    """SKUs are stored upper-case with dashes ("BRD-001"). The POS and the
    warehouse scanner send whatever was typed ("brd 001", " Brd_001"), so only
    compare SKUs after passing them through this function."""
    return "-".join(raw.strip().upper().replace("_", " ").replace("-", " ").split())


def listed(products):
    """Products the shop still sells. Archived products only appear on old orders."""
    return [p for p in products if not p["archived"]]


def find_product(products, raw_sku):
    """The product a POS or scanner SKU refers to, or None."""
    sku = canonical_sku(raw_sku)
    for product in products:
        if product["sku"] == sku:
            return product
    return None
'''

CUSTOMERS = r'''"""Customers look like {"id": "c-117", "email": "Ann@Example.com", "deleted_at": None}.

`deleted_at` is set (an ISO timestamp) when a customer closes their account;
the record is kept for accounting.
"""


def normalize_email(email):
    """The canonical form of an email address, used for every comparison.

    Lower-cases and trims the address. Gmail ignores dots and anything after a
    "+" in the local part, and googlemail.com is the same mailbox as gmail.com,
    so those are folded too.
    """
    local, _, domain = email.strip().lower().rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def active(customers):
    """Customers whose account is open. Closed accounts never show up in the product."""
    return [c for c in customers if c.get("deleted_at") is None]
'''

ORDERS = r'''"""Orders look like
{"id": "o-1", "customer_id": "c-117", "placed_at": "2026-03-01T06:05:00Z",
 "status": "paid", "discount_cents": 0,
 "lines": [{"sku": "BRD-001", "qty": 2, "unit_cents": 350}]}

Status moves pending -> paid -> fulfilled, or ends as cancelled or refunded.
"""

REVENUE_STATUSES = ("paid", "fulfilled")  # only these count as revenue


def is_internal(order):
    """Orders placed by staff accounts ("qa-...") to test the tills. Reports leave them out."""
    return order["customer_id"].startswith("qa-")


def subtotal(order):
    return sum(line["qty"] * line["unit_cents"] for line in order["lines"])


def order_total(order):
    """What the customer pays: subtotal minus discount, never below zero."""
    return max(0, subtotal(order) - order["discount_cents"])


def counts_as_revenue(order):
    return order["status"] in REVENUE_STATUSES and not is_internal(order)
'''

REPORTS_HEAD = r'''"""Numbers for the finance and operations teams."""
<<imports>>


def daily_revenue(orders):
    """Revenue per business day, e.g. {"2026-03-01": 12850}."""
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        day = business_date(order["placed_at"])
        totals[day] = totals.get(day, 0) + order_total(order)
    return dict(sorted(totals.items()))
'''
REPORTS_IMPORTS = 'from .clock import business_date\nfrom .orders import counts_as_revenue, order_total'
REPORTS = tk._fill(REPORTS_HEAD, imports=REPORTS_IMPORTS)

CONVENTIONS = r'''# Team conventions

- Money is always an int number of cents. Every percentage goes through `money.percent_of` (rounds half-up).
- Timestamps are UTC. Anything "per day" uses the business day from `clock.business_date`
  (04:00-03:59 store time), never the UTC date.
- Revenue counts only orders whose status is in `orders.REVENUE_STATUSES`.
- Reports leave out internal test orders (`orders.is_internal`).
- Compare SKUs only through `catalog.canonical_sku`.
- Compare emails only through `customers.normalize_email`.
- Closed accounts (`deleted_at` set) never show up anywhere; use `customers.active`.
- Archived products are hidden everywhere except on old orders; use `catalog.listed`.
'''

BASE_FILES = {'shop/__init__.py': INIT, 'shop/money.py': MONEY, 'shop/clock.py': CLOCK, 'shop/catalog.py': CATALOG,
              'shop/customers.py': CUSTOMERS, 'shop/orders.py': ORDERS, 'shop/reports.py': REPORTS}

# ---- 把程式庫與作答包成一段可在隔離執行器裡跑的程式 ----

_LOADER = r'''import importlib as _importlib
import importlib.abc as _importlib_abc
import importlib.util as _importlib_util
import sys as _sys


class _RepoFinder(_importlib_abc.MetaPathFinder, _importlib_abc.Loader):
    def __init__(self, files):
        self.files = files

    def _path(self, name):
        rel = name.replace('.', '/')
        if rel + '/__init__.py' in self.files:
            return rel + '/__init__.py', True
        if rel + '.py' in self.files:
            return rel + '.py', False
        return None, False

    def find_spec(self, name, path=None, target=None):
        found, package = self._path(name)
        if found is None:
            return None
        return _importlib_util.spec_from_loader(name, self, origin=found, is_package=package)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        found, _ = self._path(module.__name__)
        exec(compile(self.files[found], found, 'exec'), module.__dict__)


_sys.meta_path.insert(0, _RepoFinder(<<files>>))
_target_module = _importlib.import_module(<<module>>)
<<entry>> = getattr(_target_module, <<entry_name>>)
'''


def module_name(path):
    return path[:-len('.py')].replace('/', '.')


def repo_program(files, target, answer, entry):
    """其他檔案＋作答（目標檔案的新內容）→ 一段程式；執行後 ``entry`` 綁在最外層。"""
    sources = {p: s for p, s in files.items() if p.endswith('.py')}
    sources[target] = answer
    return tk._fill(_LOADER, files=repr(sources), module=repr(module_name(target)), entry=entry,
                    entry_name=repr(entry))


@lru_cache(maxsize=None)
def _repo_namespace(program):
    """在本行程裡執行（只給可信的參考解、錯誤解與原始程式）；執行完把匯入器與 shop 模組拿掉。"""
    saved_path = list(sys.meta_path)
    saved = {k: v for k, v in sys.modules.items() if k == PACKAGE or k.startswith(PACKAGE + '.')}
    for k in saved:
        del sys.modules[k]
    namespace = {'__name__': 'cloudyfan_repo_ticket'}
    try:
        exec(compile(program, '<repo-ticket>', 'exec'), namespace)
    finally:
        sys.meta_path[:] = saved_path
        for k in [k for k in sys.modules if k == PACKAGE or k.startswith(PACKAGE + '.')]:
            del sys.modules[k]
        sys.modules.update(saved)
    return namespace


@lru_cache(maxsize=None)
def _base_namespace(target, target_source, entry):
    return _repo_namespace(repo_program(BASE_FILES, target, target_source, entry))


# ---- 產生測試資料用的小工具（在本行程，不進程式庫）----

PRODUCTS = [
    {'sku': 'BRD-001', 'name': 'Country loaf', 'category': 'bread', 'archived': False},
    {'sku': 'BRD-002', 'name': 'Rye loaf', 'category': 'bread', 'archived': False},
    {'sku': 'PST-010', 'name': 'Croissant', 'category': 'pastry', 'archived': False},
    {'sku': 'PST-011', 'name': 'Pain au chocolat', 'category': 'pastry', 'archived': False},
    {'sku': 'CAK-020', 'name': 'Cheesecake slice', 'category': 'cake', 'archived': False},
    {'sku': 'DRK-030', 'name': 'Latte', 'category': 'drinks', 'archived': False},
    {'sku': 'DRK-031', 'name': 'Black tea', 'category': 'drinks', 'archived': False},
]
ARCHIVED = [
    {'sku': 'PST-012', 'name': 'Matcha roll', 'category': 'pastry', 'archived': True},
    {'sku': 'DRK-032', 'name': 'Pumpkin spice latte', 'category': 'drinks', 'archived': True},
]
PRICES = {'BRD-001': 180, 'BRD-002': 220, 'PST-010': 65, 'PST-011': 75, 'CAK-020': 120, 'DRK-030': 110,
          'DRK-031': 70, 'PST-012': 85, 'DRK-032': 130}
NAMES = ('ann.lee', 'bo', 'chen.wei', 'dana', 'eli.wu', 'fay', 'gus.lin', 'hana', 'ivy.tsai', 'jo')
DOMAINS = ('example.com', 'mail.tw', 'gmail.com')
REVENUE = ('paid', 'fulfilled')
OTHER = ('pending', 'cancelled', 'refunded')


def _utc(local):
    return (local - timedelta(hours=8)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _local(day, hour, minute=0):
    return datetime.fromisoformat(day) + timedelta(hours=hour, minutes=minute)


def _raw_sku(rng, sku):
    return rng.choice((sku.lower(), sku.replace('-', ' ').lower(), sku.replace('-', '_'), ' ' + sku + ' ',
                       sku.capitalize(), sku.replace('-', ' ')))


def _line(sku, qty, raw=None):
    return {'sku': raw or sku, 'qty': qty, 'unit_cents': PRICES[sku]}


def _order(oid, customer, local, status, lines, discount=0):
    return {'id': oid, 'customer_id': customer, 'placed_at': _utc(local), 'status': status,
            'discount_cents': discount, 'lines': lines}


def _customer_ids(rng, count):
    return ['c-%d' % n for n in rng.sample(range(101, 200), count)]


def _status(rng, revenue_share=.55):
    return rng.choice(REVENUE) if rng.random() < revenue_share else rng.choice(OTHER)


# ---- 題型基底 ----

REPO_INTRO = "You are working in our team's Python repository. These are all of its files."
REPLY = ('Reply with exactly one ```python code block that contains the complete new contents of `{target}`. '
         'The other files stay exactly as shown, and your code may import from them. Use only the Python '
         'standard library. The code must not read or write files, access the network, start processes or print '
         'anything. Do not include tests or example calls in the block.')


class RepoFamily(tk.TicketFamily):
    """一類程式庫情境題。``target`` 是要改的檔案；修 bug 題的 ``original_target`` 是目前內容（含 bug）。"""
    target = ''
    original_target = ''   # 修 bug 題：目標檔案目前的內容；新功能題用 BASE_FILES 裡的內容
    report = ''            # 修 bug 題：回報；{call}、{got}、{expected} 由產生器填入
    ticket = ('', '')      # 新功能題：(標題, 內文)
    reference_target = ''  # 參考解（目標檔案內容，含 _solve）
    trap_targets = {}      # 錯誤解 -> 目標檔案內容

    def files(self, level):
        files = dict(BASE_FILES)
        if self.kind == 'bugfix':
            files[self.target] = self.original_target
        if level == 3:
            files = {'CONVENTIONS.md': CONVENTIONS, **files}
        return files

    def _module(self, target_source):
        return _base_namespace(self.target, target_source, self.entry)

    def reference(self):
        return self._module(self.reference_target)[self.entry]

    def trap(self, name):
        return self._module(self.trap_targets[name])[self.entry]

    def original(self, level):
        return self._module(self.original_target)[self.entry]

    def solve(self, args, skip=()):
        module = self._module(self.reference_target)['_target_module']
        return module._solve(*tk._json_copy(args), skip=frozenset(skip))

    def task_prompt(self, level, public):
        lines = [REPO_INTRO, '']
        for path, source in self.files(level).items():
            lang = 'markdown' if path.endswith('.md') else 'python'
            lines += [f'### {path}', f'```{lang}', source.strip('\n'), '```', '']
        examples = [f'- {_call(self.entry, t["args"])} returns {t["expected"]!r}' for t in public]
        if self.kind == 'feature':
            title, body = self.ticket
            lines += ["You picked up this ticket from the team's issue tracker.", '', f'Ticket: {title}', '', body,
                      '', 'Examples from the ticket:', *examples, '', f'Implement it in `{self.target}`.']
        else:
            repro = public[0]
            report = self.report.format(call=_call(self.entry, repro['args']),
                                        got=tk._outcome(self.original(level), repro['args']),
                                        expected=repr(repro['expected']))
            lines += ['A user filed the bug report below.', '', 'Bug report:', report, '', tk.BUG_EXAMPLES,
                      *examples, '', f'Fix it in `{self.target}`.']
        return '\n'.join(lines + ['', REPLY.format(target=self.target)])

    def repo(self, level):
        return {'files': {p: s for p, s in self.files(level).items() if p.endswith('.py') and p != self.target},
                'target': self.target}


def _levels(reqs, traps, explicit=(), repro=None):
    config = {'variant': 'repo', 'reqs': tuple(reqs), 'traps': tuple(traps), 'explicit': tuple(explicit)}
    if repro is not None:
        config['repro'] = repro
    return {level: dict(config) for level in LEVELS}


def _reports(imports, body):
    return tk._fill(REPORTS_HEAD, imports=imports) + '\n\n' + body.strip('\n') + '\n'


_DOC_BASIS = 'CONVENTIONS.md（第 3 級）；第 5 級只看得到 '

# ---- ZA：各品類營收（新功能，shop/reports.py）----

ZA_IMPORTS = ('from .catalog import canonical_sku\nfrom .clock import business_date\n'
              'from .orders import REVENUE_STATUSES, counts_as_revenue, is_internal, order_total')
ZA_REFERENCE = _reports(ZA_IMPORTS, r'''
def _solve(orders, products, skip=frozenset()):
    category = {p["sku"]: p["category"] for p in products}
    totals = {}
    for order in orders:
        both = "za_filters" in skip
        if not both and "za_status" not in skip and order["status"] not in REVENUE_STATUSES:
            continue
        if not both and "za_internal" not in skip and is_internal(order):
            continue
        for line in order["lines"]:
            sku = line["sku"] if "za_sku" in skip else canonical_sku(line["sku"])
            name = category[sku]
            totals[name] = totals.get(name, 0) + line["qty"] * line["unit_cents"]
    return dict(sorted(totals.items()))


def revenue_by_category(orders, products):
    return _solve(orders, products)
''')
_ZA_TRAP = r'''
def revenue_by_category(orders, products):
    category = {p["sku"]: p["category"] for p in products}
    totals = {}
    for order in orders:
        if <<keep>>:
            continue
        for line in order["lines"]:
            name = category[<<sku>>]
            totals[name] = totals.get(name, 0) + line["qty"] * line["unit_cents"]
    return totals
'''
ZA_TRAPS = {
    'all_orders': _reports(ZA_IMPORTS, tk._fill(_ZA_TRAP, keep='False', sku='canonical_sku(line["sku"])')),
    'status_only': _reports(ZA_IMPORTS, tk._fill(_ZA_TRAP, keep='order["status"] not in REVENUE_STATUSES',
                                                 sku='canonical_sku(line["sku"])')),
    'raw_sku': _reports(ZA_IMPORTS, tk._fill(_ZA_TRAP, keep='not counts_as_revenue(order)', sku='line["sku"]')),
}


class RevenueByCategory(RepoFamily):
    letter, name, kind, entry, target = 'ZA', 'revenue_by_category', 'feature', 'revenue_by_category', \
        'shop/reports.py'
    reference_target, trap_targets = ZA_REFERENCE, ZA_TRAPS
    ticket = ('Revenue per product category',
              'Finance wants to see revenue split by product category. Please add '
              '`revenue_by_category(orders, products)` next to the other reports. `orders` and `products` are '
              'lists in the usual shapes. Return a dict from category to revenue in cents, using the line amounts '
              '(quantity times unit price); order-level discounts are not split across categories. Leave out '
              'categories with no revenue.')
    trap_breaks = {'all_orders': {'za_status', 'za_internal'}, 'status_only': {'za_internal'},
                   'raw_sku': {'za_sku'}}
    # 同時是內部單又不算營收的訂單，只有兩個條件一起關掉才看得出來：兩條要求都算碰到。
    probe_skips = {'za_status': ('za_status', 'za_filters'), 'za_internal': ('za_internal', 'za_filters')}
    requirements = {
        'za_status': ('只算狀態在 REVENUE_STATUSES（paid、fulfilled）的訂單；pending、cancelled、refunded 不算營收。',
                      '工單要的是「營收」；程式庫定義了哪些狀態算營收，daily_revenue 也這樣算。',
                      _DOC_BASIS + 'orders.REVENUE_STATUSES 的註解與 reports.daily_revenue 的寫法'),
        'za_internal': ('排除內部測試單（customer_id 以 qa- 開頭）。',
                        '程式庫寫明報表要排除內部測試單，daily_revenue 也排除。',
                        _DOC_BASIS + 'orders.is_internal 的 docstring 與 reports.daily_revenue 的寫法'),
        'za_sku': ('明細上的 SKU 要先經 canonical_sku 再對商品目錄（POS 會送小寫、空白或底線）。',
                   '程式庫寫明 POS 送來的 SKU 要正規化後才能比對。',
                   _DOC_BASIS + 'catalog.canonical_sku 的 docstring 與 catalog.find_product 的寫法'),
    }
    levels = _levels(('za_status', 'za_internal', 'za_sku'), ('all_orders', 'status_only', 'raw_sku'))

    def _case(self, rng, plain):
        products = rng.sample(PRODUCTS, rng.randint(3, 5))
        orders = []
        for n in range(rng.randint(2, 4) if plain else rng.randint(2, 6)):
            status = rng.choice(REVENUE) if plain else _status(rng)
            customer = 'qa-0%d' % rng.randint(1, 3) if not plain and rng.random() < .2 else \
                'c-%d' % rng.randint(101, 199)
            lines = []
            for p in rng.sample(products, rng.randint(1, 2)):
                raw = _raw_sku(rng, p['sku']) if not plain and rng.random() < .4 else None
                lines.append(_line(p['sku'], rng.randint(1, 4), raw))
            local = _local('2026-03-0%d' % rng.randint(2, 6), rng.randint(8, 20), rng.choice((0, 15, 30, 45)))
            orders.append(_order('o-%d' % (n + 1), customer, local, status, lines))
        return [orders, products]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 2


# ---- ZB：重複帳號（新功能，shop/customers.py）----

ZB_REFERENCE = CUSTOMERS + r'''

def _solve(customers, skip=frozenset()):
    pool = customers if "zb_deleted" in skip else active(customers)
    groups = {}
    for customer in pool:
        email = customer["email"]
        key = email.strip().lower() if "zb_normalize" in skip else normalize_email(email)
        groups.setdefault(key, []).append(customer["id"])
    return sorted(sorted(ids) for ids in groups.values() if len(ids) > 1)


def duplicate_accounts(customers):
    return _solve(customers)
'''
_ZB_TRAP = CUSTOMERS + r'''

def duplicate_accounts(customers):
    groups = {}
    for customer in <<pool>>:
        groups.setdefault(<<key>>, []).append(customer["id"])
    return sorted(sorted(ids) for ids in groups.values() if len(ids) > 1)
'''
ZB_TRAPS = {
    'lower_strip': tk._fill(_ZB_TRAP, pool='active(customers)', key='customer["email"].strip().lower()'),
    'no_active': tk._fill(_ZB_TRAP, pool='customers', key='normalize_email(customer["email"])'),
}


def _email_variant(rng, name, domain, tricky):
    local = name
    if tricky and domain == 'gmail.com':
        roll = rng.random()
        if roll < .35:
            local = name.replace('.', '') if '.' in name else name[:1] + '.' + name[1:]
        elif roll < .7:
            local = name + '+' + rng.choice(('shop', 'promo', 'crumb'))
        else:
            domain = 'googlemail.com'
    text = f'{local}@{domain}'
    if rng.random() < .5:
        text = rng.choice((text.capitalize(), text.upper(), local.title() + '@' + domain))
    if rng.random() < .2:
        text = rng.choice((' ', '  ')) + text + rng.choice(('', ' '))
    return text


class DuplicateAccounts(RepoFamily):
    letter, name, kind, entry, target = 'ZB', 'duplicate_accounts', 'feature', 'duplicate_accounts', \
        'shop/customers.py'
    reference_target, trap_targets = ZB_REFERENCE, ZB_TRAPS
    ticket = ('Find people with more than one account',
              'Support keeps running into customers who signed up twice. Please add '
              '`duplicate_accounts(customers)` to the customers module. It should return the groups of customer '
              'ids that share an email address, only for groups with at least two accounts. Sort the ids inside '
              'each group, and sort the groups by their first id.')
    trap_breaks = {'lower_strip': {'zb_normalize'}, 'no_active': {'zb_deleted'}}
    requirements = {
        'zb_normalize': ('email 用 normalize_email 比對：Gmail 的點、+ 後綴、googlemail.com 都視為同一個信箱。',
                         '程式庫寫明 email 一律用 normalize_email 比對；同一個 Gmail 信箱換寫法註冊正是重複帳號的典型。',
                         _DOC_BASIS + 'customers.normalize_email 的 docstring（"used for every comparison"）'),
        'zb_deleted': ('已關閉的帳號（deleted_at 有值）不列入。',
                       '程式庫寫明已關閉的帳號不會出現在任何地方；客服也無法處理已關閉的帳號。',
                       _DOC_BASIS + 'customers.active 的 docstring'),
    }
    levels = _levels(('zb_normalize', 'zb_deleted'), ('lower_strip', 'no_active'))

    def _case(self, rng, plain):
        names = rng.sample(NAMES, rng.randint(3, 5))
        people = []
        for i, name in enumerate(names):
            domain = rng.choice(DOMAINS)
            copies = rng.choice((1, 2, 2, 3)) if not plain else (2 if i == 0 else 1)
            for c in range(copies):
                text = f'{name}@{domain}' if c == 0 else _email_variant(rng, name, domain, not plain)
                deleted = None if plain or rng.random() > .25 else '2026-02-%02dT09:00:00Z' % rng.randint(1, 28)
                people.append((text, deleted))
        rng.shuffle(people)
        ids = _customer_ids(rng, len(people))
        return [[{'id': i, 'email': e, 'deleted_at': d} for i, (e, d) in zip(ids, people)]]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 1 and any(c['email'] != c['email'].lower() for c in args[0])


# ---- ZC：每小時下單數（新功能，shop/reports.py）----

ZC_IMPORTS = ('from .clock import business_date, local_time\n'
              'from .orders import counts_as_revenue, is_internal, order_total')
ZC_REFERENCE = _reports(ZC_IMPORTS, r'''
def _solve(orders, day, skip=frozenset()):
    counts = [0] * 24
    for order in orders:
        if "zc_internal" not in skip and is_internal(order):
            continue
        local = local_time(order["placed_at"])
        when = local.date().isoformat() if "zc_cutoff" in skip else business_date(order["placed_at"])
        if when == day:
            counts[local.hour] += 1
    return counts


def orders_per_hour(orders, day):
    return _solve(orders, day)
''')
_ZC_TRAP = r'''
def orders_per_hour(orders, day):
    counts = [0] * 24
    for order in orders:
        <<skip>>
        local = local_time(order["placed_at"])
        if <<when>> == day:
            counts[local.hour] += 1
    return counts
'''
ZC_TRAPS = {
    'calendar_day': _reports(ZC_IMPORTS, tk._fill(_ZC_TRAP, skip='if is_internal(order):\n            continue',
                                                  when='local.date().isoformat()')),
    'with_internal': _reports(ZC_IMPORTS, tk._fill(_ZC_TRAP, skip='pass', when='business_date(order["placed_at"])')),
}


class OrdersPerHour(RepoFamily):
    letter, name, kind, entry, target = 'ZC', 'orders_per_hour', 'feature', 'orders_per_hour', 'shop/reports.py'
    reference_target, trap_targets = ZC_REFERENCE, ZC_TRAPS
    ticket = ('Orders per hour for staffing',
              'Store managers want to see how busy each hour of a day was, to plan shifts. Please add '
              '`orders_per_hour(orders, day)` to the reports. `day` is a date string like "2026-03-02". Return a '
              'list of 24 counts, where index 0 is 00:00-00:59 store time and index 23 is 23:00-23:59. Count every '
              'order customers placed, whatever its status is now.')
    trap_breaks = {'calendar_day': {'zc_cutoff'}, 'with_internal': {'zc_internal'}}
    requirements = {
        'zc_cutoff': ('「一天」是營業日（店內時間 04:00～隔天 03:59）：隔天凌晨 0～3 點的單算這天，'
                      '當天凌晨 0～3 點的單算前一天。',
                      '程式庫寫明凡是「每天」都用 business_date；店長排班看的也是營業日（夜班到 04:00）。',
                      _DOC_BASIS + 'clock.business_date 的 docstring 與 reports.daily_revenue 的寫法'),
        'zc_internal': ('排除內部測試單（customer_id 以 qa- 開頭）。',
                        '程式庫寫明報表要排除內部測試單；員工測試收銀機的單不是客人下的單。',
                        _DOC_BASIS + 'orders.is_internal 的 docstring'),
    }
    levels = _levels(('zc_cutoff', 'zc_internal'), ('calendar_day', 'with_internal'))

    def _case(self, rng, plain):
        day = '2026-03-0%d' % rng.randint(2, 6)
        base = datetime.fromisoformat(day)
        orders = []
        for n in range(rng.randint(3, 6) if plain else rng.randint(4, 9)):
            if plain:
                offset = rng.choice((0, 0, 0, 0, -1, 1))
                hour = rng.randint(7, 21)
            else:
                offset = rng.choice((0, 0, 1, 1, -1))
                hour = rng.choice((0, 1, 2, 3, 3, rng.randint(4, 23), rng.randint(4, 23), 23))
            local = base + timedelta(days=offset, hours=hour, minutes=rng.choice((0, 10, 25, 40, 55)))
            customer = 'qa-0%d' % rng.randint(1, 3) if not plain and rng.random() < .2 else \
                'c-%d' % rng.randint(101, 199)
            sku = rng.choice(PRODUCTS)['sku']
            orders.append(_order('o-%d' % (n + 1), customer, local, _status(rng, .7), [_line(sku, 1)]))
        orders.sort(key=lambda o: o['placed_at'])
        return [orders, day]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return sum(expected) >= 2


# ---- ZD：百分比折價券（新功能，shop/orders.py）----

ZD_REFERENCE = ORDERS + r'''
from .money import percent_of


def _solve(order, percent, skip=frozenset()):
    base = subtotal(order)
    if "zd_round" in skip:
        extra = round(base * percent / 100)
    elif "zd_truncate" in skip:
        extra = int(base * percent / 100)
    else:
        extra = percent_of(base, percent)
    return order["discount_cents"] + extra


def add_coupon(order, percent):
    return _solve(order, percent)
'''
_ZD_TRAP = ORDERS + r'''
<<imports>>


def add_coupon(order, percent):
    return order["discount_cents"] + <<extra>>
'''
ZD_TRAPS = {
    'builtin_round': tk._fill(_ZD_TRAP, imports='', extra='round(subtotal(order) * percent / 100)'),
    'truncate': tk._fill(_ZD_TRAP, imports='', extra='int(subtotal(order) * percent / 100)'),
    'half_even': tk._fill(_ZD_TRAP, imports='from decimal import Decimal, ROUND_HALF_EVEN',
                          extra='int((Decimal(subtotal(order)) * Decimal(str(percent)) / 100)'
                                '.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))'),
}


class AddCoupon(RepoFamily):
    letter, name, kind, entry, target = 'ZD', 'add_coupon', 'feature', 'add_coupon', 'shop/orders.py'
    reference_target, trap_targets = ZD_REFERENCE, ZD_TRAPS
    ticket = ('Percentage coupons',
              'Marketing is launching percentage-off coupons. Please add `add_coupon(order, percent)` to the orders '
              'module. It returns the order\'s new `discount_cents`: the existing discount plus `percent`% of the '
              'subtotal. `percent` is between 1 and 50 and may have one decimal place (for example 12.5).')
    trap_breaks = {'builtin_round': {'zd_round'}, 'truncate': {'zd_round'}, 'half_even': {'zd_round'}}
    probe_skips = {'zd_round': ('zd_round', 'zd_truncate')}   # 內建 round 與無條件捨去錯在不同的輸入
    requirements = {
        'zd_round': ('百分比金額用 money.percent_of 計算：Decimal 精確計算後四捨五入（half-up）到整分。',
                     '程式庫寫明每個百分比都要經過 percent_of，讓所有畫面與報表對到每一分錢；'
                     'Python 內建 round 是銀行家捨入且有浮點誤差，int() 是無條件捨去。',
                     _DOC_BASIS + 'money.percent_of 的 docstring（"Use this for every percentage"）'),
    }
    levels = _levels(('zd_round',), ('builtin_round', 'truncate', 'half_even'))

    def public_case(self, rng, level, index):
        percent = rng.choice((5, 10, 20, 25, 50))
        lines = [_line(sku, rng.randint(1, 3)) for sku in rng.sample(sorted(PRICES), rng.randint(1, 3))]
        for _ in range(8):
            if sum(l['qty'] * l['unit_cents'] for l in lines) * percent % 100 == 0:
                break
            lines[0]['qty'] += 1
        else:
            lines = [_line('BRD-001', rng.randint(1, 4) * 5)]   # 900 的倍數，五種百分比都整除
        discount = rng.choice((0, 0, 50, 100))
        return [_order('o-1', 'c-%d' % rng.randint(101, 199), _local('2026-03-03', 10), 'pending', lines, discount),
                percent]

    def random_case(self, rng, level):
        percent = rng.choice((rng.randint(1, 50), rng.randint(1, 49) + .5, 5, 15, 25, 35, 45, 12.5, 7.5))
        if rng.random() < .5:
            unit = rng.randint(30, 999)
            lines = [{'sku': rng.choice(sorted(PRICES)), 'qty': 1, 'unit_cents': unit}]
            if isinstance(percent, int) and percent % 5 == 0 and percent % 10 and rng.random() < .7:
                lines[0]['unit_cents'] = unit - unit % 20 + 10   # 5 的奇數倍 × 個位為 10 的金額 → 剛好 .5
        else:
            lines = [_line(sku, rng.randint(1, 4)) for sku in rng.sample(sorted(PRICES), rng.randint(1, 3))]
        discount = rng.choice((0, 0, 0, 30, 120))
        return [_order('o-1', 'c-%d' % rng.randint(101, 199), _local('2026-03-03', 10), 'pending', lines, discount),
                percent]


# ---- ZE：每週營收跨週錯誤（修 bug，shop/reports.py）----

ZE_IMPORTS = 'from datetime import date\n\nfrom .clock import business_date\nfrom .orders import counts_as_revenue, order_total'
ZE_ORIGINAL = _reports(ZE_IMPORTS, r'''
def weekly_revenue(orders):
    """Revenue per ISO week, e.g. {"2026-W09": 51200}."""
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        year, week, _ = date.fromisoformat(order["placed_at"][:10]).isocalendar()
        key = f"{year}-W{week:02d}"
        totals[key] = totals.get(key, 0) + order_total(order)
    return dict(sorted(totals.items()))
''')
ZE_REF_IMPORTS = ('from datetime import date\n\nfrom .clock import business_date, local_time\n'
                  'from .orders import counts_as_revenue, order_total')
ZE_REFERENCE = _reports(ZE_REF_IMPORTS, r'''
def _solve(orders, skip=frozenset()):
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        if "ze_local" in skip:
            day = order["placed_at"][:10]
        elif "ze_cutoff" in skip:
            day = local_time(order["placed_at"]).date().isoformat()
        else:
            day = business_date(order["placed_at"])
        year, week, _ = date.fromisoformat(day).isocalendar()
        key = f"{year}-W{week:02d}"
        totals[key] = totals.get(key, 0) + order_total(order)
    return dict(sorted(totals.items()))


def weekly_revenue(orders):
    return _solve(orders)
''')
_ZE_TRAP = r'''
def weekly_revenue(orders):
    totals = {}
    for order in orders:
        <<keep>>
        year, week, _ = <<day>>.isocalendar()
        key = f"{year}-W{week:02d}"
        totals[key] = totals.get(key, 0) + order_total(order)
    return dict(sorted(totals.items()))
'''
ZE_TRAPS = {
    'local_date': _reports(ZE_REF_IMPORTS, tk._fill(_ZE_TRAP, keep='if not counts_as_revenue(order):\n            continue',
                                                    day='local_time(order["placed_at"]).date()')),
    'all_statuses': _reports(ZE_REF_IMPORTS, tk._fill(_ZE_TRAP, keep='pass',
                                                      day='date.fromisoformat(business_date(order["placed_at"]))')),
}
_ZE_MONDAYS = ('2026-03-02', '2026-03-09', '2026-03-16')


class WeeklyRevenue(RepoFamily):
    letter, name, kind, entry, target = 'ZE', 'weekly_revenue', 'bugfix', 'weekly_revenue', 'shop/reports.py'
    reference_target, trap_targets, original_target = ZE_REFERENCE, ZE_TRAPS, ZE_ORIGINAL
    report = ('Our Monday-morning sales land in the previous week. A customer paid 12.00 at 07:30 on Monday '
              'March 2nd in the Daan store, but the weekly report puts it in week 9.\n'
              '`{call}` {got}; it should return {expected}.')
    trap_breaks = {'local_date': {'ze_cutoff'}, 'all_statuses': {tk.REGRESSION}}
    requirements = {
        'ze_local': ('以店內時間（台北）判斷日期，而不是 UTC 日期。', '回報本身。', 'report'),
        'ze_cutoff': ('以營業日判斷是哪一週：週一凌晨 0～3 點的單屬於週日的營業日，算前一週。',
                      '程式庫寫明凡是「每天」都用 business_date（daily_revenue 也這樣用）；'
                      '週報和日報對不起來會被財務抓到。',
                      _DOC_BASIS + 'clock.business_date 的 docstring；reports.py 已匯入 business_date，daily_revenue 就用它'),
        tk.REGRESSION: ('原本就正確的行為不能改壞（例如只算 REVENUE_STATUSES 的訂單）。', '修 bug 不可以順手改壞別的地方。',
                        'report'),
    }
    levels = _levels(('ze_cutoff',), ('local_date', 'all_statuses'), explicit=('ze_local',),
                     repro=[[_order('o-1', 'c-118', _local('2026-03-02', 7, 30), 'paid', [_line('CAK-020', 10)])]])

    def _when(self, rng, plain):
        monday = rng.choice(_ZE_MONDAYS)
        if plain:
            return _local(monday, 0) + timedelta(days=rng.randint(1, 4), hours=rng.randint(9, 20))
        kind = rng.choice(('sunday', 'night', 'morning', 'later', 'mid'))
        if kind == 'sunday':
            return _local(monday, rng.randint(-14, -1))
        if kind == 'night':
            return _local(monday, rng.randint(0, 3), rng.choice((0, 20, 45)))
        if kind == 'morning':
            return _local(monday, rng.randint(4, 7), rng.choice((0, 20, 45)))
        if kind == 'later':
            return _local(monday, rng.randint(8, 23))
        return _local(monday, 0) + timedelta(days=rng.randint(1, 5), hours=rng.randint(0, 23))

    def _case(self, rng, plain):
        orders = []
        for n in range(rng.randint(2, 4) if plain else rng.randint(2, 6)):
            status = rng.choice(REVENUE) if plain else _status(rng, .6)
            lines = [_line(sku, rng.randint(1, 3)) for sku in rng.sample(sorted(PRICES), rng.randint(1, 2))]
            orders.append(_order('o-%d' % (n + 1), 'c-%d' % rng.randint(101, 199), self._when(rng, plain), status,
                                 lines))
        return [orders]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)


# ---- ZF：登入找不到帳號（修 bug，shop/customers.py）----

ZF_ORIGINAL = CUSTOMERS + r'''

def find_customer(customers, email):
    """The id of the open account registered with `email`, or None. Used by the login page."""
    for customer in active(customers):
        if customer["email"] == email:
            return customer["id"]
    return None
'''
ZF_REFERENCE = CUSTOMERS + r'''

def _solve(customers, email, skip=frozenset()):
    def key(text):
        if "zf_case" in skip:
            return text
        if "zf_gmail" in skip:
            return text.strip().lower()
        return normalize_email(text)
    for customer in active(customers):
        if key(customer["email"]) == key(email):
            return customer["id"]
    return None


def find_customer(customers, email):
    return _solve(customers, email)
'''
_ZF_TRAP = CUSTOMERS + r'''

def find_customer(customers, email):
    for customer in <<pool>>:
        if <<stored>> == <<given>>:
            return customer["id"]
    return None
'''
ZF_TRAPS = {
    'lower_strip': tk._fill(_ZF_TRAP, pool='active(customers)', stored='customer["email"].strip().lower()',
                            given='email.strip().lower()'),
    'no_active': tk._fill(_ZF_TRAP, pool='customers', stored='normalize_email(customer["email"])',
                          given='normalize_email(email)'),
}


class FindCustomer(RepoFamily):
    letter, name, kind, entry, target = 'ZF', 'find_customer', 'bugfix', 'find_customer', 'shop/customers.py'
    reference_target, trap_targets, original_target = ZF_REFERENCE, ZF_TRAPS, ZF_ORIGINAL
    report = ('I can\'t log in. I signed up as Ann.Lee@Example.com but the login page says there is no account when '
              'I type my address in lower case.\n`{call}` {got}; it should return {expected}.')
    trap_breaks = {'lower_strip': {'zf_gmail'}, 'no_active': {tk.REGRESSION}}
    requirements = {
        'zf_case': ('email 比對不分大小寫、忽略前後空白。', '回報本身。', 'report'),
        'zf_gmail': ('email 用 normalize_email 比對：Gmail 的點、+ 後綴、googlemail.com 都是同一個信箱。',
                     '程式庫寫明 email 一律用 normalize_email 比對；根本原因是「沒有用正規化後的 email 比對」，'
                     '只補大小寫是修症狀。',
                     _DOC_BASIS + 'customers.normalize_email 的 docstring（"used for every comparison"）'),
        tk.REGRESSION: ('原本就正確的行為不能改壞（例如已關閉的帳號找不到）。', '修 bug 不可以順手改壞別的地方。', 'report'),
    }
    levels = _levels(('zf_gmail',), ('lower_strip', 'no_active'), explicit=('zf_case',),
                     repro=[[{'id': 'c-131', 'email': 'Ann.Lee@Example.com', 'deleted_at': None},
                             {'id': 'c-152', 'email': 'bo@mail.tw', 'deleted_at': None}], 'ann.lee@example.com'])

    def _case(self, rng, plain):
        names = rng.sample(NAMES, rng.randint(3, 5))
        people = []
        for name in names:
            domain = rng.choice(DOMAINS)
            people.append([f'{name}@{domain}', None, name, domain])
            if not plain and rng.random() < .3:
                people.append([_email_variant(rng, name, domain, True), '2026-01-%02dT08:00:00Z' % rng.randint(1, 28),
                               name, domain])
        if not plain and rng.random() < .3:
            people[0][1] = '2026-02-%02dT08:00:00Z' % rng.randint(1, 28)
        rng.shuffle(people)
        ids = _customer_ids(rng, len(people))
        customers = [{'id': i, 'email': p[0], 'deleted_at': p[1]} for i, p in zip(ids, people)]
        pick = rng.choice(people)
        if plain:
            query = pick[0] if rng.random() < .8 else 'nobody@example.com'
        elif rng.random() < .15:
            query = rng.choice(('nobody@example.com', 'zed@gmail.com'))
        else:
            query = _email_variant(rng, pick[2], pick[3], True)
        return [customers, query]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)


# ---- ZG：補貨清單（新功能，shop/catalog.py）----

ZG_REFERENCE = CATALOG + r'''

def _solve(stock, products, threshold, skip=frozenset()):
    pool = products if "zg_archived" in skip else listed(products)
    on_hand = {}
    for row in stock:
        sku = row["sku"] if "zg_canon" in skip else canonical_sku(row["sku"])
        on_hand[sku] = on_hand.get(sku, 0) + row["on_hand"]
    return sorted(p["sku"] for p in pool if on_hand.get(p["sku"], 0) < threshold)


def restock_list(stock, products, threshold):
    return _solve(stock, products, threshold)
'''
_ZG_TRAP = CATALOG + r'''

def restock_list(stock, products, threshold):
    on_hand = {}
    for row in stock:
        sku = <<sku>>
        on_hand[sku] = on_hand.get(sku, 0) + row["on_hand"]
    return sorted(p["sku"] for p in <<pool>> if on_hand.get(p["sku"], 0) < threshold)
'''
ZG_TRAPS = {
    'all_products': tk._fill(_ZG_TRAP, sku='canonical_sku(row["sku"])', pool='products'),
    'raw_sku': tk._fill(_ZG_TRAP, sku='row["sku"]', pool='listed(products)'),
}


class RestockList(RepoFamily):
    letter, name, kind, entry, target = 'ZG', 'restock_list', 'feature', 'restock_list', 'shop/catalog.py'
    reference_target, trap_targets = ZG_REFERENCE, ZG_TRAPS
    ticket = ('Morning restock list',
              'The warehouse wants a list of what to restock each morning. Please add '
              '`restock_list(stock, products, threshold)` to the catalog module. `stock` is the scanner export: a '
              'list of rows like {"sku": ..., "on_hand": 4}, one per shelf, so a product can have several rows. '
              'Return the SKUs of the products whose total on hand is below `threshold`, sorted. A product with no '
              'rows has 0 on hand.')
    trap_breaks = {'all_products': {'zg_archived'}, 'raw_sku': {'zg_canon'}}
    requirements = {
        'zg_archived': ('已下架（archived）的商品不列入補貨清單。',
                        '程式庫寫明下架商品除了舊訂單之外到處都要隱藏；也不會有人去補已經不賣的東西。',
                        _DOC_BASIS + 'catalog.listed 的 docstring'),
        'zg_canon': ('掃描器匯出的 SKU 要先經 canonical_sku 才對得上商品（寫法不同的列要加總在同一個商品）。',
                     '程式庫寫明倉庫掃描器的 SKU 是人工輸入、要正規化後才能比對。',
                     _DOC_BASIS + 'catalog.canonical_sku 的 docstring 與 find_product 的寫法'),
    }
    levels = _levels(('zg_archived', 'zg_canon'), ('all_products', 'raw_sku'))

    def _case(self, rng, plain):
        products = rng.sample(PRODUCTS, rng.randint(3, 5))
        if not plain:
            products += rng.sample(ARCHIVED, rng.randint(0, 2))
            rng.shuffle(products)
        threshold = rng.randint(3, 10)
        stock = []
        for p in products:
            for _ in range(rng.choice((0, 1, 1, 2) if plain else (0, 1, 2, 2))):
                raw = _raw_sku(rng, p['sku']) if not plain and rng.random() < .45 else p['sku']
                stock.append({'sku': raw, 'on_hand': rng.randint(0, threshold + 2)})
        rng.shuffle(stock)
        return [stock, products, threshold]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        listed_skus = {r['sku'] for r in args[0]}
        return 1 <= len(expected) < len(args[1]) and any(p['sku'] not in listed_skus for p in args[1])


# ---- ZH：取消的訂單讓客人上榜（修 bug，shop/reports.py）----

ZH_IMPORTS = 'from .clock import business_date\nfrom .orders import counts_as_revenue, is_internal, order_total'
ZH_ORIGINAL = _reports(ZH_IMPORTS, r'''
def top_customers(orders, n):
    """The `n` biggest spenders as [[customer_id, cents], ...]; ties go to the smaller id."""
    spent = {}
    for order in orders:
        if is_internal(order):
            continue
        spent[order["customer_id"]] = spent.get(order["customer_id"], 0) + order_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]
''')
ZH_REFERENCE = _reports(ZH_IMPORTS, r'''
def _solve(orders, n, skip=frozenset()):
    spent = {}
    for order in orders:
        if is_internal(order):
            continue
        if order["status"] == "cancelled" and "zh_cancelled" not in skip:
            continue
        if order["status"] in ("pending", "refunded") and "zh_status" not in skip:
            continue
        spent[order["customer_id"]] = spent.get(order["customer_id"], 0) + order_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]


def top_customers(orders, n):
    return _solve(orders, n)
''')
_ZH_TRAP = r'''
def top_customers(orders, n):
    spent = {}
    for order in orders:
        if <<skip>>:
            continue
        spent[order["customer_id"]] = spent.get(order["customer_id"], 0) + order_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]
'''
ZH_TRAPS = {
    'drop_cancelled': _reports(ZH_IMPORTS, tk._fill(_ZH_TRAP, skip='is_internal(order) or order["status"] == "cancelled"')),
    'no_internal': _reports(ZH_IMPORTS, tk._fill(_ZH_TRAP, skip='order["status"] not in ("paid", "fulfilled")')),
}


class TopCustomers(RepoFamily):
    letter, name, kind, entry, target = 'ZH', 'top_customers', 'bugfix', 'top_customers', 'shop/reports.py'
    reference_target, trap_targets, original_target = ZH_REFERENCE, ZH_TRAPS, ZH_ORIGINAL
    report = ('The "top customers" list on the dashboard shows c-202 first, but their only order was cancelled the '
              'same day.\n`{call}` {got}; it should return {expected}.')
    trap_breaks = {'drop_cancelled': {'zh_status'}, 'no_internal': {tk.REGRESSION}}
    requirements = {
        'zh_cancelled': ('已取消的訂單不算消費。', '回報本身。', 'report'),
        'zh_status': ('只算 REVENUE_STATUSES（paid、fulfilled）的訂單：尚未付款（pending）與已退款（refunded）也不算消費。',
                      '根本原因是沒有用營收的定義；程式庫寫明只有這些狀態算營收，daily_revenue 用 counts_as_revenue。'
                      '只排除 cancelled 是修症狀，退款的客人一樣會上榜。',
                      _DOC_BASIS + 'orders.REVENUE_STATUSES 的註解；reports.py 已匯入 counts_as_revenue'),
        tk.REGRESSION: ('原本就正確的行為不能改壞（例如排除內部測試單、同額時 id 小的在前）。', '修 bug 不可以順手改壞別的地方。',
                        'report'),
    }
    levels = _levels(('zh_status',), ('drop_cancelled', 'no_internal'), explicit=('zh_cancelled',),
                     repro=[[_order('o-1', 'c-201', _local('2026-03-03', 9), 'paid', [_line('BRD-001', 5)]),
                             _order('o-2', 'c-202', _local('2026-03-03', 10), 'cancelled', [_line('CAK-020', 20)]),
                             _order('o-3', 'c-203', _local('2026-03-03', 11), 'fulfilled', [_line('DRK-030', 5)])],
                            2])

    def _case(self, rng, plain):
        customers = _customer_ids(rng, rng.randint(3, 5))
        orders = []
        for n in range(rng.randint(3, 5) if plain else rng.randint(3, 8)):
            customer = 'qa-0%d' % rng.randint(1, 3) if not plain and rng.random() < .15 else rng.choice(customers)
            status = rng.choice(REVENUE) if plain else _status(rng, .5)
            lines = [_line(sku, rng.randint(1, 6)) for sku in rng.sample(sorted(PRICES), rng.randint(1, 2))]
            orders.append(_order('o-%d' % (n + 1), customer, _local('2026-03-0%d' % rng.randint(2, 6),
                                                                    rng.randint(8, 20)), status, lines))
        return [orders, rng.randint(1, 4)]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 2


REPO_FAMILIES = {f.letter: f for f in (RevenueByCategory(), DuplicateAccounts(), OrdersPerHour(), AddCoupon(),
                                       WeeklyRevenue(), FindCustomer(), RestockList(), TopCustomers())}


def solution_code(task, code):
    """作答（目標檔案的新內容）→ 可在隔離執行器跑的程式；語法錯就原樣交回，讓執行器回 syntax_error。"""
    repo = task['repo']
    try:
        compile(code, repo['target'], 'exec', dont_inherit=True)
    except (SyntaxError, ValueError):
        return code
    return repo_program(repo['files'], repo['target'], code, task['entry_point'])


def describe(task):
    return json.dumps({'id': task['id'], 'target': task['repo']['target']}, ensure_ascii=False)
