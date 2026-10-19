"""Public synthetic qualification material, never a production correctness oracle.

Reference SQL, independent Python expectations and deliberate wrong queries are
review-only. SqlContract receives only the prompt/schema/public samples. There
is no dispatch, model selection, private dataset or paid request in this module.
"""
from dataclasses import dataclass
import hashlib
import json

from .sql import SqlContract


VERSION = 'sql-review-catalog-v1'
TABLES = {
    'customers': [('id', 'INTEGER'), ('name', 'TEXT')],
    'orders': [('id', 'INTEGER'), ('customer', 'INTEGER'), ('amount', 'INTEGER'),
               ('day', 'TEXT'), ('status', 'TEXT')],
    'lefts': [('label', 'TEXT')], 'rights': [('label', 'TEXT')],
}


def _row(i, customer, amount, day, status='paid'):
    return dict(id=i, customer=customer, amount=amount, day=day, status=status)


def datasets():
    # All inputs and expected rows are intentionally public. Each query also
    # faces empty relations and substantially different values/multiplicities.
    return [
        {'customers': [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}, {'id': 3, 'name': 'C'}],
         'orders': [_row(1, 1, 5, '2025-12-31'), _row(2, 1, 7, '2026-01-01'),
                    _row(3, 1, 7, '2026-01-31'), _row(4, 2, 9, '2026-02-01'),
                    _row(5, 2, None, '2026-01-02'), _row(6, None, 4, '2026-01-03'),
                    _row(7, 2, 8, '2026-01-04', 'void')],
         'lefts': [{'label': None}, {'label': 'a'}, {'label': 'a'}],
         'rights': [{'label': 'a'}, {'label': 'B'}]},
        {'customers': [{'id': 8, 'name': 'Z'}, {'id': 9, 'name': 'Y'}],
         'orders': [_row(20, 8, -3, '2026-01-01'), _row(21, 8, -3, '2026-01-01'),
                    _row(22, 9, 12, '2026-02-01', 'void'),
                    _row(23, 8, None, '2026-01-10'), _row(24, None, 2, None)],
         'lefts': [{'label': 'x'}], 'rights': [{'label': None}, {'label': 'x'}, {'label': 'z'}]},
        {'customers': [], 'orders': [], 'lefts': [], 'rights': []},
        {'customers': [{'id': 4, 'name': 'D'}], 'orders': [],
         'lefts': [{'label': 'only'}], 'rights': []},
        {'customers': [{'id': 5, 'name': 'E'}],
         'orders': [_row(30, 5, None, '2026-01-01')], 'lefts': [], 'rights': [{'label': None}]},
    ]


def _paid(data):
    return [r for r in data['orders'] if r['status'] == 'paid']


def _key(value):
    return (value is not None, value)


def _sum(values):
    numbers = [n for n in values if n is not None]
    return sum(numbers) if numbers else None


def total(data):
    return [[_sum(r['amount'] for r in data['orders'])]]


def january(data):
    return [[r['id']] for r in sorted(_paid(data), key=lambda r: r['id'])
            if r['day'] is not None and '2026-01-01' <= r['day'] < '2026-02-01']


def grouped(data):
    groups = {}
    for row in _paid(data):
        groups.setdefault(row['customer'], []).append(row)
    return [[customer, len(rows), _sum(r['amount'] for r in rows)]
            for customer, rows in sorted(groups.items(), key=lambda pair: _key(pair[0]))]


def counts(data):
    return [[c['id'], sum(r['customer'] == c['id'] for r in _paid(data))]
            for c in sorted(data['customers'], key=lambda r: r['id'])]


def absent(data):
    return [[c['id']] for c in sorted(data['customers'], key=lambda r: r['id'])
            if not any(r['customer'] == c['id'] for r in _paid(data))]


def winners(data):
    groups = {}
    for row in _paid(data):
        groups.setdefault(row['customer'], []).append(row)
    return [[customer, min(rows, key=lambda r: (r['amount'] is None,
                                               -(r['amount'] or 0), r['id']))['id']]
            for customer, rows in sorted(groups.items(), key=lambda pair: _key(pair[0]))]


def running(data):
    groups = {}
    for row in _paid(data):
        groups.setdefault(row['customer'], []).append(row)
    output = []
    for customer, rows in sorted(groups.items(), key=lambda pair: _key(pair[0])):
        accumulated = 0
        for row in sorted(rows, key=lambda r: (_key(r['day']), r['id'])):
            accumulated += row['amount'] or 0
            output.append([customer, row['id'], accumulated])
    return output


