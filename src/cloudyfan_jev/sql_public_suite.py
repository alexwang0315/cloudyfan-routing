"""Prospective public fixture qualification, independent of model answers.

The trusted application owns corpus generation and the independent Python oracle.
Only materialized PUBLIC inputs/expected rows reach SqlContract. No callback runs
during routing, no SQL is inferred from prose, and coverage is not equivalence.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .sql import SqlContract


VERSION = 'sql-public-suite-v1'
_FIELDS = {
    'empty_table': {'kind', 'table'},
    'null_value': {'kind', 'table', 'column'},
    'duplicate_key': {'kind', 'table', 'columns'},
    'orphan_reference': {'kind', 'table', 'column', 'parent_table', 'parent_column'},
    'parent_without_children': {'kind', 'table', 'column', 'parent_table', 'parent_column'},
}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def _rules(rules, tables):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 32:
        raise ValueError('Declare one to32 public coverage rules')
    for rule in rules:
        if (not isinstance(rule, dict) or not isinstance(rule.get('kind'), str)
                or rule['kind'] not in _FIELDS or set(rule) != _FIELDS[rule['kind']]):
            raise ValueError('Invalid public coverage rule')
        for table_key, column_key in [('table', 'column'), ('parent_table', 'parent_column')]:
            if table_key not in rule:
                continue
            name = rule[table_key]
            if not isinstance(name, str) or name not in tables:
                raise ValueError('Unknown public coverage table')
            if column_key in rule and rule[column_key] not in dict(tables[name]):
                raise ValueError('Unknown public coverage column')
        if rule['kind'] == 'duplicate_key':
            columns = rule['columns']
            if (not isinstance(columns, list) or not columns
                    or any(not isinstance(c, str) or c not in dict(tables[rule['table']]) for c in columns)
                    or len(set(columns)) != len(columns)):
                raise ValueError('Invalid public duplicate key')
        if 'parent_table' in rule:
            child_kind = dict(tables[rule['table']])[rule['column']].split()[0]
            parent_kind = dict(tables[rule['parent_table']])[rule['parent_column']].split()[0]
            if child_kind != parent_kind:
                raise ValueError('Reference coverage requires matching column types')
    if len({_json(rule) for rule in rules}) != len(rules):
        raise ValueError('Duplicate public coverage rules')


def _witness(rule, data):
    rows = data[rule['table']]
    kind = rule['kind']
    if kind == 'empty_table':
        return not rows
    if kind == 'null_value':
        return any(row[rule['column']] is None for row in rows)
    if kind == 'duplicate_key':
        values = [tuple(row[c] for c in rule['columns']) for row in rows]
        return len(set(values)) < len(values)
    parents = {row[rule['parent_column']] for row in data[rule['parent_table']]
               if row[rule['parent_column']] is not None}
    children = {row[rule['column']] for row in rows if row[rule['column']] is not None}
    return bool(children - parents) if kind == 'orphan_reference' else bool(parents - children)


def build_public_sql_contract(*, version, prompt, tables, expected_columns,
                              public_cases, oracle, oracle_version, corpus_version,
                              required_coverage, require_address_space=False,
                              domain_policy=None, isolation=None):
    """Freeze a bounded public corpus before dispatch; fail closed on missing evidence.

    Each public case has exactly id/tables. The oracle is trusted application code,
    not model code or a private grader. All of its returned expected rows become
    public prompt content. Oracle identity is an application assertion; it is not
    independently authenticated here. It is called only during construction.
    """
    try:
        if (not callable(oracle) or any(not isinstance(v, str) or not v.strip() or len(v) > 160
                                        for v in (version, oracle_version, corpus_version))
                or not isinstance(public_cases, list) or not 1 <= len(public_cases) <= 32):
            raise ValueError('Public suite needs a bounded corpus and trusted versioned oracle')
        cases = json.loads(_json(public_cases))
        if any(not isinstance(c, dict) or set(c) != {'id', 'tables'} for c in cases):
            raise ValueError('Only public case identities and inputs are accepted')
        rules = json.loads(_json(required_coverage))
        samples = [{**case, 'expected': []} for case in cases]
        # Validate trusted input shape/types/bounds BEFORE calling application code.
        common = dict(prompt=prompt, tables=tables, expected_columns=expected_columns,
                      require_address_space=require_address_space, domain_policy=domain_policy,
                      isolation=isolation)
        admission = SqlContract(version=version, samples=samples, **common)
        admitted = json.loads(admission._public_json)
        tables = admitted['tables']
        common['tables'] = tables
        common['expected_columns'] = admitted['expected_columns']
        _rules(rules, tables)
        coverage = [{'rule': rule, 'case_ids': [c['id'] for c in cases if _witness(rule, c['tables'])]}
                    for rule in rules]
        if any(not item['case_ids'] for item in coverage):
            raise ValueError('Required public coverage has no witness')
        for sample in samples:
            answers = []
            for _ in range(2):
                inputs = deepcopy(sample['tables'])
                expected = oracle(inputs)
                if _json(inputs) != _json(sample['tables']):
                    raise ValueError('Public oracle mutated input')
                answers.append(_json(expected))
            if answers[0] != answers[1]:
                raise ValueError('Public oracle is not reproducible')
            sample['expected'] = json.loads(answers[0])
        metadata = {'version': VERSION, 'oracle_version': oracle_version,
                    'corpus_version': corpus_version,
                    'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    'coverage': coverage, 'semantic_equivalence_proven': False}
        encoded = _json(metadata)
        common['prompt'] = prompt + '\nPublic validation suite: ' + encoded
        # SqlContract deep-snapshots every input and expected row, and keeps all
        # existing worker, diagnostic, run identity and attempt-limit semantics.
        return SqlContract(version=version + ':' + VERSION + ':' + hashlib.sha256(encoded.encode()).hexdigest(),
                           samples=samples, **common)
    except Exception:
        # Trusted callback failures are admission/engineering failures, not model
        # mistakes. Never expose their raw exception, input values or stack.
        raise ValueError('Public SQL suite admission failed; no dispatch permitted') from None
