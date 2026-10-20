"""Prospective model exam; Python oracles and existing frozen SQL worker, no API."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import random

from ..sql import SqlContract
from .catalog import digest

VERSION = 'sql-model-exam-v1'
OPERATIONS = ('sql_filter', 'sql_join', 'sql_aggregate', 'sql_window',
              'sql_set_operations', 'sql_resource_limits')
TABLES = {
    'depots': [('id', 'INTEGER PRIMARY KEY'), ('zone', 'TEXT')],
    'parcels': [('id', 'INTEGER PRIMARY KEY'), ('depot', 'INTEGER'), ('units', 'INTEGER')],
    'legacy': [('depot', 'INTEGER')],
}


def dataset(seed, *, empty=False):
    rng = random.Random(seed)
    depots = [{'id': x, 'zone': 'AB'[i % 2]} for i, x in enumerate([0, 2, 5, 8, 13])]
    parcels = [{'id': i, 'depot': rng.choice([0, 2, 5, 8, 99, None]),
                'units': rng.choice([None, -2, 0, 1, 3, 7])} for i in range(1, 25)]
    # Deliberate NULL, duplicate values, orphan and unmatched parent witnesses.
    parcels += [{'id': 25, 'depot': 0, 'units': None}, {'id': 26, 'depot': 0, 'units': 3},
                {'id': 27, 'depot': 0, 'units': 3}, {'id': 28, 'depot': 99, 'units': 7},
                *[{'id': x, 'depot': 77, 'units': None} for x in range(29, 33)],
                {'id': 33, 'depot': 0, 'units': 7}, {'id': 34, 'depot': 0, 'units': 7}]
    legacy = [{'depot': x} for x in [0, 0, 2, 13, 21, None]]
    return {'depots': [] if empty else depots, 'parcels': [] if empty else parcels,
            'legacy': [] if empty else legacy}


def definition(family, variant, k):
    """SQL reference and independent Python semantics; never use model answers."""
    if family == 'filter':
        if variant == 0:
            return (['id', 'units'], f'SELECT id, units FROM parcels WHERE units >= {k} ORDER BY id',
                    lambda d: [[r['id'], r['units']] for r in sorted(d['parcels'], key=lambda x: x['id'])
                               if r['units'] is not None and r['units'] >= k],
                    f'Return parcel id and units for units >= {k}; exclude NULL units; order by id.',
                    f'SELECT id, units FROM parcels WHERE units < {k} ORDER BY id')
        return (['id', 'units'], f'SELECT id, COALESCE(units, 0) AS units FROM parcels WHERE COALESCE(units, 0) <= {k} ORDER BY id',
                lambda d: [[r['id'], r['units'] or 0] for r in sorted(d['parcels'], key=lambda x: x['id']) if (r['units'] or 0) <= k],
                f'Treat NULL units as zero; return id and units where that value <= {k}; order by id.',
                f'SELECT id, units FROM parcels WHERE units <= {k} ORDER BY id')
    if family == 'join':
        if variant == 0:
            return (['id', 'zone'], f'SELECT p.id, d.zone FROM parcels p JOIN depots d ON d.id=p.depot WHERE p.units >= {k} ORDER BY p.id',
                    lambda d: [[p['id'], next(x['zone'] for x in d['depots'] if x['id'] == p['depot'])]
                               for p in sorted(d['parcels'], key=lambda x: x['id'])
                               if p['units'] is not None and p['units'] >= k and p['depot'] in {x['id'] for x in d['depots']}],
                    f'Return parcel id and its existing depot zone when units >= {k}; exclude orphans; order by parcel id.',
                    f'SELECT p.id, d.zone FROM parcels p LEFT JOIN depots d ON d.id=p.depot WHERE p.units >= {k} ORDER BY p.id')
        return (['id'], f'SELECT d.id FROM depots d WHERE NOT EXISTS (SELECT 1 FROM parcels p WHERE p.depot=d.id AND p.units >= {k}) ORDER BY d.id',
                lambda d: [[x['id']] for x in sorted(d['depots'], key=lambda x: x['id']) if not any(p['depot'] == x['id'] and p['units'] is not None and p['units'] >= k for p in d['parcels'])],
                f'Return existing depot ids with no parcel having units >= {k}; include depots with no parcels; order by id.',
                f'SELECT d.id FROM depots d JOIN parcels p ON p.depot=d.id GROUP BY d.id HAVING MAX(p.units) < {k} ORDER BY d.id')
    if family == 'aggregate':
        if variant == 0:
            def oracle(d):
                groups = defaultdict(list)
                for r in d['parcels']:
                    if r['depot'] is not None:
                        groups[r['depot']].append(r['units'])
                return [[key, sum(v for v in vals if v is not None)] for key, vals in sorted(groups.items())
                        if sum(v is not None for v in vals) >= k]
            return (['depot', 'total'], f'SELECT depot, COALESCE(SUM(units),0) AS total FROM parcels WHERE depot IS NOT NULL GROUP BY depot HAVING COUNT(units) >= {k} ORDER BY depot', oracle,
                    f'Group parcels by non-NULL depot, including orphan depot ids. Keep groups with at least {k} non-NULL units; total non-NULL units, or zero; order by depot.',
                    f'SELECT depot, COALESCE(SUM(units),0) AS total FROM parcels WHERE depot IS NOT NULL GROUP BY depot HAVING COUNT(*) >= {k} ORDER BY depot')
        return (['id', 'n'], f'SELECT d.id, COUNT(DISTINCT p.units) AS n FROM depots d LEFT JOIN parcels p ON p.depot=d.id AND p.units >= {k} GROUP BY d.id ORDER BY d.id',
                lambda d: [[x['id'], len({p['units'] for p in d['parcels'] if p['depot'] == x['id'] and p['units'] is not None and p['units'] >= k})] for x in sorted(d['depots'], key=lambda x: x['id'])],
                f'For every existing depot return id and count n of distinct units >= {k}. Preserve zero counts. Order by id.',
                f'SELECT d.id, COUNT(p.units) AS n FROM depots d LEFT JOIN parcels p ON p.depot=d.id AND p.units >= {k} GROUP BY d.id ORDER BY d.id')
    if family == 'window':
        def oracle(d):
            groups = defaultdict(list)
            for p in d['parcels']:
                if p['depot'] is not None:
                    groups[p['depot']].append(p)
            out = []
            for depot, rows in sorted(groups.items()):
                rows.sort(key=lambda x: x['id'])
                total = 0
                for i, r in enumerate(rows):
                    total += r['units'] or 0
                    if variant == 0:
                        if i < k:
                            out.append([depot, r['id'], total])
                    else:
                        previous = rows[i-1]['units'] or 0 if i else 0
                        out.append([depot, r['id'], (r['units'] or 0) - previous])
            return out
        if variant == 0:
            return (['depot', 'id', 'value'], f'WITH w AS (SELECT depot,id,SUM(COALESCE(units,0)) OVER(PARTITION BY depot ORDER BY id ROWS UNBOUNDED PRECEDING) AS value,ROW_NUMBER() OVER(PARTITION BY depot ORDER BY id) AS rn FROM parcels WHERE depot IS NOT NULL) SELECT depot,id,value FROM w WHERE rn <= {k} ORDER BY depot,id', oracle,
                    f'Within each non-NULL depot (including orphans), order parcels by id. Return its first {k} parcels with cumulative units as value, treating NULL as zero. Order by depot,id.',
                    f'SELECT depot,id,COALESCE(units,0) AS value FROM parcels WHERE depot IS NOT NULL ORDER BY depot,id LIMIT {k}')
        return (['depot', 'id', 'value'], 'SELECT depot,id,COALESCE(units,0)-LAG(COALESCE(units,0),1,0) OVER(PARTITION BY depot ORDER BY id) AS value FROM parcels WHERE depot IS NOT NULL ORDER BY depot,id', oracle,
                'For each parcel with non-NULL depot, return depot,id,value: current units minus previous parcel units within that depot ordered by id. Treat NULL and absent previous units as zero. Order by depot,id.',
                'SELECT depot,id,COALESCE(units,0) AS value FROM parcels WHERE depot IS NOT NULL ORDER BY depot,id')
    if family == 'set':
        op = 'UNION' if variant == 0 else 'EXCEPT'
        def oracle(d):
            left = {p['depot'] for p in d['parcels'] if p['depot'] is not None and p['units'] is not None and p['units'] >= k}
            right = {r['depot'] for r in d['legacy'] if r['depot'] is not None}
            return [[v] for v in sorted(left | right if variant == 0 else left - right)]
        return (['depot'], f'SELECT depot FROM parcels WHERE depot IS NOT NULL AND units >= {k} {op} SELECT depot FROM legacy WHERE depot IS NOT NULL ORDER BY depot', oracle,
                f'Take distinct non-NULL depot ids of parcels with units >= {k}. '+('Union with' if variant == 0 else 'Remove')+' non-NULL ids in legacy. Include orphan ids; output depot in ascending order.',
                f'SELECT depot FROM parcels WHERE depot IS NOT NULL AND units >= {k} ORDER BY depot')
    if family != 'resource':
        raise ValueError('Unknown SQL exam family')
    # Bounded correlated counting: correctness + actual frozen worker limits, not an OS/SLO benchmark.
    if variant == 0:
        def oracle(d):
            counts = Counter(p['depot'] for p in d['parcels'] if p['units'] is not None and p['units'] >= k)
            return [[x['id'], counts[x['id']]] for x in sorted(d['depots'], key=lambda x: x['id'])]
        return (['id', 'n'], f'SELECT d.id,(SELECT COUNT(*) FROM parcels p WHERE p.depot=d.id AND p.units >= {k}) AS n FROM depots d ORDER BY d.id', oracle,
                f'Within the published resource limits, return every depot id and number n of parcels with units >= {k}, including zero counts; order by id. One read-only query only.',
                f'SELECT d.id,(SELECT COUNT(*) FROM parcels p WHERE p.units >= {k}) AS n FROM depots d ORDER BY d.id')
    return (['id', 'total'], f'SELECT d.id,COALESCE((SELECT SUM(p.units) FROM parcels p WHERE p.depot=d.id AND p.units >= {k}),0) AS total FROM depots d ORDER BY d.id',
            lambda d: [[x['id'], sum(p['units'] for p in d['parcels'] if p['depot'] == x['id'] and p['units'] is not None and p['units'] >= k)] for x in sorted(d['depots'], key=lambda x: x['id'])],
            f'Within the published resource limits, return every depot id and sum total of units >= {k}, zero for no eligible rows; order by id. One read-only query only.',
            f'SELECT d.id,COALESCE((SELECT SUM(p.units) FROM parcels p WHERE p.units >= {k}),0) AS total FROM depots d ORDER BY d.id')


@dataclass(frozen=True)
class ExamCase:
    id: str
    family: str
    stratum: str
    k: int
    variant: int

    @property
    def operations(self):
        primary = {'filter': 'sql_filter', 'join': 'sql_join', 'aggregate': 'sql_aggregate',
                   'window': 'sql_window', 'set': 'sql_set_operations', 'resource': 'sql_resource_limits'}
        return sorted({primary[self.family], 'sql_resource_limits'})

    def contract(self, *, grading=False):
        columns, _, oracle, prompt, _ = definition(self.family, self.variant, self.k)
        seeds = [20000 + int(self.id[1:]) * 17 + i for i in (range(2, 6) if grading else range(2))]
        samples = [{'id': 'case-' + str(i), 'tables': dataset(seed, empty=(not grading and i == 1))} for i, seed in enumerate(seeds)]
        for sample in samples:
            sample['expected'] = oracle(sample['tables'])
        return SqlContract(version=f'{VERSION}:{self.id}:'+('sealed' if grading else 'public'),
                           prompt=prompt, tables=TABLES, expected_columns=columns, samples=samples)

    def answer(self, *, mutant=False):
        values = definition(self.family, self.variant, self.k)
        return json.dumps({'sql': values[4 if mutant else 1]})


def cases():
    families = [x for x in ('filter', 'join', 'aggregate', 'window', 'set', 'resource') for _ in range(8)] + ['resource'] * 2
    strata = ['easy'] * 3 + ['medium'] * 4 + ['hard'] * 3
    return [ExamCase(f'Q{i+1:03}', family, strata[i % 10], (i % 4)+1, (i // 4) % 2) for i, family in enumerate(families)]


def manifest():
    return deepcopy(_manifest(hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))


@lru_cache(maxsize=1)
def _manifest(implementation_sha256):
    body = {'schema_version': VERSION, 'purpose': 'prospective independent SQL model qualification',
            'implementation_sha256': implementation_sha256,
            'cases': [{'id': c.id, 'family': c.family, 'stratum': c.stratum, 'operations': c.operations,
                       'public_sha256': digest(json.loads(c.contract()._public_json)),
                       'grading_sha256': digest(json.loads(c.contract(grading=True)._public_json))} for c in cases()],
            'case_count': 50, 'independent_model_evidence': False,
            'sql_runtime': json.loads(cases()[0].contract()._runtime_json)}
    return {**body, 'suite_id': digest(body)}
