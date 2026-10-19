"""SQL public validation through Core.run with real SQLite and mocked model I/O."""
import json
import sqlite3

import pytest

from cloudyfan_jev.demo import example_config
from cloudyfan_jev.engine import Core
from cloudyfan_jev.sql import SQL_DIAGNOSTICS, SqlContract, SqlValidationUnavailable, run_sql
from test_engine import API, core


def contract(**changes):
    values = {
        'version': 'sum-public-v1', 'prompt': 'Sum all numbers, returning column total.',
        'tables': {'numbers': [('n', 'INTEGER')]}, 'expected_columns': ['total'],
        'samples': [{'id': 'public-1', 'tables': {'numbers': [{'n': 1}, {'n': 2}]},
                     'expected': [[3]]}],
    }
    values.update(changes)
    return SqlContract(**values)


def sql_core(tmp_path, api):
    base = core(tmp_path, api)
    profiles, decision = example_config()
    return Core(base.client, profiles, decision, diagnostic_contract=SQL_DIAGNOSTICS)


def answer(sql):
    return {'content': json.dumps({'sql': sql})}


def test_public_sql_pass_skips_classification_and_reuses_answer(tmp_path):
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])
    engine = sql_core(tmp_path, api)
    task = contract()
    result = run_sql(engine, 'sql-pass', task)
    assert result['status'] == 'passed' and result['selection'] is None
    assert result['attempts'][0]['public']['diagnostic_source'] == 'sql-environment-v1'
    assert run_sql(engine, 'sql-pass', task) == result
    assert len(api.requests) == 1 and 'tools' not in api.requests[0]


@pytest.mark.parametrize('failure', [OSError, MemoryError, sqlite3.OperationalError])
def test_validator_engineering_interruption_retains_answer_and_resumes_without_resend(tmp_path, monkeypatch, failure):
    native_connect = sqlite3.connect
    fail = False
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])

    def endpoint(request):
        nonlocal fail
        response = api(request)
        fail = True
        return response

    def connect(*args, **kwargs):
        if fail:
            raise failure('PRIVATE_HOST_DETAIL')
        return native_connect(*args, **kwargs)

    task = contract()
    monkeypatch.setattr(sqlite3, 'connect', connect)
    engine = sql_core(tmp_path, endpoint)
    with pytest.raises(SqlValidationUnavailable) as caught:
        run_sql(engine, 'validator-interrupted', task)
    assert caught.value.category == 'engineering'
    assert 'PRIVATE_HOST_DETAIL' not in str(caught.value)
    assert len(api.requests) == 1
    fail = False
    result = run_sql(engine, 'validator-interrupted', task)
    assert result['status'] == 'passed' and result['resumed']
    assert result['costs']['api_usd'] == '0.01' and len(api.requests) == 1


def test_unknown_column_is_a_public_repair_code_without_raw_sql_error(tmp_path):
    api = API(messages=[answer('SELECT PRIVATE_COLUMN FROM numbers'),
                        answer('SELECT SUM(n) AS total FROM numbers')])
    result = run_sql(sql_core(tmp_path, api), 'column-repair', contract())
    assert result['status'] == 'passed' and len(result['attempts']) == 2
    first = result['attempts'][0]['public']
    assert first['passed'] is False
    assert first['checks'] == ['SQL_EXECUTION_ERROR:unknown_column']
    assert 'PRIVATE_COLUMN' not in json.dumps(first)
    assert len(api.requests) == 3 and result['costs']['api_usd'] == '0.0201'


def test_invalid_fixture_stops_before_any_model_request(tmp_path):
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])
    task = contract(tables={'numbers': [('n', 'INTEGER PRIMARY KEY')]}, samples=[
        {'id': 'invalid-public', 'tables': {'numbers': [{'n': 1}, {'n': 1}]}, 'expected': [[2]]}])
    with pytest.raises(SqlValidationUnavailable):
        run_sql(sql_core(tmp_path, api), 'invalid-fixture', task)
    assert not api.requests


def test_sqlite_build_cannot_silently_lower_contract_limit_before_dispatch(tmp_path, monkeypatch):
    task = contract()
    native = sqlite3.connect

    class LowerLimit:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def getlimit(self, kind):
            if kind == sqlite3.SQLITE_LIMIT_COMPOUND_SELECT:
                return 1
            return self.connection.getlimit(kind)

    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: LowerLimit(native(*a, **k)))
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])
    with pytest.raises(SqlValidationUnavailable):
        run_sql(sql_core(tmp_path, api), 'incompatible-build', task)
    assert not api.requests


