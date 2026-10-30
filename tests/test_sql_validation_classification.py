"""Model-caused SQL errors stay public failures; result order is actually exercised."""
import json
import re
import sqlite3

import pytest

from cloudyfan_jev import sql_catalog, sql_executor
from cloudyfan_jev.scores import sql_exam
from cloudyfan_jev.sql import SqlValidationUnavailable, run_sql
from cloudyfan_jev.sql_review import cases as review_cases
from test_engine import API
from test_sql_environment import answer, contract, sql_core


def _overflowing():
    return contract(samples=[{'id': 'big', 'tables': {'numbers': [{'n': 2**63 - 1}, {'n': 1}]},
                              'expected': [[0]]}])


MODEL_ERRORS = [
    ('SELECT n AS total FROM numbers ORDER BY 3', contract, 'SQL_EXECUTION_ERROR:syntax_error'),
    ('SELECT n AS total FROM numbers GROUP BY 5', contract, 'SQL_EXECUTION_ERROR:syntax_error'),
    ('SELECT n AS total FROM numbers UNION SELECT n, n FROM numbers', contract,
     'SQL_EXECUTION_ERROR:compound_column_mismatch'),
    ('SELECT substr(n) AS total FROM numbers', contract, 'SQL_EXECUTION_ERROR:wrong_argument_count'),
    ("SELECT 'abc AS total", contract, 'SQL_EXECUTION_ERROR:syntax_error'),
    ('SELECT SUM(n) AS total FROM numbers', _overflowing, 'SQL_EXECUTION_ERROR:integer_overflow'),
    ('SELECT abs(-9223372036854775808) AS total', contract, 'SQL_EXECUTION_ERROR:integer_overflow'),
    ('SELECT nth_value(n, 0) OVER () AS total FROM numbers', contract, 'SQL_EXECUTION_ERROR:execution_error'),
    ("SELECT n AS total FROM numbers LIMIT 'x'", contract, 'SQL_EXECUTION_ERROR:execution_error'),
    ("SELECT CAST(x'ff' AS TEXT) AS total", contract, 'SQL_RESULT_TYPES'),
]


@pytest.mark.parametrize('sql,build,check', MODEL_ERRORS, ids=[m[0][:36] for m in MODEL_ERRORS])
def test_deterministic_sql_mistakes_are_public_failures_not_engineering(sql, build, check):
    result = build().environment().validate(json.dumps({'sql': sql}))
    assert result['passed'] is False
    assert check in result['checks']
    # Fixed categories only: no SQLite message, identifier or sample value is echoed.
    assert all(re.fullmatch(r'[A-Z_]+(:[a-z_]+)?', item) for item in result['checks'])


def test_order_term_mistake_reaches_the_sequential_repair_and_resume_does_not_resend(tmp_path):
    good = 'SELECT SUM(n) AS total FROM numbers'
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers ORDER BY 3'), answer(good)])
    engine = sql_core(tmp_path, api)
    task = contract()
    result = run_sql(engine, 'order-term', task)
    assert result['status'] == 'passed' and len(result['attempts']) == 2
    assert result['first_public_pass'] is False and result['final_public_pass'] is True
    assert result['attempts'][0]['public']['checks'] == ['SQL_EXECUTION_ERROR:syntax_error']
    assert len(api.requests) == 3
    assert run_sql(engine, 'order-term', task) == result
    assert len(api.requests) == 3


@pytest.mark.parametrize('message,code', [
    ('parser stack overflow', sqlite3.SQLITE_ERROR),
    ('disk I/O error', sqlite3.SQLITE_IOERR),
    ('out of memory', sqlite3.SQLITE_NOMEM),
    ('database is locked', sqlite3.SQLITE_BUSY),
    ('something new', sqlite3.SQLITE_INTERNAL),
    ('no code at all', None),
])
def test_build_or_host_dependent_failures_remain_engineering(message, code):
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    assert sql_executor._is_model_sql_error(error) is False


@pytest.mark.parametrize('message', [
    'You can only execute one statement at a time.',
    'Incorrect number of bindings supplied. The current statement uses 1, and there are 0 supplied.',
])
def test_known_errors_without_a_sqlite_code_stay_model_failures(message):
    assert sql_executor._is_model_sql_error(sqlite3.ProgrammingError(message)) is True


def test_multiple_statements_and_bound_parameters_keep_their_public_categories():
    env = contract().environment()
    assert env.validate(json.dumps({'sql': 'SELECT 1 AS total; SELECT 2'}))['checks'] == [
        'SQL_EXECUTION_ERROR:multiple_statements']
    assert env.validate(json.dumps({'sql': 'SELECT ? AS total'}))['checks'] == [
        'SQL_EXECUTION_ERROR:bound_parameters_forbidden']


def test_memory_error_policy_is_unchanged():
    # Each CTE inlines the previous one twice, so this compiles past max_compiled_vm_operations.
    terms = ['c0 AS (SELECT n FROM numbers)'] + [
        f'c{i} AS (SELECT n FROM c{i-1} UNION ALL SELECT n FROM c{i-1})' for i in range(1, 14)]
    sql = 'WITH ' + ', '.join(terms) + ' SELECT n AS total FROM c13'
    with pytest.raises(SqlValidationUnavailable) as caught:
        contract().environment().validate(json.dumps({'sql': sql}))
    assert caught.value.code == 'SQL_VALIDATION_ENVIRONMENT'


