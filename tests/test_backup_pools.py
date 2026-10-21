"""Synthetic candidate preparation is not real model/provider qualification."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from cloudyfan_jev.scores.backup_pools import (
    TIERS, backup_coverage_report, prepare_backup_pools, verify_backup_pools,
)
from cloudyfan_jev.scores.categories import CATEGORY_SPECS, compile_category_matrix
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.configuration import configuration_digest
from test_categories import CATEGORY, NOW, fixture
from test_qcs import interval


def settings(category=CATEGORY, count=3):
    rows, registries, policies = fixture(category)
    registry = registries[category]
    originals = [registry['open_source_candidates'][0], *registry['candidates']]
    registry['open_source_candidates'], registry['candidates'] = [], []
    base_rows = deepcopy(rows)
    rows = []
    evidence = {}
    for tier, original, source in zip(TIERS, originals, base_rows):
        for n in range(count):
            entry, row = deepcopy(original), deepcopy(source)
            identity = f'example/{tier}-{n}'
            entry['config_id'] = f'{tier}-{n}'
            entry['source_model_id'] = row['model_id'] = identity
            entry['profile'].update(model=identity, model_version=identity, accepted_models=[identity],
                                    provider=f'provider-{n}', provider_name=f'Provider {n}')
            entry['model_family'] = f'family-{n}'
            row['source_row_index'] = len(rows)
            entry['observation_binding']['source_row_index'] = len(rows)
            (registry['open_source_candidates'] if tier == 'open_source'
             else registry['candidates']).append(entry)
            rows.append(row)
            evidence[entry['config_id']] = {
                'configuration_sha256': configuration_digest(entry),
                'provenance': {'basis': 'independent_calibration', 'workload_id': 'offline-category-fixture',
                    'category_id': category, 'source_url': 'https://example.org/independent',
                    'raw_sha256': 'b' * 64, 'retrieved_at': NOW, 'valid_until': '2026-10-22T00:00:00Z'},
                'quality_sufficient': True, 'generation_cost_usd': interval('.01'),
                'complete_cost_usd': interval(str(.1 + n * .1)),
                'speed': {'metric': 'flow_ms', 'statistic': 'p95', 'upper': '100'}, 'source_metrics': []}
    policy = {'schema_version': 'qcs-policy-v1', 'category_id': category,
              'workload_id': 'offline-category-fixture', 'speed_statistic': 'p95', 'max_flow_ms': '1000'}
    requirements = {'domain': category, 'capabilities': ['tools'], 'context_tokens': 1000}
    return compile_category_matrix(rows, registries, policies, now=NOW), requirements, evidence, policy


def prepare(values, category=CATEGORY, **kwargs):
    matrix, requirements, evidence, policy = values
    return prepare_backup_pools(matrix, category, requirements, evidence, policy, now=NOW, **kwargs)


def rehash(matrix, category=CATEGORY):
    view = matrix['categories'][category]
    snap = view['snapshot']
    snap['pools'] = {tier: deepcopy(view['pools'][tier]) for tier in ('low', 'mid', 'high')}
    snap['open_source_pool'] = deepcopy(view['pools']['open_source'])
    snap['open_source'] = deepcopy(view['pools']['open_source'][0])
    snap['snapshot_id'] = digest({k: v for k, v in snap.items() if k != 'snapshot_id'})
    matrix['matrix_id'] = digest({k: v for k, v in matrix.items() if k != 'matrix_id'})


@pytest.mark.parametrize('category', list(CATEGORY_SPECS))
def test_all_23_categories_can_have_three_distinct_models_per_pool(category):
    values = settings(category)
    before = deepcopy(values)
    plan = prepare(values, category)
    assert plan['coverage'] == {'target_size': 3, 'counts': dict.fromkeys(TIERS, 3), 'complete': True}
    assert plan['shortages'] == dict.fromkeys(TIERS, 0)
    assert all(len({r['entry']['profile']['model_version'] for r in pool}) == 3
               for pool in plan['pools'].values())
    verify_backup_pools(plan, now=NOW)
    assert values == before


def test_cost_speed_then_id_primary_and_explicit_diversity_for_backups():
    values = settings(count=4)
    matrix, _, evidence, _ = values
    pool = matrix['categories'][CATEGORY]['pools']['low']
    pool[1]['profile'].update(provider='provider-0', provider_name='Provider 0')
    pool[1]['model_family'] = 'family-0'
    evidence[pool[1]['config_id']]['configuration_sha256'] = configuration_digest(pool[1])
    rehash(matrix)
    plan = prepare(values)
    assert [r['entry']['config_id'] for r in plan['pools']['low']] == ['low-0', 'low-2', 'low-3']
    assert plan['physical_failure_independence_proven'] is False
    # Flow speed breaks identical complete cost; raw token estimates do not.
    evidence['low-1']['complete_cost_usd'] = interval('.1')
    evidence['low-1']['speed']['upper'] = '1'
    assert prepare(values)['pools']['low'][0]['entry']['config_id'] == 'low-1'


@pytest.mark.parametrize('identity_field', ['model_version', 'canonical_model_id', 'model'])
def test_same_model_multiple_providers_or_efforts_never_fill_three_slots(identity_field):
    values = settings()
    matrix, _, evidence, _ = values
    for entry in matrix['categories'][CATEGORY]['pools']['low']:
        entry['profile'][identity_field] = 'shared-model'
        if identity_field == 'model_version':
            entry['profile']['accepted_models'] = ['shared-model']
        evidence[entry['config_id']]['configuration_sha256'] = configuration_digest(entry)
    rehash(matrix)
    plan = prepare(values)
    assert len(plan['pools']['low']) == 1 and plan['shortages']['low'] == 2
    assert len([e for e in plan['excluded'] if e.get('reason') == 'duplicate_model_identity']) == 2


@pytest.mark.parametrize('bad', ['missing', 'cost', 'speed', 'quality', 'category', 'row', 'source_category'])
def test_bad_optional_dimensions_only_exclude_the_affected_candidate(bad):
    values = settings()
    evidence = values[2]
    row = evidence['low-0']
    if bad == 'missing': del evidence['low-0']
    if bad == 'cost': del row['complete_cost_usd']
    if bad == 'speed': row['speed']['metric'] = 'endpoint_ms'
    if bad == 'quality': row['quality_sufficient'] = False
    if bad == 'category': row['provenance']['category_id'] = 'design-arena:models:website'
    if bad == 'row': evidence['low-0'] = []
    if bad == 'source_category':
        row['source_metrics'] = [{'quality': {'coding_index': 10}}]
    plan = prepare(values)
    assert len(plan['pools']['low']) == 2 and len(plan['pools']['mid']) == 3
    assert any(e.get('config_id') == 'low-0' for e in plan['excluded'])


def test_pending_approval_never_promoted_by_rehash_or_preparation():
    values = settings()
    values[0]['categories'][CATEGORY]['pools']['low'][0]['approved'] = False
    rehash(values[0])
    plan = prepare(values)
    assert len(plan['pools']['low']) == 2
    assert any(e['reason'] == 'configuration_not_approved' for e in plan['excluded'])


def test_sql_capability_requires_additional_exact_sql_qualification():
    values = settings()
    matrix, requirements, _, _ = values
    requirements['capabilities'] = ['sql-readonly']
    for pool in matrix['categories'][CATEGORY]['pools'].values():
        for entry in pool:
            entry['capabilities'] += ['sql-readonly']
            entry['sql_qualification'] = {'configuration_sha256': configuration_digest(entry),
                'report_id': 'c' * 64, 'valid_until': '2026-10-22T00:00:00Z'}
    matrix['categories'][CATEGORY]['pools']['low'][0]['sql_qualification'] = []
    rehash(matrix)
    plan = prepare(values)
    assert len(plan['pools']['low']) == 2 and len(plan['pools']['high']) == 3
    assert any(e['reason'] == 'sql_qualification_missing_or_expired' for e in plan['excluded'])


def test_absent_category_is_a_shortage_and_never_borrows_coding_data():
    values = settings()
    category = 'design-arena:models:website'
    values[1]['domain'] = values[3]['category_id'] = category
    plan = prepare(values, category)
    assert plan['coverage']['counts'] == dict.fromkeys(TIERS, 0)
    assert plan['shortages'] == dict.fromkeys(TIERS, 3)
    assert plan['snapshot_id'] is None
    verify_backup_pools(plan, now=NOW)


def test_stale_unrelated_category_does_not_block_selected_category():
    values = settings()
    other = 'design-arena:models:website'
    other_matrix = settings(other)[0]
    view = deepcopy(other_matrix['categories'][other])
    view['snapshot']['created_at'] = '2020-01-01T00:00:00Z'
    view['snapshot']['valid_until'] = '2020-01-02T00:00:00Z'
    view['snapshot']['snapshot_id'] = digest({k: v for k, v in view['snapshot'].items() if k != 'snapshot_id'})
    values[0]['categories'][other] = view
    values[0]['matrix_id'] = digest({k: v for k, v in values[0].items() if k != 'matrix_id'})
    assert prepare(values)['coverage']['complete']


def test_copies_and_digests_protect_frozen_inputs_and_candidate_identity():
    values = settings()
    plan = prepare(values)
    values[2]['low-0']['speed']['upper'] = '999'
    assert plan['pools']['low'][0]['evidence']['speed']['upper'] == '100'
    plan['pools']['low'][0]['entry']['profile']['provider'] = 'changed'
    with pytest.raises(ValueError, match='integrity'):
        verify_backup_pools(plan)
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    with pytest.raises(ValueError, match='snapshot'):
        verify_backup_pools(plan)


def test_report_retains_all_23_shortages_and_cli_runs_offline(tmp_path):
    matrix, requirements, evidence, policy = settings(count=1)
    inputs = (matrix, {CATEGORY: requirements}, {CATEGORY: evidence}, {CATEGORY: policy})
    report = backup_coverage_report(*inputs, now=NOW)
    assert len(report['categories']) == 23
    assert report['categories'][CATEGORY]['shortages'] == dict.fromkeys(TIERS, 2)
    assert report['categories']['design-arena:models:image']['coverage']['counts'] == dict.fromkeys(TIERS, 0)
    paths = [tmp_path / f'{n}.json' for n in range(4)]
    for path, value in zip(paths, inputs): path.write_text(json.dumps(value))
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/prepare_category_backups.py')]
    for option, path in zip(('--matrix', '--requirements', '--evidence', '--qcs-policies'), paths):
        command += [option, str(path)]
    completed = subprocess.run([*command, '--now', NOW], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout)['report_id'] == report['report_id']


def test_new_backup_qcs_schema_keeps_full_policy_without_old_singleton_calibration():
    values = settings()
    values[3].update(schema_version='qcs-backup-policy-v1', arbitrary_future_contract={'binding': 'preserved'})
    plan = prepare(values)
    assert plan['qcs_policy'] == values[3] and plan['coverage']['complete']


def test_task_provider_context_and_capability_filters_cannot_fill_shortages():
    values = settings()
    values[1]['allowed_providers'] = ['provider-1']
    plan = prepare(values)
    assert plan['coverage']['counts'] == dict.fromkeys(TIERS, 1)
    values[1]['context_tokens'] = 999999
    assert prepare(values)['coverage']['counts'] == dict.fromkeys(TIERS, 0)
    values[1]['context_tokens'] = 1000
    values[1]['capabilities'] = ['unsupported-modality']
    assert prepare(values)['coverage']['counts'] == dict.fromkeys(TIERS, 0)


def test_selected_snapshot_expiry_and_mutated_coverage_are_rejected():
    matrix, requirements, evidence, policy = settings()
    with pytest.raises(ValueError, match='stale'):
        prepare_backup_pools(matrix, CATEGORY, requirements, evidence, policy,
                             now='2026-10-23T00:00:00Z')
    plan = prepare((matrix, requirements, evidence, policy))
    plan['shortages']['low'] = 1
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    with pytest.raises(ValueError, match='coverage'):
        verify_backup_pools(plan)


@pytest.mark.parametrize('target', [0, 4, True, '3'])
def test_invalid_targets_are_not_coerced(target):
    with pytest.raises(ValueError, match='target'):
        prepare(settings(), target_size=target)
