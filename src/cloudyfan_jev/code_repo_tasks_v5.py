"""第五版盲測改程式題：更多「慣例只寫在程式碼裡」的程式庫情境題（ZI～ZP）。設計見
plans/mrc-blind-validation-experiment-2026-11-24.md §3.11。

第四版（``code_repo_tasks``）的 48 題裡，便宜首答唯一漏掉的是 ZB5：產品規則和工單的直覺讀法相反、
而且只寫在輔助函式的 docstring。第五版專門加大這種題目的樣本。程式庫沿用同一家麵包店，再加上幾條同類型的規則：

- 禮券不是營收（``orders.revenue_total``）；作廢的品項從沒賣出（``orders.live_lines``）。
- 員工與「guest」散客不是顧客（``orders.is_staff``、``orders.is_guest``）。
- 合併過的帳號要歸到保留的帳號（``customers.resolve_id``）；合併掉的帳號和已關閉的一樣不出現。
- 行銷信只寄給同意且信箱已驗證的人（``customers.can_email``）。
- 平均一律用 ``money.average_cents``（四捨五入 half-up）。
- 預購按取貨日入帳；每天、每週、每月一律用營業日（``orders.revenue_day``、``clock.business_date``）。

題目機制、公平性與評分都沿用第四版（``RepoFamily``、``code_tickets`` 的產生器）；兩級一樣是
第 3 級多附 CONVENTIONS.md、第 5 級只看得到 docstring。
"""
from datetime import date, datetime, timedelta

from .code_repo_tasks import (ARCHIVED, BASES, DOMAINS, INIT, NAMES, PRICES, PRODUCTS, REVENUE, RepoFamily, _DOC_BASIS,
                              _customer_ids, _levels, _local, _raw_sku, _status, _utc)
from . import code_tickets as tk

REPO_VERSION = 'cloudyfan-code-tasks-v5'
BASE = 'v5'

# ---- 程式庫（第四版加上新規則）----

MONEY = r'''"""Money helpers. Every amount in the shop is an int number of cents."""
from decimal import Decimal, ROUND_HALF_UP


def percent_of(cents, percent):
    """`percent`% of `cents`, rounded half-up to a whole cent.

    Use this for every percentage (discounts, tax, commission) so that all
    screens and reports agree to the cent. `percent` may have decimals (12.5).
    """
    value = Decimal(cents) * Decimal(str(percent)) / Decimal(100)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def average_cents(total, count):
    """The average of `count` amounts that add up to `total` cents, rounded half-up
    to a whole cent. Use this for every average: round() rounds halves to even and
    // always rounds down, so screens would disagree by a cent."""
    value = Decimal(total) / Decimal(count)
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
    March 2nd is booked on March 1st. Every per-day, per-week and per-month
    number groups by this date.
    """
    shifted = local_time(ts) - timedelta(hours=BUSINESS_DAY_CUTOFF_HOUR)
    return shifted.date().isoformat()
'''

CATALOG = r'''"""Product catalog. A product looks like
{"sku": "BRD-001", "name": "Country loaf", "category": "bread", "archived": False}.
Gift cards are sold like products ("GFT-500", category "gift card").
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

CUSTOMERS = r'''"""Customers look like
{"id": "c-117", "email": "Ann@Example.com", "deleted_at": None, "merged_into": None,
 "marketing_opt_in": True, "email_verified_at": "2026-01-05T10:00:00Z"}.

`deleted_at` is set (an ISO timestamp) when a customer closes their account;
the record is kept for accounting. `merged_into` is set when support merges a
duplicate account into another one.
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
    """Customers whose account is open. Closed and merged-away accounts never show up in the product."""
    return [c for c in customers if c["deleted_at"] is None and c["merged_into"] is None]


def resolve_id(customers, customer_id):
    """The id that owns `customer_id`'s history now. Orders keep the id they were
    placed with even after support merges that account into another one, so
    per-customer numbers group by this id."""
    by_id = {c["id"]: c for c in customers}
    while customer_id in by_id and by_id[customer_id]["merged_into"]:
        customer_id = by_id[customer_id]["merged_into"]
    return customer_id


def can_email(customer):
    """Whether marketing email may go to this customer: an open account that opted in
    and has a verified address. Mail to unverified addresses bounces, and bounces
    get our sending domain blocklisted."""
    return (customer["deleted_at"] is None and customer["merged_into"] is None
            and customer["marketing_opt_in"] and customer["email_verified_at"] is not None)
'''

ORDERS = r'''"""Orders look like
{"id": "o-1", "customer_id": "c-117", "placed_at": "2026-03-01T06:05:00Z",
 "status": "paid", "discount_cents": 0, "pickup_on": None,
 "lines": [{"sku": "BRD-001", "qty": 2, "unit_cents": 350, "void": False}]}

