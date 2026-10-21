"""Independent category boundaries; fixtures never qualify a real service."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from cloudyfan_jev.demo import example_config
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.categories import (
    CATEGORY_SPECS, category_matches, compile_category_matrix,
    resolve_category, verify_category_matrix,
)

NOW = '2026-10-21T00:00:00+00:00'
CATEGORY = 'artificial-analysis:coding_index'


def fixture(category=CATEGORY):
    spec = CATEGORY_SPECS[category]
    profiles, _ = example_config()
    bounds = ((1200, 1400) if spec['source'] == 'design-arena'
              else (.5, .8) if spec['source'] == 'openrouter' else (30, 60))
    scores = ([900, 1000, 1300, 1500] if spec['source'] == 'design-arena'
              else [.1, .2, .6, .95] if spec['source'] == 'openrouter' else [10, 20, 45, 80])
    policy = {'id': category + '-fixture', 'domain': category, 'source': spec['source'],
              'source_version': 'openrouter-benchmarks-v1', 'metric': spec['metric'],
              'scale': 'source_native', 'low_mid': bounds[0], 'mid_high': bounds[1],
              'max_source_age_hours': 48, 'max_catalog_age_hours': 48,
              'snapshot_ttl_hours': 24, 'min_samples': 0}
    registry = {'candidates': [], 'open_source_candidates': []}
    rows = []
    for index, (tier, score) in enumerate(zip(('open_source', 'low', 'mid', 'high'), scores)):
        row = {'schema_version': 'qcs-source-v1', 'source': spec['source'],
               'source_version': policy['source_version'], 'source_url': 'https://example.org/scores',
               'model_id': 'example/' + tier, 'quality': {spec['metric']: score},
               'effort': 'low', 'retrieved_at': NOW, 'raw_sha256': 'a' * 64,
               'source_row_index': index, 'binding_status': 'unreviewed_source_variant',
               'as_of': NOW, 'arena': spec.get('arena'), 'category': spec.get('category'),
               'benchmark_type': spec.get('benchmark_type')}
        entry = {'config_id': tier, 'profile': profiles[tier], 'approved': True,
                 'context_tokens': 8192, 'capabilities': ['tools'], 'catalog_checked_at': NOW,
                 'source_model_id': row['model_id'], 'source': spec['source'],
                 'source_version': policy['source_version'],
                 'observation_binding': {'raw_sha256': row['raw_sha256'], 'source_row_index': index,
                                         'reviewed': True, 'evidence_url': 'https://example.org/review'}}
        if tier == 'open_source':
            entry['open_weight'] = True
            registry['open_source_candidates'].append(entry)
        else:
            registry['candidates'].append(entry)
        rows.append(row)
    return rows, {category: registry}, {category: policy}


def compile_fixture(category=CATEGORY):
    return compile_category_matrix(*fixture(category), now=NOW)


def test_independent_23_category_matrix_has_four_pools_and_no_input_mutation():
    args = fixture()
    before = deepcopy(args)
    result = compile_category_matrix(*args, now=NOW)
    assert args == before
    assert len(result['categories']) == 23
    selected = result['categories'][CATEGORY]
    assert selected['status'] == 'ready'
    assert set(selected['pools']) == {'open_source', 'low', 'mid', 'high'}
    assert [selected['pools'][k][0]['config_id'] for k in ('open_source', 'low', 'mid', 'high')] == [
        'open_source', 'low', 'mid', 'high']
    assert selected['snapshot']['policy']['domain'] == CATEGORY
    assert all(e['profile']['capability_card']['category_id'] == CATEGORY
               for pool in selected['pools'].values() for e in pool)
    other = result['categories']['design-arena:models:website']
    assert other['status'] == 'blocked' and other['snapshot'] is None
    verify_category_matrix(result)


@pytest.mark.parametrize('alias, expected', [('coding', CATEGORY), ('website', 'design-arena:models:website'),
    ('agentic', 'artificial-analysis:agentic_index'), ('intelligence', 'artificial-analysis:intelligence_index')])
def test_resolves_only_unambiguous_explicit_categories(alias, expected):
    assert resolve_category(alias) == expected
    assert resolve_category(expected) == expected


@pytest.mark.parametrize('value', [None, '', 'search', 'sql', 'Coding', {}, 'openrouter:unknown'])
def test_unknown_or_ambiguous_category_never_falls_back(value):
    with pytest.raises(ValueError):
        resolve_category(value)


def test_source_category_metric_are_bound_and_no_cross_category_borrowing():
    rows, registries, policies = fixture()
    assert category_matches(rows[0], CATEGORY)
    rows[1]['source'] = 'design-arena'
    rows[2]['quality'] = {'intelligence_index': 45}
    result = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert not result['pools']['low'] and not result['pools']['mid']
    assert result['pools']['high']
    assert not category_matches(rows[0], 'design-arena:models:website')


@pytest.mark.parametrize('change,reason', [
    ({'binding_status': 'ambiguous_source_identity'}, 'ambiguous_source_identity'),
    ({'raw_sha256': 'b' * 64}, 'observation_binding_missing_or_mismatch'),
    ({'source_row_index': 99}, 'observation_binding_missing_or_mismatch'),
    ({'as_of': '2026-10-01T00:00:00Z'}, 'source_stale'),
    ({'as_of': '2026-10-22T00:00:00Z'}, 'source_timestamp_in_future'),
])
def test_bad_source_evidence_excludes_only_its_candidate(change, reason):
    rows, registries, policies = fixture()
    rows[1].update(change)
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert not selected['pools']['low'] and selected['pools']['high']
    assert {'config_id': 'low', 'reason': reason} in selected['excluded']


def test_explicit_review_and_effort_required_without_name_guessing():
    rows, registries, policies = fixture()
    entry = registries[CATEGORY]['candidates'][0]
    entry['observation_binding']['reviewed'] = False
    result = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert not result['pools']['low']
    entry['observation_binding']['reviewed'] = True
    rows[1]['effort'] = None
    rows[1]['source_variant_label'] = 'Low effort model'
    assert not compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]['pools']['low']
    entry.update(verified_source_effort='low', effort_evidence_url='https://example.org/effort')
    assert compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]['pools']['low']


def test_multiple_oss_candidates_and_malformed_optional_candidates_are_isolated():
    rows, registries, policies = fixture()
    registry = registries[CATEGORY]
    second = deepcopy(registry['open_source_candidates'][0])
    second['config_id'] = 'oss-two'
    registry['open_source_candidates'] += [second, {'config_id': 'broken'}]
    registry['candidates'].append({'config_id': 'broken-scored'})
    result = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert [e['config_id'] for e in result['snapshot']['open_source_pool']] == ['open_source', 'oss-two']
    assert result['snapshot']['open_source']['config_id'] == 'open_source'
    assert result['status'] == 'ready' and len(result['excluded']) == 2


def test_same_model_different_reviewed_rows_not_merged_or_best_picked():
    rows, registries, policies = fixture()
    extra = deepcopy(rows[1])
    extra.update(source_row_index=9, quality={'coding_index': 95})
    rows.append(extra)
    # An unbound second upstream variant cannot affect the bound low model.
    result = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert result['pools']['low'][0]['evidence']['score'] == 20
    rows.append(deepcopy(rows[1]))
    result = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert not result['pools']['low']


def test_family_prior_keeps_sql_qualification_requirement():
    rows, registries, policies = fixture()
    policies[CATEGORY]['score_role'] = 'family_effort_prior'
    result = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert result['status'] == 'blocked'
    assert not any(result['pools'].values())
    assert any('sql_qualification' in e['reason'] for e in result['excluded'])


def test_invalid_category_policy_blocks_only_that_category():
    rows, registries, policies = fixture()
    policies[CATEGORY]['domain'] = 'coding'
    result = compile_category_matrix(rows, registries, policies, now=NOW)
    assert result['categories'][CATEGORY]['status'] == 'blocked'
    assert any(e['reason'] == 'category_policy_mismatch' for e in result['categories'][CATEGORY]['excluded'])


def test_matrix_integrity_snapshot_and_category_binding():
    result = compile_fixture()
    result['categories'][CATEGORY]['pools']['low'][0]['approved'] = False
    with pytest.raises(ValueError):
        verify_category_matrix(result)
    result = compile_fixture()
    result['categories'][CATEGORY]['snapshot']['policy']['domain'] = 'coding'
    result['matrix_id'] = digest({k: v for k, v in result.items() if k != 'matrix_id'})
    with pytest.raises(ValueError):
        verify_category_matrix(result)


def test_all_23_policy_templates_native_metric_and_scopes():
    template = json.loads((Path(__file__).parents[1] / 'examples/category-policies-v1.json').read_text())
    assert set(template['policies']) == set(CATEGORY_SPECS)
    for category, policy in template['policies'].items():
        assert policy['domain'] == category and policy['metric'] == CATEGORY_SPECS[category]['metric']
        assert policy['score_role'] == 'category_family_prior'
        assert policy['qualification_status'] == 'exploratory_unvalidated'


@pytest.mark.parametrize('category', list(CATEGORY_SPECS))
def test_each_category_can_compile_independently(category):
    result = compile_fixture(category)
    assert result['categories'][category]['status'] == 'ready'
    verify_category_matrix(result)


def test_search_configuration_requires_exact_reviewed_binding():
    category = 'openrouter:search_browsecomp'
    rows, registries, policies = fixture(category)
    rows[1].update(run_config={'reasoning_effort': 'low', 'max_agent_turns': 8},
                   search_engine='engine-a', search_surface='server-tool')
    entry = registries[category]['candidates'][0]
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]
    assert not selected['pools']['low']
    entry['observation_binding'].update(run_config=deepcopy(rows[1]['run_config']),
                                        search_engine='engine-a', search_surface='server-tool')
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]
    assert selected['pools']['low']
    entry['observation_binding']['run_config']['max_agent_turns'] = 100
    assert not compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]['pools']['low']


def test_explicit_empty_oss_list_does_not_reuse_legacy_singleton():
    rows, registries, policies = fixture()
    registry = registries[CATEGORY]
    registry['open_source'] = registry['open_source_candidates'][0]
    registry['open_source_candidates'] = []
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert selected['status'] == 'blocked' and not selected['pools']['open_source']


def test_source_expiry_starts_at_old_as_of_not_fresh_download():
    rows, registries, policies = fixture()
    rows[0]['as_of'] = '2026-10-19T04:00:00Z'
    snapshot = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]['snapshot']
    assert snapshot['valid_until'] == '2026-10-21T04:00:00+00:00'
    # Integrity verification intentionally takes no current time; selecting a
    # different category will not be blocked by this category's age.
    verify_category_matrix(compile_category_matrix(rows, registries, policies, now=NOW))


def test_missing_optional_low_tier_does_not_block_valid_mid_and_high():
    rows, registries, policies = fixture()
    registries[CATEGORY]['candidates'][0]['approved'] = False
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert selected['status'] == 'ready'
    assert not selected['pools']['low'] and selected['pools']['mid']


def test_model_identity_binding_is_exact_not_family_name_matching():
    rows, registries, policies = fixture()
    registries[CATEGORY]['candidates'][0]['source_model_id'] += '-new-version'
    assert not compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]['pools']['low']


def test_rehashed_matrix_cannot_relabel_a_different_category_observation():
    result = compile_fixture()
    selected = result['categories'][CATEGORY]
    for entry in (selected['pools']['low'][0], selected['snapshot']['pools']['low'][0]):
        entry['evidence']['metric'] = 'intelligence_index'
    snapshot = selected['snapshot']
    snapshot['snapshot_id'] = digest({k: v for k, v in snapshot.items() if k != 'snapshot_id'})
    result['matrix_id'] = digest({k: v for k, v in result.items() if k != 'matrix_id'})
    with pytest.raises(ValueError, match='Candidate category'):
        verify_category_matrix(result)


def category_qualified_fixture(category='design-arena:models:website'):
    from cloudyfan_jev.scores.configuration import configuration_digest
    rows, registries, _ = fixture(category)
    template = json.loads((Path(__file__).parents[1] / 'examples/category-policies-v1.json').read_text())
    policy = template['policies'][category]
    lower, upper = policy['low_mid'], policy['mid_high']
    values = [lower / 2, lower / 2, (lower + upper) / 2, upper + (upper - lower) / 4]
    for row, value in zip(rows, values):
        row['quality'][policy['metric']] = value
    entries = registries[category]['open_source_candidates'] + registries[category]['candidates']
    for entry in entries:
        entry['category_qualification'] = {'category_id': category,
            'configuration_sha256': configuration_digest(entry), 'report_id': 'b' * 64,
            'valid_until': '2026-10-21T03:00:00Z'}
    return rows, registries, {category: policy}


def test_real_website_policy_uses_independent_category_qualification_without_sql():
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    assert policies[category]['score_role'] == 'category_family_prior'
    assert all('sql_qualification' not in e for e in registries[category]['candidates'])
    result = compile_category_matrix(rows, registries, policies, now=NOW)
    selected = result['categories'][category]
    assert selected['status'] == 'ready'
    assert all(selected['pools'][tier] for tier in ('open_source', 'low', 'mid', 'high'))
    assert selected['snapshot']['valid_until'] == '2026-10-21T03:00:00+00:00'
    assert selected['pools']['mid'][0]['profile']['capability_card']['benchmark_evidence']['evidence_role'] == 'category_family_prior'
    verify_category_matrix(result)


@pytest.mark.parametrize('change', [None, {'category_id': CATEGORY},
    {'configuration_sha256': 'c' * 64}, {'report_id': 'x' * 64},
    {'valid_until': '2026-10-20T00:00:00Z'}, {'valid_until': None}])
def test_category_qualification_missing_mismatch_or_expiry_excludes_only_candidate(change):
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    entry = registries[category]['candidates'][0]
    if change is None:
        del entry['category_qualification']
    else:
        entry['category_qualification'].update(change)
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]
    assert selected['status'] == 'ready' and not selected['pools']['low']
    assert {'config_id': 'low', 'reason': 'category_qualification_missing_or_expired'} in selected['excluded']


def test_category_qualification_oss_expiry_cannot_be_refreshed_by_catalog():
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    registries[category]['open_source_candidates'][0]['category_qualification']['valid_until'] = NOW
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]
    assert selected['status'] == 'blocked' and selected['snapshot'] is None


def test_independent_category_qualified_oss_needs_no_published_benchmark_score():
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    entry = registries[category]['open_source_candidates'][0]
    del entry['observation_binding']
    rows = rows[1:]
    result = compile_category_matrix(rows, registries, policies, now=NOW)
    selected = result['categories'][category]
    assert selected['status'] == 'ready'
    oss = selected['pools']['open_source'][0]
    assert oss['profile']['external_score'] is None
    assert 'benchmark_evidence' not in oss['profile']['capability_card']
    assert oss['profile']['capability_card']['category_source']['basis'] == 'independent_category_qualification'
    assert selected['snapshot']['valid_until'] == '2026-10-21T03:00:00+00:00'
    verify_category_matrix(result)


def test_bad_explicit_oss_benchmark_binding_does_not_fall_back_to_independent_report():
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    registries[category]['open_source_candidates'][0]['observation_binding']['source_row_index'] = 999
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]
    assert selected['status'] == 'blocked' and selected['snapshot'] is None


def test_unscored_oss_missing_its_independent_report_remains_blocked():
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    entry = registries[category]['open_source_candidates'][0]
    del entry['observation_binding']
    del entry['category_qualification']
    selected = compile_category_matrix(rows[1:], registries, policies, now=NOW)['categories'][category]
    assert selected['status'] == 'blocked'
    assert {'config_id': 'open_source', 'reason': 'category_qualification_missing_or_expired'} in selected['excluded']


@pytest.mark.parametrize('category', list(CATEGORY_SPECS))
def test_every_provided_native_policy_compiles_with_its_own_category_qualification(category):
    result = compile_category_matrix(*category_qualified_fixture(category), now=NOW)
    selected = result['categories'][category]
    assert selected['status'] == 'ready'
    assert all(selected['pools'][tier] for tier in ('open_source', 'low', 'mid', 'high'))
    verify_category_matrix(result)


def test_scored_category_qualification_shorter_expiry_limits_snapshot_lifetime():
    category = 'design-arena:models:website'
    rows, registries, policies = category_qualified_fixture(category)
    registries[category]['candidates'][0]['category_qualification']['valid_until'] = '2026-10-21T01:00:00Z'
    result = compile_category_matrix(rows, registries, policies, now=NOW)
    assert result['categories'][category]['snapshot']['valid_until'] == '2026-10-21T01:00:00+00:00'


@pytest.mark.parametrize('category', ['openrouter:gpqa_diamond', 'openrouter:search_widesearch'])
@pytest.mark.parametrize('value', [-.01, 1.01])
def test_invalid_native_ratio_excludes_only_its_bound_candidate(category, value):
    rows, registries, policies = fixture(category)
    rows[1]['quality'][CATEGORY_SPECS[category]['metric']] = value
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][category]
    assert selected['status'] == 'ready' and not selected['pools']['low']
    assert {'config_id': 'low', 'reason': 'score_outside_native_range'} in selected['excluded']


def test_native_aa_index_is_not_artificially_capped_at_100():
    rows, registries, policies = fixture()
    rows[3]['quality']['coding_index'] = 120
    selected = compile_category_matrix(rows, registries, policies, now=NOW)['categories'][CATEGORY]
    assert selected['pools']['high'][0]['evidence']['score'] == 120


def test_malformed_optional_sql_qualification_does_not_abort_category_compilation():
    from cloudyfan_jev.scores.configuration import configuration_digest
    rows, registries, policies = fixture()
    policies[CATEGORY]['score_role'] = 'family_effort_prior'
    registry = registries[CATEGORY]
    for entry in registry['candidates'] + registry['open_source_candidates']:
        entry['sql_qualification'] = {'configuration_sha256': configuration_digest(entry),
            'report_id': 'a' * 64, 'valid_until': '2026-10-22T00:00:00Z'}
    registry['candidates'][0]['sql_qualification'] = []
    matrix = compile_category_matrix(rows, registries, policies, now=NOW)
    view = matrix['categories'][CATEGORY]
    assert view['status'] == 'ready' and not view['pools']['low']
    assert view['pools']['mid'] and view['pools']['high']
    assert any(e['reason'] == 'sql_qualification_missing_or_expired' for e in view['excluded'])
