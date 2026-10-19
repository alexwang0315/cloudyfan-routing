"""Synthetic public-only regression cases; no historical/hidden test imports."""
from copy import deepcopy
import json

import pytest

from cloudyfan_jev.sql import SqlContract, run_sql
from cloudyfan_jev.sql_public_suite import build_public_sql_contract
from test_engine import API
from test_sql_environment import answer, sql_core


TABLES = {
    'accounts': [('id', 'INTEGER PRIMARY KEY'), ('label', 'TEXT')],
    'access': [('account_id', 'INTEGER'), ('actor', 'TEXT'), ('role', 'TEXT')],
    'payments': [('account_id', 'INTEGER'), ('units', 'INTEGER')],
}
BASE = {'id': 'ordinary', 'tables': {
    'accounts': [{'id': 1, 'label': ' A '}, {'id': 2, 'label': 'a'}],
    'access': [{'account_id': 1, 'actor': 'u', 'role': 'owner'},
               {'account_id': 1, 'actor': 'u', 'role': 'editor'}],
    'payments': [{'account_id': 1, 'units': 2}, {'account_id': 2, 'units': 3}],
}}
EDGE = {'id': 'relational-edges', 'tables': {
    'accounts': [{'id': 0, 'label': None}, {'id': -1, 'label': None},
                 {'id': 1, 'label': ' A '}, {'id': 2, 'label': 'a'}],
    'access': [{'account_id': 1, 'actor': 'u', 'role': 'owner'},
               {'account_id': 1, 'actor': 'u', 'role': 'owner'},
               {'account_id': 1, 'actor': 'u', 'role': 'editor'},
               {'account_id': 99, 'actor': 'v', 'role': 'owner'},
               {'account_id': 99, 'actor': 'v', 'role': 'editor'},
               {'account_id': 0, 'actor': None, 'role': 'owner'},
               {'account_id': 0, 'actor': None, 'role': 'editor'}],
    'payments': [{'account_id': 1, 'units': -7}, {'account_id': 2, 'units': None},
                 {'account_id': 99, 'units': 20}],
}}
EMPTY = {'id': 'empty', 'tables': {table: [] for table in TABLES}}
RULES = [
    {'kind': 'empty_table', 'table': 'payments'},
    {'kind': 'null_value', 'table': 'payments', 'column': 'units'},
    {'kind': 'duplicate_key', 'table': 'access', 'columns': ['account_id', 'actor', 'role']},
    {'kind': 'orphan_reference', 'table': 'access', 'column': 'account_id',
     'parent_table': 'accounts', 'parent_column': 'id'},
    {'kind': 'parent_without_children', 'table': 'payments', 'column': 'account_id',
     'parent_table': 'accounts', 'parent_column': 'id'},
]
EXISTS = """SELECT a.id FROM accounts a WHERE EXISTS(
 SELECT 1 FROM access o JOIN access e ON o.account_id=e.account_id AND o.actor=e.actor
 WHERE o.account_id=a.id AND o.role='owner' AND e.role='editor') ORDER BY a.id"""
GROUP = """SELECT DISTINCT a.id FROM accounts a JOIN access x ON x.account_id=a.id
 WHERE x.actor IS NOT NULL AND x.role IN('owner','editor') GROUP BY a.id,x.actor
 HAVING count(DISTINCT x.role)=2 ORDER BY a.id"""
AGGREGATE = """WITH x AS(SELECT a.id,coalesce(lower(trim(a.label)),'(none)') AS label,
 coalesce(sum(abs(p.units)),0) AS spent FROM accounts a
 LEFT JOIN payments p ON p.account_id=a.id GROUP BY a.id)
 SELECT label,count(*) AS n,sum(spent) AS spent FROM x
 GROUP BY label HAVING count(*)>=2 ORDER BY label"""
BAD_DUP = """SELECT a.id FROM accounts a JOIN access o ON o.account_id=a.id
 JOIN access e ON e.account_id=o.account_id AND e.actor=o.actor
 WHERE o.role='owner' AND e.role='editor' ORDER BY a.id"""
BAD_ORPHAN = """SELECT DISTINCT o.account_id AS id FROM access o JOIN access e
 ON e.account_id=o.account_id AND e.actor=o.actor
 WHERE o.role='owner' AND e.role='editor' ORDER BY id"""