Status moves pending -> paid -> fulfilled, or ends as cancelled or refunded.
`pickup_on` is the store-local pickup date ("2026-03-02") of a preorder, such as
a birthday cake ordered days ahead; it is None for sales over the counter.
"""
from .catalog import canonical_sku
from .clock import business_date

REVENUE_STATUSES = ("paid", "fulfilled")  # only these count as revenue
GIFT_CARD_PREFIX = "GFT-"


def is_internal(order):
    """Orders placed by staff accounts ("qa-...") to test the tills. Reports leave them out."""
    return order["customer_id"].startswith("qa-")


def is_staff(order):
    """Staff meals and drinks, rung up under the staff member's id ("staff-...").
    They are real sales and count as revenue, but staff are not customers:
    numbers about customers leave these orders out."""
    return order["customer_id"].startswith("staff-")


def is_guest(order):
    """Walk-in sales without a loyalty card are all booked to the id "guest". That
    id stands for many different people, so it never counts as a customer."""
    return order["customer_id"] == "guest"


def live_lines(order):
    """The lines that were actually sold. A cashier who rings something up by
    mistake voids the line ("void": True); it stays on the order for the audit
    trail but was never sold, so anything that counts items or money uses these
    lines only."""
    return [line for line in order["lines"] if not line["void"]]


def is_gift_card(line):
    """Gift card sales are a liability until the card is spent, so they are never revenue."""
    return canonical_sku(line["sku"]).startswith(GIFT_CARD_PREFIX)


def subtotal(order):
    return sum(line["qty"] * line["unit_cents"] for line in live_lines(order))


def order_total(order):
    """What the customer pays: subtotal minus discount, never below zero."""
    return max(0, subtotal(order) - order["discount_cents"])


def revenue_total(order):
    """What the order adds to revenue: like order_total but without gift card
    lines. Every revenue number uses this, never order_total."""
    goods = sum(line["qty"] * line["unit_cents"] for line in live_lines(order) if not is_gift_card(line))
    return max(0, goods - order["discount_cents"])


def counts_as_revenue(order):
    return order["status"] in REVENUE_STATUSES and not is_internal(order)


def revenue_day(order):
    """The business day an order's revenue is booked on: the pickup day for a
    preorder, otherwise the business day it was placed."""
    return order["pickup_on"] or business_date(order["placed_at"])
'''

REPORTS_HEAD = r'''"""Numbers for the finance and operations teams."""
<<imports>>


def daily_revenue(orders):
    """Revenue per business day, e.g. {"2026-03-01": 12850}."""
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        day = revenue_day(order)
        totals[day] = totals.get(day, 0) + revenue_total(order)
    return dict(sorted(totals.items()))
'''
REPORTS_IMPORTS = 'from .orders import counts_as_revenue, revenue_day, revenue_total'
REPORTS = tk._fill(REPORTS_HEAD, imports=REPORTS_IMPORTS)

CONVENTIONS = r'''# Team conventions

- Money is always an int number of cents. Every percentage goes through `money.percent_of` and every
  average through `money.average_cents` (both round half-up).
- Timestamps are UTC. Anything per day, week or month uses the business day from `clock.business_date`
  (04:00-03:59 store time), never the UTC date. Revenue is booked on `orders.revenue_day`
  (preorders on their pickup day).
- Revenue counts only orders whose status is in `orders.REVENUE_STATUSES`, and only
  `orders.revenue_total` of them: gift cards are not revenue.
- Voided lines were never sold: count items and money through `orders.live_lines`.
- Reports leave out internal test orders (`orders.is_internal`).
- Staff (`orders.is_staff`) and walk-in "guest" sales (`orders.is_guest`) are not customers: leave them
  out of anything about customers.
- Per-customer numbers group by `customers.resolve_id`, so a merged account counts as one customer.
- Compare SKUs only through `catalog.canonical_sku`.
- Compare emails only through `customers.normalize_email`.
- Closed and merged-away accounts never show up anywhere; use `customers.active`.
- Marketing email goes only to customers for whom `customers.can_email` is true.
- Archived products are hidden everywhere except on old orders; use `catalog.listed`.
'''

BASE_FILES = {'shop/__init__.py': INIT, 'shop/money.py': MONEY, 'shop/clock.py': CLOCK, 'shop/catalog.py': CATALOG,
              'shop/customers.py': CUSTOMERS, 'shop/orders.py': ORDERS, 'shop/reports.py': REPORTS}
BASES[BASE] = (BASE_FILES, CONVENTIONS)

# ---- 產生測試資料用的小工具 ----

REGULAR = [p['sku'] for p in PRODUCTS]
GIFTS = [{'sku': 'GFT-500', 'name': 'Gift card NT$500', 'category': 'gift card', 'archived': False},
         {'sku': 'GFT-1000', 'name': 'Gift card NT$1000', 'category': 'gift card', 'archived': False}]
ALL_PRODUCTS = PRODUCTS + ARCHIVED + GIFTS
PRICES5 = {**PRICES, 'GFT-500': 50000, 'GFT-1000': 100000}
DAYS = ('2026-03-02', '2026-03-03', '2026-03-04', '2026-03-05', '2026-03-06')
VERIFIED = '2026-01-05T10:00:00Z'


def _line(sku, qty, raw=None, void=False):
    return {'sku': raw or sku, 'qty': qty, 'unit_cents': PRICES5[sku], 'void': void}


def _order(oid, customer, local, status, lines, discount=0, pickup=None):
    return {'id': oid, 'customer_id': customer, 'placed_at': _utc(local), 'status': status,
            'discount_cents': discount, 'pickup_on': pickup, 'lines': lines}


def _customer(cid, email, deleted=None, merged=None, opt_in=True, verified=VERIFIED):
    return {'id': cid, 'email': email, 'deleted_at': deleted, 'merged_into': merged,
            'marketing_opt_in': opt_in, 'email_verified_at': verified}


def _emails(rng, count):
    return ['%s@%s' % (n, rng.choice(DOMAINS)) for n in rng.sample(NAMES, count)]


def _stamp(rng):
    return '2026-02-%02dT09:00:00Z' % rng.randint(1, 28)


def _accounts(rng, count, merges, closed_share):
    """顧客清單：最多 ``merges`` 個帳號合併進另一個仍開著的帳號；保留的帳號不會被關閉或合併。"""
    ids = _customer_ids(rng, count)
    merged, survivors = {}, set()
    for _ in range(merges):
        free = [i for i in ids if i not in merged and i not in survivors]
        if len(free) < 2:
            break
        old, keep = rng.sample(free, 2)
        merged[old] = keep
        survivors.add(keep)
    people = []
    for cid, email in zip(ids, _emails(rng, count)):
        closed = cid not in merged and cid not in survivors and rng.random() < closed_share
        people.append(_customer(cid, email, _stamp(rng) if closed else None, merged.get(cid)))
    return people


def _sold_lines(rng, skus, count, max_qty, raw_share, void_share):
    """一張單的品項：部分 SKU 照收銀員打的原樣；有時某個品項先多打了數量、作廢後重打（作廢的那行排在前面）。"""
    lines = []
    for sku in rng.sample(skus, count):
        raw = _raw_sku(rng, sku) if rng.random() < raw_share else None
        lines.append(_line(sku, rng.randint(1, max_qty), raw))
    if rng.random() < void_share:
        index = rng.randrange(len(lines))
        sku = _canonical(lines[index]['sku'])
        lines.insert(index, _line(sku, lines[index]['qty'] + rng.randint(3, 10), _raw_sku(rng, sku), void=True))
    return lines


def _canonical(raw):
    return '-'.join(raw.strip().upper().replace('_', ' ').replace('-', ' ').split())


def _biz_day(ts):
    return (datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ') + timedelta(hours=4)).date().isoformat()


def _reports(imports, body):
    return tk._fill(REPORTS_HEAD, imports=imports) + '\n\n' + body.strip('\n') + '\n'


class RepoFamilyV5(RepoFamily):
    base = BASE


# ---- ZI：每日平均客單價（新功能，shop/reports.py）----

ZI_IMPORTS = 'from .money import average_cents\nfrom .orders import counts_as_revenue, order_total, revenue_day, revenue_total'
ZI_REFERENCE = _reports(ZI_IMPORTS, r'''
def _solve(orders, skip=frozenset()):
    totals, counts = {}, {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        day = revenue_day(order)
        totals[day] = totals.get(day, 0) + (order_total(order) if "zi_gift" in skip else revenue_total(order))
        counts[day] = counts.get(day, 0) + 1
    if "zi_round" in skip:
        return {day: round(totals[day] / counts[day]) for day in sorted(totals)}
    return {day: average_cents(totals[day], counts[day]) for day in sorted(totals)}


def average_order_value(orders):
    return _solve(orders)
''')
_ZI_TRAP = r'''
def average_order_value(orders):
    totals, counts = {}, {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        day = revenue_day(order)
        totals[day] = totals.get(day, 0) + <<amount>>(order)
        counts[day] = counts.get(day, 0) + 1
    return {day: <<average>> for day in sorted(totals)}
'''
ZI_TRAPS = {
    'gift_in': _reports(ZI_IMPORTS, tk._fill(_ZI_TRAP, amount='order_total',
                                             average='average_cents(totals[day], counts[day])')),
    'round_builtin': _reports(ZI_IMPORTS, tk._fill(_ZI_TRAP, amount='revenue_total',
                                                   average='round(totals[day] / counts[day])')),
}


class AverageOrderValue(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZI', 'average_order_value', 'feature', 'average_order_value', \
        'shop/reports.py'
    reference_target, trap_targets = ZI_REFERENCE, ZI_TRAPS
    ticket = ('Average order value per day',
              'Finance wants to watch the average order value. Please add `average_order_value(orders)` to the '
              'reports module: for each business day, the average revenue per order, over the orders that count '
              'toward that day\'s revenue. Business days work the same way as in `daily_revenue`. Amounts are in '
              'cents, rounded to a whole cent.')
    trap_breaks = {'gift_in': {'zi_gift'}, 'round_builtin': {'zi_round'}}
    requirements = {
        'zi_gift': ('營收用 revenue_total：訂單裡的禮券品項不算營收。',
                    '程式庫寫明禮券在被使用前是負債、從不算營收，所有營收數字都用 revenue_total、不用 order_total。',
                    _DOC_BASIS + 'orders.revenue_total 與 orders.is_gift_card 的 docstring；daily_revenue 用 revenue_total'),
        'zi_round': ('平均用 money.average_cents（剛好 .5 時進位），不用 round()（剛好 .5 時取偶數）。',
                     '程式庫寫明每個平均都用 average_cents，才不會和其他畫面差一分錢。',
                     _DOC_BASIS + 'money.average_cents 的 docstring（"Use this for every average"）'),
    }
    levels = _levels(('zi_gift', 'zi_round'), ('gift_in', 'round_builtin'))

    def _case(self, rng, plain):
        customers = _customer_ids(rng, 6)
        orders = []
        for day in rng.sample(DAYS, 2 if plain else rng.randint(1, 3)):
            for _ in range(rng.choice((2, 3)) if plain else rng.choice((2, 2, 3, 4, 5))):
                lines = [_line(sku, rng.randint(1, 4)) for sku in rng.sample(REGULAR, rng.randint(1, 2 if plain else 3))]
                if not plain and rng.random() < .3:
                    lines.append(_line(rng.choice(GIFTS)['sku'], 1))
                goods = sum(line['qty'] * line['unit_cents'] for line in lines if not line['sku'].startswith('GFT'))
                discount = rng.choice((0, 0, 0, 50, 100)) if goods > 300 else 0
                orders.append(_order('o-%d' % (len(orders) + 1), rng.choice(customers),
                                     _local(day, rng.randint(8, 20), rng.randint(0, 59)), rng.choice(REVENUE),
                                     lines, discount))
        rng.shuffle(orders)
        return [orders]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        # 至少一天的平均餘數超過一半，所以只取整數部分（//）在公開範例就會錯；剛好 .5 的情況留給隱藏測試。
        totals, counts = {}, {}
        for order in args[0]:
            day = _biz_day(order['placed_at'])
            totals[day] = totals.get(day, 0) + max(0, sum(l['qty'] * l['unit_cents'] for l in order['lines'])
                                                   - order['discount_cents'])
            counts[day] = counts.get(day, 0) + 1
        return any(2 * (totals[d] % counts[d]) > counts[d] for d in totals)


# ---- ZJ：每天有幾位顧客（新功能，shop/reports.py）----

ZJ_IMPORTS = 'from .clock import business_date\nfrom .orders import counts_as_revenue, is_guest, is_staff, revenue_day, revenue_total'
ZJ_REFERENCE = _reports(ZJ_IMPORTS, r'''
def _solve(orders, skip=frozenset()):
    seen = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        if is_guest(order) and "zj_guest" not in skip:
            continue
        if is_staff(order) and "zj_staff" not in skip:
            continue
        seen.setdefault(business_date(order["placed_at"]), set()).add(order["customer_id"])
    return {day: len(ids) for day, ids in sorted(seen.items())}


def customers_per_day(orders):
    return _solve(orders)
''')
_ZJ_TRAP = r'''
def customers_per_day(orders):
    seen = {}
    for order in orders:
        if not counts_as_revenue(order) or <<skip>>:
            continue
        seen.setdefault(business_date(order["placed_at"]), set()).add(order["customer_id"])
    return {day: len(ids) for day, ids in sorted(seen.items())}
'''
ZJ_TRAPS = {
    'count_guest': _reports(ZJ_IMPORTS, tk._fill(_ZJ_TRAP, skip='is_staff(order)')),
    'count_staff': _reports(ZJ_IMPORTS, tk._fill(_ZJ_TRAP, skip='is_guest(order)')),
}


class CustomersPerDay(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZJ', 'customers_per_day', 'feature', 'customers_per_day', \
        'shop/reports.py'
    reference_target, trap_targets = ZJ_REFERENCE, ZJ_TRAPS
    ticket = ('How many customers came in each day',
              'The store managers want to see foot traffic next to revenue. Please add `customers_per_day(orders)` '
              'to the reports module: for each business day, how many different customers bought something that '
              'day, e.g. {"2026-03-02": 41}. Business days work the same way as in `daily_revenue`. Leave out days '
              'when no customer bought anything.')
    trap_breaks = {'count_guest': {'zj_guest'}, 'count_staff': {'zj_staff'}}
    requirements = {
        'zj_guest': ('「guest」不算顧客：沒有會員卡的散客都記在 guest 這個 id 底下。',
                     '程式庫寫明 guest 代表很多不同的人，從不算一位顧客。',
                     _DOC_BASIS + 'orders.is_guest 的 docstring'),
        'zj_staff': ('員工（staff- 開頭）的訂單不算顧客。',
                     '程式庫寫明員工餐算營收，但員工不是顧客，跟顧客有關的數字都排除。',
                     _DOC_BASIS + 'orders.is_staff 的 docstring'),
    }
    levels = _levels(('zj_guest', 'zj_staff'), ('count_guest', 'count_staff'))

    def _case(self, rng, plain):
        customers = _customer_ids(rng, rng.randint(3, 6))
        orders = []
        for day in rng.sample(DAYS, 2 if plain else rng.randint(1, 3)):
            ids = [rng.choice(customers) for _ in range(rng.randint(2, 3) if plain else rng.randint(2, 6))]
            if not plain:
                ids += [rng.choice(('guest', 'staff-0%d' % rng.randint(1, 3))) for _ in range(rng.randint(0, 3))]
            for customer in ids:
                lines = [_line(sku, rng.randint(1, 3)) for sku in rng.sample(REGULAR, 1 if plain else rng.randint(1, 2))]
                orders.append(_order('o-%d' % (len(orders) + 1), customer,
                                     _local(day, rng.randint(8, 20), rng.randint(0, 59)), rng.choice(REVENUE), lines))
        rng.shuffle(orders)
        return [orders]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(args[0]) > sum(expected.values())   # 範例裡有人同一天買兩次，看得出是「不同的」顧客


# ---- ZK：每位顧客累計消費（新功能，shop/reports.py）----

ZK_IMPORTS = 'from .customers import active, resolve_id\nfrom .orders import counts_as_revenue, revenue_day, revenue_total'
ZK_REFERENCE = _reports(ZK_IMPORTS, r'''
def _solve(orders, customers, skip=frozenset()):
    if "zk_merge" in skip:
        spent = {c["id"]: 0 for c in customers if c["deleted_at"] is None}
    else:
        spent = {c["id"]: 0 for c in active(customers)}
    for order in orders:
        if "zk_revenue" not in skip and not counts_as_revenue(order):
            continue
        owner = order["customer_id"] if "zk_merge" in skip else resolve_id(customers, order["customer_id"])
        if owner in spent:
            spent[owner] += revenue_total(order)
    return dict(sorted(spent.items()))


def lifetime_spend(orders, customers):
    return _solve(orders, customers)
''')
_ZK_TRAP = r'''
def lifetime_spend(orders, customers):
    spent = {c["id"]: 0 for c in <<pool>>}
    for order in orders:
        <<filter>>
        owner = <<owner>>
        if owner in spent:
            spent[owner] += revenue_total(order)
    return dict(sorted(spent.items()))
'''
ZK_TRAPS = {
    'raw_ids': _reports(ZK_IMPORTS, tk._fill(
        _ZK_TRAP, pool='customers if c["deleted_at"] is None',
        filter='if not counts_as_revenue(order):\n            continue', owner='order["customer_id"]')),
    'all_statuses': _reports(ZK_IMPORTS, tk._fill(
        _ZK_TRAP, pool='active(customers)', filter='pass', owner='resolve_id(customers, order["customer_id"])')),
}


class LifetimeSpend(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZK', 'lifetime_spend', 'feature', 'lifetime_spend', 'shop/reports.py'
    reference_target, trap_targets = ZK_REFERENCE, ZK_TRAPS
    ticket = ('Lifetime spend for the loyalty program',
              'The loyalty program gives perks by how much people have spent with us. Please add '
              '`lifetime_spend(orders, customers)` to the reports module. It returns {customer_id: cents} with one '
              'entry for every customer whose account is open, including customers who have not spent anything '
              'yet.')
    trap_breaks = {'raw_ids': {'zk_merge'}, 'all_statuses': {'zk_revenue'}}
    requirements = {
        'zk_merge': ('合併過的帳號歸到保留的帳號：用 resolve_id 加總，合併掉的帳號不出現在結果裡。',
                     '程式庫寫明訂單保留下單時的 id，帳號合併後每位顧客的數字要用 resolve_id 分組；合併掉的帳號和關閉的一樣不出現。',
                     _DOC_BASIS + 'customers.resolve_id 與 customers.active 的 docstring'),
        'zk_revenue': ('只算 REVENUE_STATUSES（paid、fulfilled）的訂單；未付款、取消、退款都不算消費。',
                       '程式庫寫明只有這些狀態算營收；daily_revenue 用 counts_as_revenue。',
                       _DOC_BASIS + 'orders.REVENUE_STATUSES 的註解；daily_revenue 的寫法'),
    }
    levels = _levels(('zk_merge', 'zk_revenue'), ('raw_ids', 'all_statuses'))

    def _case(self, rng, plain):
        customers = _accounts(rng, rng.randint(3, 4) if plain else rng.randint(4, 7), 0 if plain else rng.randint(1, 2), .2)
        orders = []
        for n in range(rng.randint(3, 4) if plain else rng.randint(3, 9)):
            lines = [_line(sku, rng.randint(1, 4)) for sku in rng.sample(REGULAR, 1 if plain else rng.randint(1, 2))]
            orders.append(_order('o-%d' % (n + 1), rng.choice(customers)['id'],
                                 _local(rng.choice(DAYS), rng.randint(8, 20)),
                                 rng.choice(REVENUE) if plain else _status(rng, .6), lines))
        return [orders, customers]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return sum(1 for v in expected.values() if v) >= 2 and 0 in expected.values()


# ---- ZL：電子報名單（新功能，shop/customers.py）----

ZL_REFERENCE = CUSTOMERS + r'''

def _solve(customers, skip=frozenset()):
    out = []
    for c in customers:
        if not c["marketing_opt_in"]:
            continue
        if "zl_verified" not in skip and c["email_verified_at"] is None:
            continue
        if "zl_active" not in skip and (c["deleted_at"] is not None or c["merged_into"] is not None):
            continue
        out.append(c["id"])
    return sorted(out)


def newsletter_recipients(customers):
    return _solve(customers)
'''
_ZL_TRAP = CUSTOMERS + r'''

def newsletter_recipients(customers):
    return sorted(c["id"] for c in customers if c["marketing_opt_in"] and <<rule>>)
'''
ZL_TRAPS = {
    'no_verified': tk._fill(_ZL_TRAP, rule='c in active(customers)'),
    'no_active': tk._fill(_ZL_TRAP, rule='c["email_verified_at"] is not None'),
    'closed_only': tk._fill(_ZL_TRAP, rule='c["deleted_at"] is None and c["email_verified_at"] is not None'),
}


class NewsletterRecipients(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZL', 'newsletter_recipients', 'feature', 'newsletter_recipients', \
        'shop/customers.py'
    reference_target, trap_targets = ZL_REFERENCE, ZL_TRAPS
    ticket = ('Recipient list for the spring newsletter',
              'The spring newsletter goes out next week. Please add `newsletter_recipients(customers)` to the '
              'customers module. It returns the ids of the customers who said yes to marketing email '
              '(`marketing_opt_in`), sorted.')
    trap_breaks = {'no_verified': {'zl_verified'}, 'no_active': {'zl_active'}, 'closed_only': {'zl_active'}}
    requirements = {
        'zl_verified': ('信箱沒驗證（email_verified_at 是 None）的不寄。',
                        '程式庫寫明行銷信只寄給信箱已驗證的人：寄到未驗證的地址會退信，退信會讓寄件網域被封鎖。',
                        _DOC_BASIS + 'customers.can_email 的 docstring'),
        'zl_active': ('已關閉或已合併掉的帳號不寄。',
                      '程式庫寫明關閉與合併掉的帳號不出現在任何地方，can_email 也排除它們。',
                      _DOC_BASIS + 'customers.active 與 customers.can_email 的 docstring'),
    }
    levels = _levels(('zl_verified', 'zl_active'), ('no_verified', 'no_active', 'closed_only'))

    def _case(self, rng, plain):
        people = _accounts(rng, rng.randint(4, 5) if plain else rng.randint(5, 9), 0 if plain else rng.randint(0, 2),
                           0 if plain else .2)
        for person in people:
            person['marketing_opt_in'] = rng.random() < .65
            if not plain and rng.random() < .3:
                person['email_verified_at'] = None
        return [people]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return 1 <= len(expected) < len(args[0])


# ---- ZM：各商品賣出件數（新功能，shop/reports.py）----

ZM_IMPORTS = 'from .catalog import canonical_sku\nfrom .orders import counts_as_revenue, live_lines, revenue_day, revenue_total'
ZM_REFERENCE = _reports(ZM_IMPORTS, r'''
def _solve(orders, skip=frozenset()):
    units = {}
    for order in orders:
        for line in (order["lines"] if "zm_void" in skip else live_lines(order)):
            sku = line["sku"] if "zm_sku" in skip else canonical_sku(line["sku"])
            units[sku] = units.get(sku, 0) + line["qty"]
    return dict(sorted(units.items()))


def units_sold(orders):
    return _solve(orders)
''')
_ZM_TRAP = r'''
def units_sold(orders):
    units = {}
    for order in orders:
        for line in <<lines>>:
            sku = <<sku>>
            units[sku] = units.get(sku, 0) + line["qty"]
    return dict(sorted(units.items()))
'''
ZM_TRAPS = {
    'void_counted': _reports(ZM_IMPORTS, tk._fill(_ZM_TRAP, lines='order["lines"]', sku='canonical_sku(line["sku"])')),
    'raw_sku': _reports(ZM_IMPORTS, tk._fill(_ZM_TRAP, lines='live_lines(order)', sku='line["sku"]')),
}


class UnitsSold(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZM', 'units_sold', 'feature', 'units_sold', 'shop/reports.py'
    reference_target, trap_targets = ZM_REFERENCE, ZM_TRAPS
    ticket = ('Units sold per product',
              'The bakers plan tomorrow\'s batches from what sold today. Please add `units_sold(orders)` to the '
              'reports module: how many units of each product were sold in the given orders, keyed by SKU, '
              'e.g. {"BRD-001": 37, "PST-010": 120}.')
    trap_breaks = {'void_counted': {'zm_void'}, 'raw_sku': {'zm_sku'}}
    requirements = {
        'zm_void': ('作廢的品項（void 為 True）不算賣出：用 live_lines。',
                    '程式庫寫明作廢的品項從沒賣出，只留著給稽核；凡是計算件數或金額都只看 live_lines。',
                    _DOC_BASIS + 'orders.live_lines 的 docstring；orders.subtotal 也用 live_lines'),
        'zm_sku': ('SKU 用 canonical_sku 正規化後再加總（"pst 010" 和 "PST-010" 是同一個商品）。',
                   '程式庫寫明 POS 送來的 SKU 是收銀員打的原樣，比對前一律正規化。',
                   _DOC_BASIS + 'catalog.canonical_sku 的 docstring'),
    }
    levels = _levels(('zm_void', 'zm_sku'), ('void_counted', 'raw_sku'))

    def _case(self, rng, plain):
        orders = []
        for n in range(rng.randint(2, 3) if plain else rng.randint(2, 6)):
            lines = _sold_lines(rng, REGULAR, rng.randint(1, 3), 8, 0 if plain else .4, 0 if plain else .35)
            orders.append(_order('o-%d' % (n + 1), 'c-%d' % rng.randint(101, 199),
                                 _local(rng.choice(DAYS), rng.randint(8, 20)), rng.choice(REVENUE), lines))
        return [orders]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 3


# ---- ZN：預購蛋糕算錯月份（修 bug，shop/reports.py）----

ZN_IMPORTS = 'from .clock import business_date, local_time\nfrom .orders import counts_as_revenue, revenue_day, revenue_total'
ZN_ORIGINAL = _reports(ZN_IMPORTS, r'''
def monthly_revenue(orders):
    """Revenue per month, e.g. {"2026-03": 1285000}."""
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        month = order["placed_at"][:7]
        totals[month] = totals.get(month, 0) + revenue_total(order)
    return dict(sorted(totals.items()))
''')
ZN_REFERENCE = _reports(ZN_IMPORTS, r'''
def _solve(orders, skip=frozenset()):
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        preorder = order["pickup_on"] if "zn_preorder" not in skip else None
        if preorder:
            month = preorder[:7]
        elif "zn_bizday" in skip:
            month = order["placed_at"][:7]
        elif "zn_bizday_local" in skip:
            month = local_time(order["placed_at"]).strftime("%Y-%m")
        else:
            month = business_date(order["placed_at"])[:7]
        totals[month] = totals.get(month, 0) + revenue_total(order)
    return dict(sorted(totals.items()))


def monthly_revenue(orders):
    return _solve(orders)
''')
_ZN_TRAP = r'''
def monthly_revenue(orders):
    """Revenue per month, e.g. {"2026-03": 1285000}."""
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        month = <<month>>
        totals[month] = totals.get(month, 0) + revenue_total(order)
    return dict(sorted(totals.items()))
'''
ZN_TRAPS = {
    'pickup_utc': _reports(ZN_IMPORTS, tk._fill(_ZN_TRAP, month='(order["pickup_on"] or order["placed_at"])[:7]')),
    'pickup_local': _reports(ZN_IMPORTS, tk._fill(
        _ZN_TRAP, month='order["pickup_on"][:7] if order["pickup_on"] else '
                        'local_time(order["placed_at"]).strftime("%Y-%m")')),
}
_ZN_BOUNDARY = ('2026-02-27', '2026-02-28', '2026-03-01', '2026-03-02', '2026-03-15', '2026-03-30', '2026-03-31',
                '2026-04-01', '2026-04-02')


class MonthlyRevenue(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZN', 'monthly_revenue', 'bugfix', 'monthly_revenue', 'shop/reports.py'
    reference_target, trap_targets, original_target = ZN_REFERENCE, ZN_TRAPS, ZN_ORIGINAL
    report = ('Finance says March revenue is missing the Lin family\'s birthday cake. It was ordered on 27 February '
              'and picked up on 2 March, and we book preorders in the month they are picked up.\n'
              '`{call}` {got}; it should return {expected}.')
    trap_breaks = {'pickup_utc': {'zn_bizday'}, 'pickup_local': {'zn_bizday'}}
    probe_skips = {'zn_bizday': ('zn_bizday', 'zn_bizday_local')}
    requirements = {
        'zn_preorder': ('預購按取貨日的月份入帳。', '回報本身。', 'report'),
        'zn_bizday': ('其餘訂單按營業日的月份：每月 1 日凌晨 0～4 點的單算上個月，1 日早上 4～8 點的單（UTC 還是上個月）算這個月。',
                      '根本原因是沒用營收入帳日；程式庫寫明營收入帳在 revenue_day（非預購就是下單的營業日），'
                      '每天、每週、每月都用營業日，daily_revenue 也這樣做。只把預購改成取貨日、其餘照舊切 UTC 字串是修症狀。',
                      _DOC_BASIS + 'orders.revenue_day 與 clock.business_date 的 docstring；daily_revenue 用 revenue_day'),
        tk.REGRESSION: ('原本就正確的行為不能改壞（例如只算營收狀態、排除內部測試單、禮券不算）。', '修 bug 不可以順手改壞別的地方。',
                        'report'),
    }
    levels = _levels(('zn_bizday',), ('pickup_utc', 'pickup_local'), explicit=('zn_preorder',),
                     repro=[[_order('o-1', 'c-131', _local('2026-02-20', 10), 'fulfilled', [_line('BRD-001', 3)]),
                             _order('o-2', 'c-142', _local('2026-02-27', 14), 'paid', [_line('CAK-020', 8)],
                                    pickup='2026-03-02'),
                             _order('o-3', 'c-155', _local('2026-03-10', 12), 'paid', [_line('PST-010', 4)])]])

    def _case(self, rng, plain):
        orders = []
        for n in range(rng.randint(3, 4) if plain else rng.randint(3, 8)):
            day = rng.choice(_ZN_BOUNDARY)
            if plain:
                hour = rng.randint(9, 20)
            elif day.endswith('-01') and rng.random() < .7:
                hour = rng.randint(0, 7)
            else:
                hour = rng.choice((rng.randint(0, 3), rng.randint(8, 22)))
            pickup = None
            if not plain and rng.random() < .25:   # 預購：取貨日前 2～5 天的白天下單
                pickup, hour = day, rng.randint(10, 18)
                day = (date.fromisoformat(day) - timedelta(days=rng.randint(2, 5))).isoformat()
            customer = 'qa-01' if not plain and rng.random() < .1 else 'c-%d' % rng.randint(101, 199)
            lines = [_line(sku, rng.randint(1, 4)) for sku in rng.sample(REGULAR, 1 if plain else rng.randint(1, 2))]
            orders.append(_order('o-%d' % (n + 1), customer, _local(day, hour, rng.randint(0, 59)),
                                 _status(rng, .7), lines, pickup=pickup))
        return [orders]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 2


# ---- ZO：「guest」登上消費排行榜（修 bug，shop/reports.py）----

ZO_IMPORTS = ('from .customers import resolve_id\n'
              'from .orders import counts_as_revenue, is_guest, is_staff, revenue_day, revenue_total')
ZO_ORIGINAL = _reports(ZO_IMPORTS, r'''
def top_customers(orders, customers, n):
    """The `n` biggest spenders as [[customer_id, cents], ...]; ties go to the smaller id."""
    closed = {c["id"] for c in customers if c["deleted_at"] is not None}
    spent = {}
    for order in orders:
        if not counts_as_revenue(order) or order["customer_id"] in closed:
            continue
        spent[order["customer_id"]] = spent.get(order["customer_id"], 0) + revenue_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]
''')
ZO_REFERENCE = _reports(ZO_IMPORTS, r'''
def _solve(orders, customers, n, skip=frozenset()):
    closed = {c["id"] for c in customers if c["deleted_at"] is not None}
    spent = {}
    for order in orders:
        if not counts_as_revenue(order) or order["customer_id"] in closed:
            continue
        if is_guest(order) and "zo_guest" not in skip:
            continue
        if is_staff(order) and "zo_staff" not in skip:
            continue
        owner = order["customer_id"] if "zo_merge" in skip else resolve_id(customers, order["customer_id"])
        spent[owner] = spent.get(owner, 0) + revenue_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]


def top_customers(orders, customers, n):
    return _solve(orders, customers, n)
''')
_ZO_TRAP = r'''
def top_customers(orders, customers, n):
    """The `n` biggest spenders as [[customer_id, cents], ...]; ties go to the smaller id."""
    closed = {c["id"] for c in customers if c["deleted_at"] is not None}
    spent = {}
    for order in orders:
        if not counts_as_revenue(order) or order["customer_id"] in closed or <<skip>>:
            continue
        owner = <<owner>>
        spent[owner] = spent.get(owner, 0) + revenue_total(order)
    ranked = sorted(spent.items(), key=lambda item: (-item[1], item[0]))
    return [[customer, cents] for customer, cents in ranked[:n]]
'''
_RESOLVED = 'resolve_id(customers, order["customer_id"])'
ZO_TRAPS = {
    'guest_only': _reports(ZO_IMPORTS, tk._fill(_ZO_TRAP, skip='is_guest(order)', owner='order["customer_id"]')),
    'guest_staff': _reports(ZO_IMPORTS, tk._fill(_ZO_TRAP, skip='is_guest(order) or is_staff(order)',
                                                 owner='order["customer_id"]')),
    'guest_merge': _reports(ZO_IMPORTS, tk._fill(_ZO_TRAP, skip='is_guest(order)', owner=_RESOLVED)),
}


class TopCustomersV5(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZO', 'top_customers', 'bugfix', 'top_customers', 'shop/reports.py'
    reference_target, trap_targets, original_target = ZO_REFERENCE, ZO_TRAPS, ZO_ORIGINAL
    report = ('The "top customers" board on the dashboard has "guest" at number one. That is every walk-in sale '
              'without a loyalty card, not a person.\n`{call}` {got}; it should return {expected}.')
    trap_breaks = {'guest_only': {'zo_staff', 'zo_merge'}, 'guest_staff': {'zo_merge'}, 'guest_merge': {'zo_staff'}}
    requirements = {
        'zo_guest': ('guest 不上榜。', '回報本身。', 'report'),
        'zo_staff': ('員工（staff- 開頭）的訂單也不上榜。',
                     '根本原因是把不是顧客的 id 當顧客；程式庫寫明員工不是顧客，跟顧客有關的數字都排除。只排除 guest 是修症狀。',
                     _DOC_BASIS + 'orders.is_staff 的 docstring'),
        'zo_merge': ('合併過的帳號歸到保留的帳號後再排名。',
                     '程式庫寫明訂單保留下單時的 id，每位顧客的數字要用 resolve_id 分組；否則同一個人被拆成兩筆。',
                     _DOC_BASIS + 'customers.resolve_id 的 docstring'),
        tk.REGRESSION: ('原本就正確的行為不能改壞（例如只算營收狀態、排除已關閉帳號、同額時 id 小的在前）。',
                        '修 bug 不可以順手改壞別的地方。', 'report'),
    }
    levels = _levels(('zo_staff', 'zo_merge'), ('guest_only', 'guest_staff', 'guest_merge'), explicit=('zo_guest',),
                     repro=[[_order('o-1', 'c-201', _local('2026-03-03', 9), 'paid', [_line('CAK-020', 6)]),
                             _order('o-2', 'guest', _local('2026-03-03', 10), 'paid', [_line('BRD-001', 9)]),
                             _order('o-3', 'c-202', _local('2026-03-03', 11), 'fulfilled', [_line('DRK-030', 5)]),
                             _order('o-4', 'guest', _local('2026-03-03', 12), 'paid', [_line('BRD-002', 4)])],
                            [_customer('c-201', 'ann.lee@example.com'), _customer('c-202', 'bo@mail.tw')], 2])

    def _case(self, rng, plain):
        customers = _accounts(rng, 3 if plain else rng.randint(3, 6), 0 if plain else rng.randint(1, 2), .2)
        orders = []
        for n in range(rng.randint(3, 4) if plain else rng.randint(4, 9)):
            roll = rng.random()
            if not plain and roll < .15:
                customer = 'guest'
            elif not plain and roll < .3:
                customer = 'staff-0%d' % rng.randint(1, 3)
            else:
                customer = rng.choice(customers)['id']
            lines = [_line(sku, rng.randint(1, 5)) for sku in rng.sample(REGULAR, 1 if plain else rng.randint(1, 2))]
            orders.append(_order('o-%d' % (n + 1), customer, _local(rng.choice(DAYS), rng.randint(8, 20)),
                                 _status(rng, .7), lines))
        return [orders, customers, 2 if plain else rng.randint(2, 4)]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 2


# ---- ZP：新收銀機讓品類報表當掉（修 bug，shop/reports.py）----

ZP_IMPORTS = ('from .catalog import canonical_sku\n'
              'from .orders import counts_as_revenue, is_gift_card, live_lines, revenue_day, revenue_total')
ZP_ORIGINAL = _reports(ZP_IMPORTS, r'''
def revenue_by_category(orders, products):
    """Revenue per product category before discounts, e.g. {"bread": 12850}."""
    category = {p["sku"]: p["category"] for p in products}
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        for line in order["lines"]:
            name = category[line["sku"]]
            totals[name] = totals.get(name, 0) + line["qty"] * line["unit_cents"]
    return totals
''')
ZP_REFERENCE = _reports(ZP_IMPORTS, r'''
def _solve(orders, products, skip=frozenset()):
    category = {p["sku"]: p["category"] for p in products}
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        for line in (order["lines"] if "zp_void" in skip else live_lines(order)):
            if "zp_gift" not in skip and is_gift_card(line):
                continue
            name = category[line["sku"] if "zp_sku" in skip else canonical_sku(line["sku"])]
            totals[name] = totals.get(name, 0) + line["qty"] * line["unit_cents"]
    return totals


def revenue_by_category(orders, products):
    return _solve(orders, products)
''')
_ZP_TRAP = r'''
def revenue_by_category(orders, products):
    """Revenue per product category before discounts, e.g. {"bread": 12850}."""
    category = {p["sku"]: p["category"] for p in products}
    totals = {}
    for order in orders:
        if not counts_as_revenue(order):
            continue
        for line in <<lines>>:
            if <<skip>>:
                continue
            name = category[canonical_sku(line["sku"])]
            totals[name] = totals.get(name, 0) + line["qty"] * line["unit_cents"]
    return totals
'''
ZP_TRAPS = {
    'sku_only': _reports(ZP_IMPORTS, tk._fill(_ZP_TRAP, lines='order["lines"]', skip='False')),
    'sku_void': _reports(ZP_IMPORTS, tk._fill(_ZP_TRAP, lines='live_lines(order)', skip='False')),
    'sku_gift': _reports(ZP_IMPORTS, tk._fill(_ZP_TRAP, lines='order["lines"]', skip='is_gift_card(line)')),
}


class RevenueByCategoryV5(RepoFamilyV5):
    letter, name, kind, entry, target = 'ZP', 'revenue_by_category', 'bugfix', 'revenue_by_category', \
        'shop/reports.py'
    reference_target, trap_targets, original_target = ZP_REFERENCE, ZP_TRAPS, ZP_ORIGINAL
    report = ('Since the new tills went live the category report crashes. The tills send SKUs the way the cashier '
              'typed them.\n`{call}` {got}; it should return {expected}.')
    trap_breaks = {'sku_only': {'zp_gift', 'zp_void'}, 'sku_void': {'zp_gift'}, 'sku_gift': {'zp_void'}}
    requirements = {
        'zp_sku': ('SKU 先用 canonical_sku 正規化再查品類。', '回報本身。', 'report'),
        'zp_gift': ('禮券品項不算營收，不出現在品類報表裡。',
                    '程式庫寫明禮券在被使用前是負債、從不算營收（is_gift_card、revenue_total）；這份是營收報表。',
                    _DOC_BASIS + 'orders.is_gift_card 與 orders.revenue_total 的 docstring'),
        'zp_void': ('作廢的品項不算：用 live_lines。',
                    '程式庫寫明作廢的品項從沒賣出，凡是計算件數或金額都只看 live_lines。',
                    _DOC_BASIS + 'orders.live_lines 的 docstring'),
        tk.REGRESSION: ('原本就正確的行為不能改壞（例如只算營收狀態、排除內部測試單、折扣前金額、已下架商品照算）。',
                        '修 bug 不可以順手改壞別的地方。', 'report'),
    }
    levels = _levels(('zp_gift', 'zp_void'), ('sku_only', 'sku_void', 'sku_gift'), explicit=('zp_sku',),
                     repro=[[_order('o-1', 'c-131', _local('2026-03-03', 9), 'paid',
                                    [_line('BRD-001', 2), _line('PST-010', 3, raw='pst 010')]),
                             _order('o-2', 'c-142', _local('2026-03-03', 10), 'fulfilled', [_line('DRK-030', 2)])],
                            ALL_PRODUCTS])

    def _case(self, rng, plain):
        skus = REGULAR + [p['sku'] for p in ARCHIVED]
        orders = []
        for n in range(2 if plain else rng.randint(2, 6)):
            lines = _sold_lines(rng, skus, rng.randint(1, 3), 5, 0 if plain else .4, 0 if plain else .35)
            if not plain and rng.random() < .3:
                gift = rng.choice(GIFTS)['sku']
                lines.append(_line(gift, 1, _raw_sku(rng, gift) if rng.random() < .3 else None))
            customer = 'qa-02' if not plain and rng.random() < .1 else 'c-%d' % rng.randint(101, 199)
            orders.append(_order('o-%d' % (n + 1), customer, _local(rng.choice(DAYS), rng.randint(8, 20)),
                                 _status(rng, .7), lines))
        return [orders, ALL_PRODUCTS]

    def public_case(self, rng, level, index):
        return self._case(rng, True)

    def random_case(self, rng, level):
        return self._case(rng, False)

    def plain_enough(self, level, args, expected):
        return len(expected) >= 2


REPO_FAMILIES = {f.letter: f for f in (AverageOrderValue(), CustomersPerDay(), LifetimeSpend(), NewsletterRecipients(),
                                       UnitsSold(), MonthlyRevenue(), TopCustomersV5(), RevenueByCategoryV5())}