@pytest.mark.parametrize('text', [None, '', 'not json', '{"sql":"SELECT 1","sql":"SELECT 2"}',
                                 '{"sql":"SELECT 1","hidden_answer":"PRIVATE"}',
                                 '{"sql":true}', '[]', '"\\ud800"', 'x' * 32769])
def test_invalid_or_exhausted_response_receives_only_one_public_repair(tmp_path, text):
    api = API(messages=[{'content': text}, answer('SELECT SUM(n) AS total FROM numbers')])
    result = run_sql(sql_core(tmp_path, api), 'parse-repair', contract())
    first = result['attempts'][0]['public']
    assert result['status'] == 'passed' and first['passed'] is False
    assert set(first) == {'passed', 'checks', 'diagnostic_version', 'diagnostic_source'}
    assert first['checks'][0] in {'SQL_RESPONSE_JSON', 'SQL_RESPONSE_SCHEMA', 'SQL_RESPONSE_LIMIT'}
    assert 'PRIVATE' not in json.dumps(first) and len(api.requests) == 3


@pytest.mark.parametrize('query', [
    'DELETE FROM numbers', 'SELECT * FROM sqlite_master',
    "SELECT load_extension('PRIVATE_PATH')", "SELECT readfile('PRIVATE_PATH')",
    'SELECT 1; DELETE FROM numbers',
    'WITH x AS (SELECT 1) DELETE FROM numbers',
    'WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT n AS total FROM x',
])
def test_unsafe_sql_can_fail_both_attempts_without_becoming_engineering_or_success(tmp_path, query):
    api = API(messages=[answer(query), answer(query)])
    result = run_sql(sql_core(tmp_path, api), 'unsafe', contract())
    assert result['status'] == 'failed' and len(result['attempts']) == 2
    assert all(a['public']['passed'] is False for a in result['attempts'])
    assert 'PRIVATE_PATH' not in json.dumps([a['public'] for a in result['attempts']])
    assert len(api.requests) == 3


def test_no_public_samples_is_unverifiable_after_the_same_bounded_flow(tmp_path):
    api = API(messages=[answer('SELECT 1 AS total'), answer('SELECT 1 AS total')])
    result = run_sql(sql_core(tmp_path, api), 'unverifiable', contract(samples=[]))
    assert result['status'] == 'unverifiable'
    assert result['first_public_pass'] is None and result['final_public_pass'] is None
    assert result['attempts'][0]['public']['checks'] == ['SQL_NO_PUBLIC_SAMPLES']
    assert len(api.requests) == 3


def test_public_contract_is_detached_and_changed_expected_rows_cannot_reuse_run(tmp_path):
    samples = [{'id': 'public-1', 'tables': {'numbers': [{'n': 1}, {'n': 2}]}, 'expected': [[3]]}]
    task = contract(samples=samples)
    frozen = task.task()
    samples[0]['expected'][0][0] = 4
    assert task.task() == frozen
    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])
    engine = sql_core(tmp_path, api)
    assert run_sql(engine, 'frozen-sql', task)['status'] == 'passed'
    with pytest.raises(ValueError, match='Resume evidence changed'):
        run_sql(engine, 'frozen-sql', contract(samples=samples))
    assert len(api.requests) == 1


@pytest.mark.parametrize('field,value', [
    ('samples', [{'id': 'public', 'tables': {'numbers': []}, 'expected': [], 'hidden': 'PRIVATE'}]),
    ('samples', [{'id': 'public', 'tables': {'numbers': [{'n': True}]}, 'expected': [[1]]}]),
    ('samples', [{'id': 'public', 'tables': {'numbers': [{'n': 2**80}]}, 'expected': [[1]]}]),
    ('expected_columns', ['total', 'total']), ('tables', {'numbers;DROP TABLE x': [('n', 'INTEGER')]}),
])
def test_invalid_or_hidden_contract_fields_are_rejected_before_dispatch(field, value):
    with pytest.raises(ValueError, match='Invalid public SQL contract') as caught:
        contract(**{field: value})
    assert 'PRIVATE' not in str(caught.value)


def test_unmatched_diagnostic_contract_is_rejected_before_dispatch(tmp_path):
    api = API()
    with pytest.raises(ValueError, match='diagnostic contract'):
        run_sql(core(tmp_path, api), 'bad-diagnostics', contract())
    assert not api.requests