@pytest.mark.parametrize('sql,expected', [
    ('SELECT a FROM t ORDER BY a', True),
    ('select a from t\n order\n  by a', True),
    ('SELECT a FROM t ORDER /* c */ BY a', True),
    ('SELECT a FROM t ORDER -- c\n BY a', True),
    ('SELECT a FROM t UNION SELECT a FROM u ORDER BY 1', True),
    ('WITH c AS (SELECT a FROM t ORDER BY a) SELECT a FROM c ORDER BY a', True),
    ('SELECT a FROM t', False),
    ('SELECT a FROM (SELECT a FROM t ORDER BY a)', False),
    ('WITH c AS (SELECT a FROM t ORDER BY a) SELECT a FROM c', False),
    ('SELECT rank() OVER (ORDER BY a) AS r FROM t', False),
    ('SELECT rank() OVER w AS r FROM t WINDOW w AS (ORDER BY a)', False),
    ("SELECT 'ORDER BY a' AS x FROM t", False),
    ("SELECT 'it''s ORDER BY' AS x FROM t", False),
    ('SELECT "ORDER BY" FROM t', False),
    ('SELECT [ORDER BY] FROM t', False),
    ('SELECT `ORDER BY` FROM t', False),
    ('SELECT a FROM t -- ORDER BY a', False),
    ('SELECT a FROM t /* ORDER BY a */', False),
    ('SELECT a AS "ORDER" FROM t', False),
    ('SELECT a AS reorder, b AS by FROM t', False),
    ('SELECT a FROM t AS ORDER\u00a0BY', False),
    ('SELECT a FROM t AS ORDER\u2003BY', False),
    ('SELECT a FROM t AS \u00e9ORDER BY', False),
    ('SELECT a FROM t AS ORDER\u00e9 BY', False),
])
def test_top_level_order_by_detection(sql, expected):
    assert sql_executor.has_top_level_order_by(sql) is expected


def _ordered_contract(**changes):
    return contract(expected_columns=['n'], samples=[{
        'id': 'ordered', 'tables': {'numbers': [{'n': 2}, {'n': 1}]}, 'expected': [[1], [2]]}], **changes)


def test_unicode_space_inside_an_identifier_does_not_count_as_order_by():
    env = _ordered_contract().environment()
    sql = 'SELECT n FROM numbers AS ORDER\u00a0BY'
    assert env.validate(json.dumps({'sql': sql}, ensure_ascii=False))['passed'] is False


def test_missing_order_by_fails_when_distinct_rows_make_order_observable():
    env = _ordered_contract().environment()
    result = env.validate(json.dumps({'sql': 'SELECT DISTINCT n FROM numbers GROUP BY n'}))
    assert result['passed'] is False and 'SQL_ORDER_BY_REQUIRED' in result['checks']
    assert env.validate(json.dumps({'sql': 'SELECT n FROM numbers ORDER BY n'})) == {'passed': True, 'checks': []}


@pytest.mark.parametrize('rows,expected', [
    ([{'n': 1}], [[1]]),
    ([{'n': 1}, {'n': 1}], [[1], [1]]),
    ([], []),
])
def test_order_by_is_not_required_when_every_order_is_equivalent(rows, expected):
    task = contract(expected_columns=['n'], samples=[{
        'id': 'same', 'tables': {'numbers': rows}, 'expected': expected}])
    assert task.environment().validate(json.dumps({'sql': 'SELECT n FROM numbers'})) == {
        'passed': True, 'checks': []}


def test_single_row_review_case_does_not_require_order_by():
    case = next(c for c in review_cases() if c.id == 'S01')
    assert sql_catalog.contract('S01').environment().validate(
        json.dumps({'sql': case.reference_sql}))['passed'] is True


def _without_final_order_by(sql):
    index = sql.upper().rfind(' ORDER BY ')
    assert index > 0 and not sql_executor.has_top_level_order_by(sql[:index])
    return sql[:index]


@pytest.mark.parametrize('case', sql_exam.cases(), ids=lambda c: c.id)
def test_exam_rejects_each_reference_with_its_final_order_by_removed(case):
    reference = json.loads(case.answer())['sql']
    mutant = json.dumps({'sql': _without_final_order_by(reference)})
    sealed = case.contract(grading=True).environment()
    assert sealed.validate(case.answer())['passed'] is True
    assert sealed.validate(mutant)['passed'] is False


@pytest.mark.parametrize('case', [c for c in review_cases() if c.id != 'S01'], ids=lambda c: c.id)
def test_bounded_catalog_rejects_each_reference_with_its_final_order_by_removed(case):
    env = sql_catalog.contract(case.id).environment()
    assert env.validate(json.dumps({'sql': case.reference_sql}))['passed'] is True
    mutant = _without_final_order_by(case.reference_sql)
    assert env.validate(json.dumps({'sql': mutant}))['passed'] is False


def test_published_constraints_tell_the_model_about_result_order():
    constraints = sql_executor._execution_constraints()
    assert constraints['contract_version'] == 'sqlite-readonly-contract-v3'
    assert constraints['top_level_order_by_required_when_row_order_is_observable'] is True
    assert 'top_level_order_by_required_when_row_order_is_observable' in contract().task()['prompt']