BAD_AGG = """SELECT coalesce(lower(trim(a.label)),'(none)') AS label,
 count(DISTINCT a.id) AS n,sum(abs(p.units)) AS spent FROM accounts a
 JOIN payments p ON p.account_id=a.id WHERE p.units IS NOT NULL
 GROUP BY coalesce(lower(trim(a.label)),'(none)') HAVING count(DISTINCT a.id)>=2 ORDER BY label"""


def membership(data):
    owners = {(r['account_id'], r['actor']) for r in data['access']
              if r['actor'] is not None and r['role'] == 'owner'}
    editors = {(r['account_id'], r['actor']) for r in data['access']
               if r['actor'] is not None and r['role'] == 'editor'}
    matched = {account for account, _ in owners & editors}
    return [[a['id']] for a in sorted(data['accounts'], key=lambda a: a['id']) if a['id'] in matched]


def aggregation(data):
    groups = {}
    for account in data['accounts']:
        label = '(none)' if account['label'] is None else account['label'].strip(' ').lower()
        n, spent = groups.get(label, (0, 0))
        groups[label] = (n + 1, spent + sum(abs(row['units'] or 0) for row in data['payments']
                                         if row['account_id'] == account['id']))
    return [[label, n, spent] for label, (n, spent) in sorted(groups.items()) if n >= 2]


def suite(**changes):
    kwargs = dict(version='synthetic-v1', prompt='Return existing account IDs with both owner and editor for one non-NULL actor; unique, ascending.',
                  tables=TABLES, expected_columns=['id'], public_cases=deepcopy([BASE, EDGE, EMPTY]),
                  oracle=membership, oracle_version='independent-set-oracle-v1',
                  corpus_version='synthetic-relations-v1', required_coverage=deepcopy(RULES))
    kwargs.update(changes)
    return build_public_sql_contract(**kwargs)


@pytest.mark.parametrize('bad,good,oracle,columns', [
    (BAD_DUP, EXISTS, membership, ['id']),
    (BAD_ORPHAN, GROUP, membership, ['id']),
    (BAD_AGG, AGGREGATE, aggregation, ['label', 'n', 'spent']),
])
def test_general_edges_reject_example_overfitting_without_query_text_rules(bad, good, oracle, columns):
    weak = SqlContract(version='weak', prompt='Public synthetic relational calculation.', tables=TABLES,
                       expected_columns=columns, samples=[{**deepcopy(BASE), 'expected': oracle(BASE['tables'])}])
    assert weak.environment().validate(json.dumps({'sql': bad}))['passed'] is True
    task = suite(oracle=oracle, expected_columns=columns)
    assert task.environment().validate(json.dumps({'sql': bad})) == {
        'passed': False, 'checks': ['SQL_PUBLIC_RESULT_MISMATCH']}
    assert task.environment().validate(json.dumps({'sql': good}))['passed'] is True


def test_public_suite_activates_existing_binary_repair_and_replay_bound(tmp_path):
    api = API(choices=('mid', 'high'), messages=[answer(BAD_ORPHAN), answer(EXISTS)])
    engine = sql_core(tmp_path, api)
    task = suite()
    result = run_sql(engine, 'qualified-public', task)
    assert result['status'] == 'passed' and result['first_public_pass'] is False
    assert len(result['attempts']) == 2 and result['selection']['classifier_calls'] == 2
    assert result['costs']['api_usd'] == '0.0202' and len(api.requests) == 4
    assert run_sql(engine, 'qualified-public', task) == result and len(api.requests) == 4
    public = result['attempts'][0]['public']
    assert public['checks'] == ['SQL_PUBLIC_RESULT_MISMATCH']
    assert set(public) == {'passed', 'checks', 'diagnostic_version', 'diagnostic_source'}
    assert 'relational-edges' not in json.dumps(public)


def test_repeated_semantic_failure_still_has_only_one_repair(tmp_path):
    api = API(messages=[answer(BAD_DUP), answer(BAD_ORPHAN)])
    result = run_sql(sql_core(tmp_path, api), 'two-failures', suite())
    assert result['status'] == 'failed' and len(result['attempts']) == 2
    assert len(api.requests) == 3 and result['costs']['api_usd'] == '0.0201'


def test_coverage_does_not_authenticate_the_application_oracles_semantics():
    # Explicitly retain the trust boundary: coverage and deterministic output
    # cannot establish that an application's supplied oracle is correct.
    task = suite(oracle=lambda _: [[9]])
    assert task.environment().validate('{"sql":"SELECT 9 AS id"}')['passed'] is True