def test_compound_limit_is_public_and_can_be_repaired_without_changing_limits(tmp_path):
    def query(n):
        return ' UNION ALL '.join('SELECT 1 AS total' for _ in range(n))
    task = contract(samples=[{'id': 'public', 'tables': {'numbers': []}, 'expected': [[1]] * 20}])
    api = API(messages=[answer(query(21)), answer(query(20))])
    result = run_sql(sql_core(tmp_path, api), 'compound-limit', task)
    assert result['status'] == 'passed'
    assert result['attempts'][0]['public']['checks'] == ['SQL_RESOURCE_LIMIT:compound_select_limit']
    assert len(api.requests) == 3


def test_nulls_duplicates_and_order_are_checked_against_public_rows(tmp_path):
    task = contract(expected_columns=['n'], samples=[{
        'id': 'public', 'tables': {'numbers': [{'n': 2}, {'n': None}, {'n': 2}, {'n': 1}]},
        'expected': [[None], [1], [2], [2]],
    }])
    api = API(messages=[answer('SELECT DISTINCT n FROM numbers ORDER BY n DESC'),
                        answer('SELECT n FROM numbers ORDER BY n ASC')])
    result = run_sql(sql_core(tmp_path, api), 'public-order', task)
    assert result['attempts'][0]['public']['checks'] == ['SQL_PUBLIC_RESULT_MISMATCH']
    assert result['status'] == 'passed'


@pytest.mark.parametrize('query,code', [
    ('SELECT 3 AS other', 'SQL_RESULT_COLUMNS'),
    ('SELECT 3.0 AS total', 'SQL_RESULT_TYPES'),
    ('SELECT 4 AS total', 'SQL_PUBLIC_RESULT_MISMATCH'),
])
def test_result_shape_type_and_value_failures_remain_model_failures(tmp_path, query, code):
    api = API(messages=[answer(query), answer('SELECT 3 AS total')])
    result = run_sql(sql_core(tmp_path, api), 'result-failure', contract())
    assert result['attempts'][0]['public']['checks'] == [code]
    assert result['status'] == 'passed'


def test_public_diagnostics_stay_flat_bounded_and_do_not_echo_sample_contents(tmp_path):
    samples = [{'id': f'public-{i}', 'tables': {'numbers': [{'n': i}]}, 'expected': [[i]]}
               for i in range(32)]
    api = API(messages=[answer('SELECT PRIVATE_BAD_COLUMN FROM numbers'),
                        answer('SELECT n AS total FROM numbers')])
    result = run_sql(sql_core(tmp_path, api), 'bounded-diagnostic', contract(samples=samples))
    public = result['attempts'][0]['public']
    assert public['checks'] == ['SQL_EXECUTION_ERROR:unknown_column']
    assert len(json.dumps(public, ensure_ascii=False).encode()) <= 4096
    assert 'PRIVATE_BAD_COLUMN' not in json.dumps(public)
    assert all(type(check) is str for check in public['checks'])


