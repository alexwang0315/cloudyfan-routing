"""Opt-in, prospective S01-S08 input domain; legacy contracts remain unchanged."""
from datetime import date
import hashlib
from pathlib import Path
import re


VERSION = 'rg01-sql-first-scope-policy-v1'
MAX_AMOUNT = 1_000_000_000
MAX_ROWS = 1000
TABLES = {
    'customers': [['id', 'INTEGER'], ['name', 'TEXT']],
    'orders': [['id', 'INTEGER'], ['customer', 'INTEGER'], ['amount', 'INTEGER'],
               ['day', 'TEXT'], ['status', 'TEXT']],
    'lefts': [['label', 'TEXT']], 'rights': [['label', 'TEXT']],
}


def constraints():
    return {'version': VERSION, 'max_abs_amount': MAX_AMOUNT, 'max_rows': MAX_ROWS,
            'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def validate(spec):
    """Reject outside the adopted domain, never normalize or remove input rows.

    Generic shape/type/output bounds are checked first by SqlContract. Per-value
    and row bounds imply abs(sum of any reference subset/prefix) <= 10^12,
    irrespective of insertion order. They do not bound arbitrary model SQL.
    """
    if spec['domain_policy'] != VERSION or spec['tables'] != TABLES:
        raise ValueError('Unknown SQL domain or schema')
    for sample in spec['samples']:
        data = sample['tables']
        if any(len(rows) > MAX_ROWS for rows in data.values()):
            raise ValueError('SQL domain row bound exceeded')
        for table in ('customers', 'orders'):
            ids = [row['id'] for row in data[table]]
            if any(type(i) is not int or not -(2**63) <= i < 2**63 for i in ids) or len(set(ids)) != len(ids):
                raise ValueError('SQL domain requires unique signed int64 IDs')
        for row in data['orders']:
            amount, day = row['amount'], row['day']
            if amount is not None and (type(amount) is not int or abs(amount) > MAX_AMOUNT):
                raise ValueError('SQL domain amount bound exceeded')
            if day is not None and (not isinstance(day, str)
                                    or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day)
                                    or date.fromisoformat(day).isoformat() != day):
                raise ValueError('SQL domain requires a calendar date')
            if row['status'] not in ('paid', 'void'):
                raise ValueError('SQL domain requires paid or void status')
