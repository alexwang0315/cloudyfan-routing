"""Prospective bounded SQL policy at the agreed Core.run/public-validator seam."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from cloudyfan_jev.sql import SqlContract, run_sql
from cloudyfan_jev.sql_review import cases
from test_engine import API
from test_sql_environment import answer, sql_core


POLICY = 'rg01-sql-first-scope-policy-v1'


def bounded_contract(data=None, **changes):
    spec = json.loads(cases()[0].contract()._public_json)
    spec['version'] = 'bounded-test-v1'
    if data is not None:
        spec['samples'] = [{'id': 'public', 'tables': data, 'expected': [[None]]}]
    spec.update(changes)
    return SqlContract(**spec, domain_policy=POLICY)


def test_out_of_domain_amount_is_rejected_before_model_dispatch(tmp_path):
    from cloudyfan_jev.sql_review import datasets
    data = deepcopy(datasets()[0])
    data['orders'][0]['amount'] = 1_000_000_001
    api = API(messages=[answer('SELECT SUM(amount) AS total FROM orders')])
    with pytest.raises(ValueError, match='Invalid public SQL contract'):
        run_sql(sql_core(tmp_path, api), 'out-of-domain', bounded_contract(data))
    assert not api.requests


@pytest.mark.parametrize('table,field,value', [
    ('orders', 'amount', -1_000_000_001), ('orders', 'amount', True),
    ('orders', 'amount', 1.5), ('orders', 'id', None),
    ('orders', 'id', 2**63), ('orders', 'customer', -(2**63)-1),
    ('orders', 'day', '2026-02-29'), ('orders', 'day', '0000-01-01'),
    ('orders', 'day', '2026-1-01'), ('orders', 'day', '2026-01-01T00:00:00Z'),
    ('orders', 'status', 'unknown'), ('customers', 'id', None),
])
def test_invalid_domain_data_never_dispatches(tmp_path, table, field, value):
    from cloudyfan_jev.sql_review import datasets
    data = deepcopy(datasets()[0])
    data[table][0][field] = value
    api = API()
    with pytest.raises(ValueError, match='Invalid public SQL contract'):
        run_sql(sql_core(tmp_path, api), 'invalid-domain', bounded_contract(data))
    assert not api.requests


def test_duplicate_ids_and_excess_rows_are_not_repaired_by_dropping_data():
    from cloudyfan_jev.sql_review import datasets
    data = deepcopy(datasets()[0])
    data['orders'][1]['id'] = data['orders'][0]['id']
    with pytest.raises(ValueError):
        bounded_contract(data)
    data = deepcopy(datasets()[2])
    data['orders'] = [dict(id=i, customer=None, amount=1, day=None, status='paid') for i in range(1001)]
    with pytest.raises(ValueError):
        bounded_contract(data)


@pytest.mark.parametrize('amount,total', [(1_000_000_000,1_000_000_000_000),
                                         (-1_000_000_000,-1_000_000_000_000), (None,None)])
def test_thousand_row_amount_boundary_and_signed_ids_pass(tmp_path, amount, total):
    from cloudyfan_jev.sql_review import datasets
    data = deepcopy(datasets()[2])
    data['customers'] = [{'id': -(2**63), 'name': ''}, {'id': 0, 'name': None},
                         {'id': 2**63-1, 'name': 'max'}]
    data['orders'] = [dict(id=i-500, customer=99, amount=amount,
                           day='0001-01-01' if i % 2 else '9999-12-31', status='paid') for i in range(1000)]
    api = API(messages=[answer('SELECT SUM(amount) AS total FROM orders')])
    task = bounded_contract(data, samples=[{'id':'boundary','tables':data,'expected':[[total]]}])
    engine = sql_core(tmp_path, api)
    # A 1,000-row public fixture requires an explicitly larger context budget;
    # the default Core limit is not silently changed by the SQL adapter.
    engine.limits = replace(engine.limits, max_context_bytes=512_000)
    result = run_sql(engine, 'valid-boundary', task)
    assert result['status'] == 'passed' and len(api.requests) == 1


def test_policy_identity_cannot_reuse_legacy_run_or_unknown_policy(tmp_path):
    case = cases()[0]
    api = API(messages=[answer(case.reference_sql)])
    engine = sql_core(tmp_path, api)
    original = case.contract()
    assert run_sql(engine, 'versioned', original)['status'] == 'passed'
    with pytest.raises(ValueError, match='Resume evidence changed'):
        run_sql(engine, 'versioned', bounded_contract())
    assert len(api.requests) == 1
    spec = json.loads(original._public_json)
    with pytest.raises(ValueError, match='Invalid public SQL contract'):
        SqlContract(**spec, domain_policy='future-unknown')


def test_policy_rejects_incomplete_schema_and_oversized_expected():
    from cloudyfan_jev.sql_review import datasets
    with pytest.raises(ValueError):
        bounded_contract(tables={'orders': [('amount','INTEGER')]})
    data = datasets()[2]
    with pytest.raises(ValueError):
        bounded_contract(data, samples=[{'id':'too-many','tables':data,'expected':[[1]]*101}])


def test_new_catalog_rejects_negative_sum_mutant_and_repairs_once(tmp_path):
    from cloudyfan_jev.sql_catalog import contract, counterexamples
    probe = counterexamples()[0]
    case = cases()[0]
    assert case.contract().environment().validate(answer(probe['diagnostic_wrong_sql'])['content'])['passed'] is True
    api = API(messages=[answer(probe['diagnostic_wrong_sql']), answer(case.reference_sql)])
    result = run_sql(sql_core(tmp_path, api), 'new-negative-sum', contract('S01'))
    assert result['status'] == 'passed' and len(result['attempts']) == 2
    assert result['attempts'][0]['public']['checks'] == ['SQL_PUBLIC_RESULT_MISMATCH']
    assert result['costs']['api_usd'] == '0.0201' and len(api.requests) == 3


from cloudyfan_jev import sql_catalog


@pytest.mark.parametrize('probe', sql_catalog.counterexamples(), ids=lambda p: p['id'])
def test_all_known_misses_are_rejected_then_repair_with_public_feedback(tmp_path, probe):
    case = next(c for c in cases() if c.id == probe['case_id'])
    task = sql_catalog.contract(case.id)
    public = json.loads(task._public_json)
    assert case.oracle(probe['counterexample_public_tables']) == probe['hand_expected']
    assert probe['diagnostic_wrong_sql'] not in task.task()['prompt']
    assert case.reference_sql not in task.task()['prompt']
    assert all(set(s) == {'id','tables','expected'} for s in public['samples'])
    api = API(messages=[answer(probe['diagnostic_wrong_sql']), answer(case.reference_sql)])
    engine = sql_core(tmp_path, api)
    result = run_sql(engine, probe['id'], task)
    assert result['first_public_pass'] is False and result['final_public_pass'] is True
    assert result['status'] == 'passed' and len(result['attempts']) == 2
    assert result['costs']['api_usd'] == '0.0201' and len(api.requests) == 3
    assert result['attempts'][0]['public']['checks'] == ['SQL_PUBLIC_RESULT_MISMATCH']
    assert run_sql(engine, probe['id'], task) == result and len(api.requests) == 3


def test_new_catalog_review_rejects_old_and_new_mutants_without_changing_old_catalog():
    report = sql_catalog.review()
    assert report['catalog_version'] == 'sql-review-catalog-v2-bounded'
    assert sum(r['old_mutants_rejected'] for r in report['cases']) == 14
    assert sum(len(r['new_probes_rejected']) for r in report['cases']) == 13
    assert sum(r['public_samples'] for r in report['cases']) == 53
    assert report['holdout'] is False and report['production_qualified'] is False
    assert len(cases()[0].contract().environment().spec['samples']) == 5


def test_equivalent_window_range_query_is_still_valid_with_unique_full_ordering(tmp_path):
    case = next(c for c in cases() if c.id == 'S07')
    query = case.reference_sql.replace('ROWS BETWEEN', 'RANGE BETWEEN')
    api = API(messages=[answer(query)])
    result = run_sql(sql_core(tmp_path, api), 'equivalent-range', sql_catalog.contract('S07'))
    assert result['first_public_pass'] is True and len(api.requests) == 1


@pytest.mark.parametrize('case', cases(), ids=lambda c: c.id)
def test_constant_first_example_cannot_pass_new_catalog(case):
    def literal(v):
        return 'NULL' if v is None else str(v) if type(v) is int else "'" + v.replace("'", "''") + "'"
    query = ' UNION ALL '.join('SELECT '+','.join(literal(v)+' AS '+c for c,v in zip(case.columns,row))
                               for row in case.hand_expected)
    assert sql_catalog.contract(case.id).environment().validate(json.dumps({'sql':query}))['passed'] is False


@pytest.mark.parametrize('unverifiable', [False,True])
def test_new_policy_final_failure_or_unknown_never_passes_or_generates_a_third_answer(tmp_path,unverifiable):
    task=sql_catalog.contract('S01')
    if unverifiable:
        spec=json.loads(task._public_json)
        spec['samples']=[]
        task=SqlContract(**spec)
    api=API(messages=[answer('SELECT 0 AS total'),answer('SELECT 0 AS total')])
    result=run_sql(sql_core(tmp_path,api),'new-final-status',task)
    assert result['status']==('unverifiable' if unverifiable else 'failed')
    assert len(result['attempts'])==2 and len(api.requests)==3
    assert result['costs']['api_usd']=='0.0201'