def labels(data):
    return [[v] for v in sorted({r['label'] for t in ('lefts', 'rights') for r in data[t]}, key=_key)]


@dataclass(frozen=True)
class ReviewCase:
    id: str
    operations: tuple
    prompt: str
    columns: tuple
    reference_sql: str
    oracle: object
    hand_expected: tuple
    mutants: tuple

    def contract(self):
        assumptions = ('Public input domain: customer and order IDs are unique non-NULL integers; '
                       'amounts are integers or NULL and sums fit signed int64. Customer references '
                       'may be NULL or absent. Days are valid ISO YYYY-MM-DD strings or NULL. '
                       'Statuses are paid or void. Label duplicates and NULLs are allowed. ')
        return SqlContract(version=VERSION + ':' + self.id, prompt=assumptions + self.prompt,
                           tables=TABLES, expected_columns=list(self.columns), samples=[
                               {'id': f'public-{i}', 'tables': data, 'expected': self.oracle(data)}
                               for i, data in enumerate(datasets())])


def cases():
    return (
        ReviewCase('S01', ('SUM', 'NULL', 'empty'),
                   'Sum amount over ALL orders, including void orders; ignore NULL amounts. '
                   'Return one total row; empty/all-NULL input returns NULL.', ('total',),
                   'SELECT SUM(amount) AS total FROM orders', total, ((40,),),
                   ('SELECT COALESCE(SUM(amount),0) AS total FROM orders',
                    "SELECT SUM(amount) AS total FROM orders WHERE status='paid'")),
        ReviewCase('S02', ('WHERE', 'date-boundary', 'ORDER BY'),
                   'Return paid order IDs with ISO day >=2026-01-01 and <2026-02-01. '
                   'Exclude NULL days. Sort IDs ascending.', ('id',),
                   "SELECT id FROM orders WHERE status='paid' AND day>='2026-01-01' "
                   "AND day<'2026-02-01' ORDER BY id", january, ((2,), (3,), (5,), (6,)),
                   ("SELECT id FROM orders WHERE status='paid' AND day>='2026-01-01' "
                    "AND day<='2026-02-01' ORDER BY id",
                    "SELECT id FROM orders WHERE status='paid' AND day>'2026-01-01' "
                    "AND day<'2026-02-01' ORDER BY id")),
        ReviewCase('S03', ('GROUP BY', 'COUNT', 'SUM', 'duplicates'),
                   'For paid orders, group by customer (NULL is one group), return customer, '
                   'number of orders n including NULL amounts, and amount sum total. '
                   'Keep repeated amounts. Sort customer ascending, NULL first.', ('customer', 'n', 'total'),
                   "SELECT customer,COUNT(*) AS n,SUM(amount) AS total FROM orders "
                   "WHERE status='paid' GROUP BY customer ORDER BY customer", grouped,
                   ((None, 1, 4), (1, 3, 19), (2, 2, 9)),
                   ("SELECT customer,COUNT(amount) AS n,SUM(amount) AS total FROM orders "
                    "WHERE status='paid' GROUP BY customer ORDER BY customer",
                    "SELECT customer,COUNT(*) AS n,SUM(DISTINCT amount) AS total FROM orders "
                    "WHERE status='paid' GROUP BY customer ORDER BY customer")),
        ReviewCase('S04', ('LEFT JOIN', 'COUNT', 'missing'),
                   'For every declared customer return id and paid order count n, zero when '
                   'none. Ignore orders for undeclared/NULL customers. Sort customer IDs ascending.', ('id', 'n'),
                   "SELECT c.id,COUNT(o.id) AS n FROM customers c LEFT JOIN orders o "
                   "ON o.customer=c.id AND o.status='paid' GROUP BY c.id ORDER BY c.id", counts,
                   ((1, 3), (2, 2), (3, 0)),
                   ("SELECT c.id,COUNT(*) AS n FROM customers c LEFT JOIN orders o "
                    "ON o.customer=c.id AND o.status='paid' GROUP BY c.id ORDER BY c.id",
                    "SELECT c.id,COUNT(o.id) AS n FROM customers c JOIN orders o "
                    "ON o.customer=c.id AND o.status='paid' GROUP BY c.id ORDER BY c.id")),
        ReviewCase('S05', ('NOT EXISTS', 'NULL', 'anti-join'),
                   'Return declared customer IDs having no paid orders; NULL customer IDs in '
                   'orders match nobody. Sort IDs ascending.', ('id',),
                   "SELECT c.id FROM customers c WHERE NOT EXISTS(SELECT 1 FROM orders o "
                   "WHERE o.customer=c.id AND o.status='paid') ORDER BY c.id", absent, ((3,),),
                   ("SELECT id FROM customers WHERE id NOT IN(SELECT customer FROM orders "
                    "WHERE status='paid') ORDER BY id",
                    "SELECT c.id FROM customers c WHERE NOT EXISTS(SELECT 1 FROM orders o "
                    "WHERE o.customer=c.id) ORDER BY c.id")),
        ReviewCase('S06', ('ROW_NUMBER', 'tie-break', 'NULL-order'),
                   'For each customer represented in paid orders (including NULL customer), '
                   'return the order with greatest amount; NULL amounts rank last. Break ties '
                   'by smallest order id. Return customer,id sorted customer ascending, NULL first.',
                   ('customer', 'id'),
                   "WITH ranked AS (SELECT customer,id,ROW_NUMBER() OVER(PARTITION BY customer "
                   "ORDER BY amount DESC,id ASC) AS rn FROM orders WHERE status='paid') "
                   'SELECT customer,id FROM ranked WHERE rn=1 ORDER BY customer', winners,
                   ((None, 6), (1, 2), (2, 4)),
                   ("WITH ranked AS (SELECT customer,id,ROW_NUMBER() OVER(PARTITION BY customer "
                    "ORDER BY amount DESC,id DESC) AS rn FROM orders WHERE status='paid') "
                    'SELECT customer,id FROM ranked WHERE rn=1 ORDER BY customer',)),
        ReviewCase('S07', ('window-SUM', 'ROWS', 'COALESCE', 'order'),
                   'For paid orders return customer,id,running total. Within each customer '
                   '(including NULL), order by day ascending (NULL first), then id ascending; '
                   'NULL amount contributes zero. Sum through the current ROW, not peer group. '
                   'Output ordered by customer NULL first,day NULL first,id.', ('customer', 'id', 'total'),
                   "SELECT customer,id,SUM(COALESCE(amount,0)) OVER(PARTITION BY customer "
                   "ORDER BY day,id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS total "
                   "FROM orders WHERE status='paid' ORDER BY customer,day,id", running,
                   ((None, 6, 4), (1, 1, 5), (1, 2, 12), (1, 3, 19), (2, 5, 0), (2, 4, 9)),
                   ("SELECT customer,id,SUM(COALESCE(amount,0)) OVER(PARTITION BY customer) "
                    "AS total FROM orders WHERE status='paid' ORDER BY customer,day,id",)),
        ReviewCase('S08', ('UNION', 'deduplication', 'text-order'),
                   'Return the distinct union of labels from lefts and rights. Include NULL '
                   'once. Order NULL first then SQLite BINARY text ascending (case sensitive).', ('label',),
                   'SELECT label FROM lefts UNION SELECT label FROM rights ORDER BY label', labels,
                   ((None,), ('B',), ('a',)),
                   ('SELECT label FROM lefts UNION ALL SELECT label FROM rights ORDER BY label',
                    'SELECT label FROM lefts UNION SELECT label FROM rights ORDER BY label DESC')),
    )


