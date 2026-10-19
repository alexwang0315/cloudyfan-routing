"""New bounded public catalog; preserves the historical v1 catalog and results.

Diagnostic counterexamples are post-hoc synthetic regression data, not holdout.
References/mutants/independent oracles are review-only, never prompt fields.
"""
import hashlib
import json
from pathlib import Path

from .sql import SqlContract
from . import sql_policy
from .sql_review import cases, datasets


VERSION = 'sql-review-catalog-v2-bounded'


def counterexamples():
    return json.loads(Path(__file__).with_name('sql_counterexamples.json').read_text())['probes']


def contract(case_id, **execution):
    case = next((c for c in cases() if c.id == case_id), None)
    if case is None:
        raise ValueError('Unknown bounded SQL case')
    samples = [{'id': f'public-{i}', 'tables': data, 'expected': case.oracle(data)}
               for i, data in enumerate(datasets())]
    samples.extend({'id': 'regression-' + p['id'], 'tables': p['counterexample_public_tables'],
                    'expected': p['hand_expected']} for p in counterexamples() if p['case_id'] == case_id)
    assumptions = ('Public bounded domain: unique non-NULL signed int64 customer/order IDs, '
                   'including zero and negatives; NULL/undeclared customer references allowed. '
                   'Integer/NULL amounts, abs(amount)<=1000000000, at most 1000 rows per table, '
                   'hence all reference subset/prefix sums have abs<=1000000000000. '
                   'Valid Gregorian YYYY-MM-DD years 0001..9999 or NULL; status paid/void. '
                   'TEXT/NULL labels retain empty strings and BINARY case distinctions. '
                   'No timezone, unit, case or data normalization. ')
    return SqlContract(version=VERSION + ':' + case.id, prompt=assumptions + case.prompt,
                       tables=sql_policy.TABLES, expected_columns=list(case.columns), samples=samples,
                       domain_policy=sql_policy.VERSION, **execution)


def review():
    """Review the new material, without changing or rescoring historical runs."""
    rows = []
    manual = json.loads(Path(__file__).with_name('sql_counterexamples.json').read_text())['original_manual_expected']
    for case in cases():
        if [case.oracle(data) for data in datasets()] != manual[case.id]:
            raise ValueError('Original hand/oracle disagreement: ' + case.id)
        task = contract(case.id)
        env = task.environment()
        probes = [p for p in counterexamples() if p['case_id'] == case.id]
        for p in probes:
            if case.oracle(p['counterexample_public_tables']) != p['hand_expected']:
                raise ValueError('Hand/oracle disagreement: ' + p['id'])
        reference = env.validate(json.dumps({'sql': case.reference_sql}))['passed'] is True
        rejected = [env.validate(json.dumps({'sql': q}))['passed'] is False
                    for q in case.mutants + tuple(p['diagnostic_wrong_sql'] for p in probes)]
        if not reference or not all(rejected):
            raise ValueError('Bounded reference/mutation failure: ' + case.id)
        rows.append({'id': case.id, 'contract_version': task.contract_version,
                     'public_samples': len(json.loads(task._public_json)['samples']),
                     'reference_passes': reference, 'old_mutants_rejected': len(case.mutants),
                     'new_probes_rejected': [p['id'] for p in probes]})
    payload = json.dumps({'probes': counterexamples(), 'cases': rows}, sort_keys=True).encode()
    return {'schema_version': 1, 'kind': 'offline', 'catalog_version': VERSION,
            'catalog_sha256': hashlib.sha256(payload).hexdigest(), 'cases': rows,
            'original_examples_cross_checked': 40,
            'post_hoc': True, 'holdout': False, 'model_calls': 0,
            'human_review': 'pending', 'production_qualified': False,
            'limitation': 'Finite public regression coverage; no general correctness, '
                          'population error rate, real-model quality or OS isolation qualification.'}