def test_budgeted_core_keeps_full_cost_and_releases_reservation_only_after_sql_pass(tmp_path):
    from datetime import datetime, timezone
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    api = API(messages=[answer('SELECT 4 AS total'), answer('SELECT 3 AS total')])
    engine = sql_core(tmp_path / 'client', api)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
    store.authorize(Authorization('offline-only-fixture', '1', '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'synthetic') for p in engine.profiles.values()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'synthetic')
    controlled = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    result = run_sql(controlled, 'budget-sql', contract())
    assert result['status'] == 'passed' and result['budget']['run_total_usd'] == '0.0201'
    assert store.report()['known_usd'] == '0.0201' and store.report()['reserved_usd'] == '0'
    run_sql(controlled, 'budget-sql', contract())
    assert len(api.requests) == 3 and store.report()['known_usd'] == '0.0201'


def test_changed_runtime_contract_stops_before_model_dispatch(tmp_path, monkeypatch):
    from cloudyfan_jev import sql_executor
    task = contract()
    monkeypatch.setattr(sql_executor, 'MAX_VM_STEPS', 200000)
    api = API()
    with pytest.raises(SqlValidationUnavailable) as caught:
        run_sql(sql_core(tmp_path, api), 'changed-runtime', task)
    assert caught.value.code == 'SQL_VALIDATION_RUNTIME_CHANGED' and not api.requests


def test_validator_deadline_is_an_engineering_interruption_not_paid_repair(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from cloudyfan_jev import sql_executor
    native_clock = sql_executor.time.monotonic
    after_answer = False
    ticks = 0

    def clock():
        nonlocal ticks
        if not after_answer:
            return native_clock()
        ticks += 1
        return float(ticks)

    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])

    def endpoint(request):
        nonlocal after_answer
        response = api(request)
        after_answer = True
        return response

    monkeypatch.setattr(sql_executor, 'time', SimpleNamespace(monotonic=clock))
    with pytest.raises(SqlValidationUnavailable) as caught:
        run_sql(sql_core(tmp_path, endpoint), 'deadline', contract())
    assert caught.value.code == 'SQL_VALIDATION_TIMEOUT' and len(api.requests) == 1


def test_vm_budget_failure_can_repair_but_never_relaxes_the_public_limit(tmp_path):
    task = contract(samples=[{'id': 'public', 'tables': {'numbers': [{'n': i} for i in range(10)]},
                             'expected': [[45]]}])
    api = API(messages=[answer('SELECT COUNT(*) AS total FROM numbers a, numbers b, numbers c, numbers d, numbers e'),
                        answer('SELECT SUM(n) AS total FROM numbers')])
    result = run_sql(sql_core(tmp_path, api), 'vm-limit', task)
    assert result['attempts'][0]['public']['checks'] == ['SQL_RESOURCE_LIMIT:vm_step_limit']
    assert result['status'] == 'passed' and len(api.requests) == 3


def test_output_row_limit_has_a_public_code_and_can_be_repaired(tmp_path):
    task = contract(prompt='Return first 100 numbers in n order.', expected_columns=['n'], samples=[{
        'id': 'public', 'tables': {'numbers': [{'n': i} for i in range(101)]},
        'expected': [[i] for i in range(100)],
    }])
    api = API(messages=[answer('SELECT n FROM numbers ORDER BY n'),
                        answer('SELECT n FROM numbers ORDER BY n LIMIT 100')])
    result = run_sql(sql_core(tmp_path, api), 'row-limit', task)
    assert result['attempts'][0]['public']['checks'] == ['SQL_OUTPUT_LIMIT:output_row_limit']
    assert result['status'] == 'passed'


def test_unknown_sqlite_error_does_not_guess_that_the_model_was_wrong(tmp_path):
    api = API(messages=[answer('SELECT abs(-9223372036854775808) AS total')])
    with pytest.raises(SqlValidationUnavailable):
        run_sql(sql_core(tmp_path, api), 'unknown-sqlite-error', contract())
    assert len(api.requests) == 1


def test_engineering_interruption_keeps_budget_hold_and_resume_does_not_pay_again(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from cloudyfan_jev import sql_executor
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    native_clock = sql_executor.time.monotonic
    interrupted, ticks = False, 0

    def clock():
        nonlocal ticks
        if not interrupted:
            return native_clock()
        ticks += 1
        return float(ticks)

    api = API(messages=[answer('SELECT SUM(n) AS total FROM numbers')])

    def endpoint(request):
        nonlocal interrupted
        response = api(request)
        interrupted = True
        return response

    engine = sql_core(tmp_path / 'client', endpoint)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))
    store.authorize(Authorization('offline-only-fixture', '1', '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'synthetic') for p in engine.profiles.values()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'synthetic')
    controlled = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    task = contract()
    monkeypatch.setattr(sql_executor, 'time', SimpleNamespace(monotonic=clock))
    with pytest.raises(SqlValidationUnavailable):
        run_sql(controlled, 'held-sql', task)
    assert store.report()['known_usd'] == '0.01' and store.report()['reserved_usd'] != '0'
    interrupted = False
    result = run_sql(controlled, 'held-sql', task)
    assert result['status'] == 'passed' and store.report()['reserved_usd'] == '0'
    assert store.report()['known_usd'] == '0.01' and len(api.requests) == 1


@pytest.mark.parametrize('choices,tier', [(('mid', 'mid'), 'mid'), (('mid', 'high'), 'high')])
def test_sql_diagnostics_survive_both_binary_decisions_before_one_repair(tmp_path, choices, tier):
    api = API(choices=choices, messages=[answer('SELECT 4 AS total'),
                                       answer('SELECT SUM(n) AS total FROM numbers')])
    result = run_sql(sql_core(tmp_path, api), 'two-decisions', contract())
    assert result['status'] == 'passed'
    assert [a['tier'] for a in result['attempts']] == ['open_source', tier]
    assert result['selection']['classifier_calls'] == 2 and len(api.requests) == 4
    assert result['costs']['api_usd'] == '0.0202'


def test_finite_public_examples_do_not_prove_general_sql_correctness(tmp_path):
    # This intentionally demonstrates the remaining qualification boundary.
    api = API(messages=[answer('SELECT 3 AS total')])
    result = run_sql(sql_core(tmp_path, api), 'finite-public-only', contract())
    assert result['status'] == 'passed' and result['selection'] is None
    assert len(api.requests) == 1
