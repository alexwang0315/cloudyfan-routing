"""Independent finite SQL semantic evidence, separate from model selection."""
import json

import pytest

from cloudyfan_jev.sql_review import cases, datasets, review
from test_sql_environment import answer, sql_core
from test_engine import API
from cloudyfan_jev.sql import run_sql


def test_entire_public_catalog_references_hand_expectations_and_wrong_queries():
    report = review()
    assert len(report['cases']) == 8
    assert all(r['public_samples'] == 5 and r['hand_example_agrees']
               and r['reference_passes'] and r['mutants_rejected'] == r['deliberate_mutants']
               for r in report['cases'])
    assert report['model_calls'] == 0 and report['human_review'] == 'pending'


@pytest.mark.parametrize('case', cases(), ids=lambda c: c.id)
def test_catalog_contract_excludes_reference_oracle_mutant_and_private_grade(case):
    task = case.contract().task()
    assert case.reference_sql not in task['prompt']
    assert set(task) == {'prompt', 'sql_contract_version'}
    public = json.loads(case.contract()._public_json)
    assert set(public) == {'version', 'prompt', 'tables', 'expected_columns', 'samples'}
    assert all(set(sample) == {'id', 'tables', 'expected'} for sample in public['samples'])
    assert 'hidden' not in public and 'oracle' not in public and 'mutants' not in public


@pytest.mark.parametrize('case', cases(), ids=lambda c: c.id)
def test_constant_answer_from_first_sample_is_rejected_on_different_public_inputs(case):
    rows = case.oracle(datasets()[0])

    def literal(v):
        if v is None:
            return 'NULL'
        return str(v) if type(v) is int else "'" + v.replace("'", "''") + "'"

    sql = ' UNION ALL '.join('SELECT ' + ','.join(literal(v) + ' AS ' + col
                                                 for col, v in zip(case.columns, row)) for row in rows)
    assert case.contract().environment().validate(json.dumps({'sql': sql}))['passed'] is False


def test_catalog_flow_repairs_against_public_contract_without_grading_feedback(tmp_path):
    case = next(c for c in cases() if c.id == 'S04')
    api = API(messages=[answer(case.mutants[0]), answer(case.reference_sql)])
    result = run_sql(sql_core(tmp_path, api), 'catalog-left-join', case.contract())
    assert result['status'] == 'passed' and len(result['attempts']) == 2
    assert result['attempts'][0]['public']['checks'] == ['SQL_PUBLIC_RESULT_MISMATCH']
    assert len(api.requests) == 3
    assert run_sql(sql_core(tmp_path, api), 'catalog-left-join', case.contract()) == result
    assert len(api.requests) == 3


def test_independent_oracles_define_empty_and_all_null_semantics():
    data = datasets()
    by_id = {c.id: c for c in cases()}
    assert by_id['S01'].oracle(data[2]) == [[None]]
    assert by_id['S01'].oracle(data[4]) == [[None]]
    assert by_id['S04'].oracle(data[3]) == [[4, 0]]
    assert by_id['S03'].oracle(data[4]) == [[5, 1, None]]
    assert by_id['S06'].oracle(data[4]) == [[5, 30]]
    assert by_id['S07'].oracle(data[4]) == [[5, 30, 0]]