def test_all_inputs_expectations_and_coverage_are_public_and_frozen():
    cases = deepcopy([BASE, EDGE, EMPTY]); rules = deepcopy(RULES)
    task = suite(public_cases=cases, required_coverage=rules)
    frozen = task.task()
    spec = json.loads(task._public_json)
    assert spec['samples'][1]['expected'] == [[1]]
    assert all(c['id'] in frozen['prompt'] for c in cases)
    assert all(rule['kind'] in frozen['prompt'] for rule in rules)
    assert 'semantic_equivalence_proven":false' in frozen['prompt']
    cases[0]['tables']['accounts'].clear(); rules.clear()
    assert task.task() == frozen


@pytest.mark.parametrize('changes', [
    {'oracle': None}, {'oracle_version': ''}, {'corpus_version': ''},
    {'public_cases': []}, {'public_cases': [deepcopy(BASE)] * 33},
    {'public_cases': [{**BASE, 'hidden_answer': 'PRIVATE'}]},
    {'required_coverage': []},
    {'required_coverage': [{'kind': 'magic_semantic_proof', 'table': 'access'}]},
    {'required_coverage': [RULES[0], RULES[0]]},
    {'required_coverage': [{'kind': 'empty_table', 'table': 'missing'}]},
    {'required_coverage': [{'kind': 'duplicate_key', 'table': 'access', 'columns': ['missing']}]},
    {'required_coverage': [{'kind': 'null_value', 'table': 'accounts', 'column': 'id'}]},
    {'public_cases': [deepcopy(BASE)]},
])
def test_missing_or_unwitnessed_coverage_never_silently_downgrades(changes):
    with pytest.raises(ValueError, match='no dispatch permitted') as caught:
        suite(**changes)
    assert 'PRIVATE' not in str(caught.value)


def test_oracle_is_not_called_for_invalid_or_uncovered_inputs():
    def forbidden(_):
        pytest.fail('Oracle must not see inadmissible input')
    for cases in [[deepcopy(BASE)], [{'id': 'bad', 'tables': {'accounts': []}}]]:
        with pytest.raises(ValueError):
            suite(public_cases=cases, oracle=forbidden)


def test_mutating_nondeterministic_or_malformed_oracle_cannot_qualify():
    def mutates(data):
        data['accounts'].clear()
        return []
    counter = 0

    def unstable(_):
        nonlocal counter
        counter += 1
        return [[counter]]

    def fails(_):
        raise RuntimeError('PRIVATE_CALLBACK_DETAIL')
    for fn in [mutates, unstable, fails, lambda _: [[True]], lambda _: {'hidden': 'PRIVATE'}]:
        with pytest.raises(ValueError, match='no dispatch permitted') as caught:
            suite(oracle=fn)
        assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('kind', ['bool', 'float'])
def test_equal_valued_numeric_type_mutations_are_still_input_modification(kind):
    def oracle(data):
        for row in data['accounts']:
            if kind == 'float':
                row['id'] = float(row['id'])
            elif row['id'] == 1:
                row['id'] = True
        return [[7]]
    with pytest.raises(ValueError, match='no dispatch permitted'):
        suite(oracle=oracle)


def test_oracle_is_materialized_before_dispatch_and_never_called_by_validator(tmp_path):
    calls = []

    def oracle(data):
        calls.append(1)
        return membership(data)
    task = suite(oracle=oracle)
    assert len(calls) == 6
    api = API(messages=[answer(EXISTS)])
    assert run_sql(sql_core(tmp_path, api), 'no-runtime-oracle', task)['status'] == 'passed'
    assert len(calls) == 6 and len(api.requests) == 1


@pytest.mark.parametrize('change', [{'oracle_version': 'v2'}, {'corpus_version': 'v2'},
                                  {'required_coverage': RULES[:-1]}])
def test_provenance_or_coverage_change_invalidates_run_without_paid_resend(tmp_path, change):
    task = suite(); updated = suite(**change)
    assert task.contract_version != updated.contract_version
    api = API(messages=[answer(EXISTS)])
    engine = sql_core(tmp_path, api)
    run_sql(engine, 'bound-public', task)
    with pytest.raises(ValueError, match='Resume evidence changed'):
        run_sql(engine, 'bound-public', updated)
    assert len(api.requests) == 1