def review():
    """Reproducible finite mutation evidence; not a model-quality experiment."""
    rows = []
    catalog = []
    for case in cases():
        data = datasets()
        if case.oracle(data[0]) != [list(row) for row in case.hand_expected]:
            raise ValueError('Independent hand expectation disagrees: ' + case.id)
        contract = case.contract()
        catalog.append({'id': case.id, 'operations': case.operations,
                        'public_contract': json.loads(contract._public_json),
                        'reference_sql': case.reference_sql, 'mutants': case.mutants,
                        'hand_expected': case.hand_expected})
        env = contract.environment()
        reference = env.validate(json.dumps({'sql': case.reference_sql}))
        rejected = [env.validate(json.dumps({'sql': sql}))['passed'] is False for sql in case.mutants]
        if reference['passed'] is not True or not all(rejected):
            raise ValueError('Review reference/mutation failure: ' + case.id)
        rows.append({'id': case.id, 'operations': list(case.operations),
                     'contract_version': contract.contract_version, 'public_samples': len(data),
                     'hand_example_agrees': True, 'reference_passes': True,
                     'deliberate_mutants': len(rejected), 'mutants_rejected': sum(rejected)})
    encoded = json.dumps(catalog, sort_keys=True)
    return {'schema_version': 1, 'catalog_version': VERSION, 'kind': 'offline',
            'catalog_sha256': hashlib.sha256(encoded.encode()).hexdigest(), 'cases': rows,
            'human_review': 'pending', 'model_calls': 0,
            'limitation': 'Finite public examples and mutants do not prove general SQL correctness; '
                          'no real model, provider, customer data or OS security sandbox qualification.'}
