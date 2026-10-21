from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.qcs import (prepare_qcs as _prepare_qcs, SelectionPolicy,
                                    classifier_contract_hash, flow_contract_hash, HORIZON)
from test_scores import snapshot, REQUIREMENTS, NOW
import pytest


def interval(low, high=None):
    return {'lower': str(low), 'upper': str(low if high is None else high)}


def prepare_qcs(snap, requirements, evidence, policy, *, now):
    # Each synthetic mutation represents a new independently calibrated fixture.
    # Production prepare_qcs never supplies a missing forecast or rewrites hashes.
    policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': 'p95', 'upper': '500',
        'provenance': dict(policy['calibration']),
        'contract_sha256': flow_contract_hash(snap, requirements, evidence, policy)}
    return _prepare_qcs(snap, requirements, evidence, policy, now=now)


def inputs():
    snap = snapshot()
    entries = {t: snap['pools'][t][0] for t in ('low', 'mid', 'high')}
    entries['open_source'] = snap['open_source']
    provenance = {'basis': 'independent_calibration', 'workload_id': 'arithmetic-small',
                  'source_url': 'https://example.org/calibration', 'raw_sha256': 'a' * 64,
                  'retrieved_at': NOW, 'valid_until': '2026-10-19T12:00:00Z'}
    evidence = {}
    for tier, entry in entries.items():
        evidence[entry['config_id']] = {
            'configuration_sha256': configuration_digest(entry), 'provenance': dict(provenance),
            'quality_sufficient': True, 'generation_cost_usd': interval('0.1'),
            'complete_cost_usd': interval('0.1'),
            'speed': {'metric': 'flow_ms', 'statistic': 'p95', 'upper': '100'},
            'source_metrics': []}
    stages = {}
    for key, tiers in [('low_mid', ('low', 'mid')), ('mid_high:low', ('mid', 'high')),
                       ('mid_high:mid', ('mid', 'high'))]:
        stages[key] = {'direct_tier': tiers[1], 'classifier_cost_usd': interval('0.01'),
            'classifier_ms_upper': '10', 'lower_probability': interval('.5'),
            'paths': {t: {'cost_usd': interval('0.1'), 'flow_ms_upper': '100'} for t in tiers}}
    policy = {'schema_version': 'qcs-policy-v1', 'workload_id': 'arithmetic-small',
              'speed_statistic': 'p95', 'max_flow_ms': '1000',
              'minimum_saving_usd': '0.001', 'minimum_saving_fraction': '0.01',
              'calibration': {**provenance, 'configurations': {t: configuration_digest(e) for t, e in entries.items()},
                  'horizon': HORIZON,
                  'includes': ['generation', 'failure', 'retry', 'repair', 'escalation', 'later_classification', 'cache', 'reasoning']},
              'stages': stages}
    from cloudyfan_jev.demo import example_config
    from cloudyfan_jev.engine import Core, Limits
    profiles, decision = example_config()
    policy['execution_contract'] = Core(None, profiles, decision, limits=Limits(max_turns=1)).execution_contract_descriptor('v1')
    return snap, evidence, policy


def test_same_complete_cost_sends_mid_without_paying_classifier():
    snap, evidence, policy = inputs()
    prepared = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    selection = SelectionPolicy(prepared)
    outcome = selection.decide('low_mid', 'open_source')
    assert outcome['action'] == 'direct' and outcome['selected_tier'] == 'mid'
    assert outcome['net_saving_lower_usd'] == '-0.01'
    assert selection.reservation_plan() == {
        'phases': [['open_source'], ['mid'], ['high']], 'classifier_calls': 0}
    assert prepared['plan_id'] == digest({k: v for k, v in prepared.items() if k != 'plan_id'})


def economical():
    snap, evidence, policy = inputs()
    evidence['low']['generation_cost_usd'] = interval('0.01')
    evidence['low']['complete_cost_usd'] = interval('0.01')
    p = policy['stages']['low_mid']
    p.update(classifier_cost_usd=interval('0.001'), lower_probability=interval('.9'),
             classify_flow_ms_upper='110')
    p['paths']['low']['cost_usd'] = interval('0.01')
    return snap, evidence, policy


def conditional(settings, key, stage, previous, costs):
    """Independent synthetic conditional population, never formal answers."""
    snap, evidence, policy = settings
    from cloudyfan_jev.demo import example_config
    _, decision = example_config()
    policy['classifier_version'] = decision['version']
    initial = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    policy['stages'][key]['classified_calibration'] = {
        **policy['calibration'], 'classifier_version': decision['version'],
        'stage': stage, 'previous_tier': previous, 'context': 'public_failure_or_unknown',
        'effective_probability_includes_fallback': True,
        'contract_sha256': classifier_contract_hash(initial['profiles'], decision['version'], stage, previous)}
    policy['stages'][key]['classified_paths'] = {t: {'cost_usd': interval(c)} for t, c in costs.items()}
    return settings


def classifiable():
    snap, evidence, policy = economical()
    # An unconditional low choice needs costly repairs. Independently measured
    # classifier-conditioned low choices have much less escalation, not the same
    # forecast misleadingly compared with an arbitrarily expensive direct model.
    evidence['low']['complete_cost_usd'] = interval('.2')
    policy['stages']['low_mid']['paths']['low']['cost_usd'] = interval('.2')
    return conditional((snap, evidence, policy), 'low_mid', 'low_mid', 'open_source',
                       {'low': '.01', 'mid': '.1'})


def test_joint_classified_flow_measurement_not_sum_of_individual_p95_controls_speed():
    snap, evidence, policy = classifiable()
    policy['stages']['low_mid']['classify_flow_ms_upper'] = '2000'
    prepared = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    choice = SelectionPolicy(prepared).decide('low_mid', 'open_source')
    assert choice['action'] == 'direct' and choice['reason'] == 'classification_exceeds_speed_limit'


def test_meaningful_cost_spread_after_all_continuation_costs_allows_classifier():
    snap, evidence, policy = classifiable()
    selection = SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW))
    choice = selection.decide('low_mid', 'open_source')
    assert choice['action'] == 'classify' and choice['net_saving_lower_usd'] == '0.080'
    assert selection.reservation_plan()['classifier_calls'] == 1


def test_cheap_generation_with_expensive_failure_and_escalation_does_not_pay_jev():
    snap, evidence, policy = inputs()
    evidence['low']['generation_cost_usd'] = interval('.01')
    policy['stages']['low_mid']['paths']['low']['cost_usd'] = interval('0.15', '0.2')
    choice = SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)).decide('low_mid', 'open_source')
    assert choice['action'] == 'direct' and choice['selected_tier'] == 'mid'


def test_wide_calibration_uncertainty_does_not_use_optimistic_mean_probability():
    snap, evidence, policy = classifiable()
    policy['stages']['low_mid']['lower_probability'] = interval('0', '1')
    assert SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)).decide(
        'low_mid', 'open_source')['action'] == 'direct'


@pytest.mark.parametrize('mutation', ['quality', 'speed', 'statistic', 'missing', 'stale', 'binding'])
def test_bad_lower_candidate_is_excluded_without_global_rejection(mutation):
    snap, evidence, policy = economical()
    row = evidence['low']
    if mutation == 'quality': row['quality_sufficient'] = False
    if mutation == 'speed': row['speed']['upper'] = '9999'
    if mutation == 'statistic': row['speed']['statistic'] = 'mean'
    if mutation == 'missing': row['generation_cost_usd'] = None
    if mutation == 'stale': row['provenance']['valid_until'] = NOW
    if mutation == 'binding': row['configuration_sha256'] = 'b' * 64
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    assert plan['eligible']['low'] is False and plan['eligible']['mid'] is True
    assert SelectionPolicy(plan).decide('low_mid', 'open_source')['selected_tier'] == 'mid'


def test_wrong_provider_cannot_supply_the_other_models_price_or_speed():
    snap, evidence, policy = inputs()
    evidence['low']['source_metrics'] = [{'schema_version': 'qcs-source-v1',
        'model_id': 'example/low', 'provider': 'another-provider', 'raw_sha256': 'a' * 64}]
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    assert plan['excluded'][0]['reason'] == 'Source provider mismatch'


@pytest.mark.parametrize('mutation', ['holdout', 'missing_component', 'identity', 'expired', 'private'])
def test_invalid_whole_calibration_refuses_before_any_dispatch(mutation):
    snap, evidence, policy = inputs()
    if mutation == 'holdout': policy['calibration']['basis'] = 'formal_holdout'
    if mutation == 'missing_component': policy['calibration']['includes'].remove('later_classification')
    if mutation == 'identity': policy['calibration']['configurations']['mid'] = 'b' * 64
    if mutation == 'expired': policy['calibration']['valid_until'] = NOW
    if mutation == 'private': evidence['low']['hidden_answers'] = ['2']
    with pytest.raises(ValueError):
        prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)


def test_forecasts_are_frozen_and_decimal_context_cannot_flip_the_economics():
    from decimal import localcontext
    snap, evidence, policy = classifiable()
    selector = SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW))
    mutable = selector.plan
    mutable['policy']['stages']['low_mid']['direct_tier'] = 'low'
    with localcontext() as context:
        context.prec = 2
        assert selector.decide('low_mid', 'open_source')['net_saving_lower_usd'] == '0.080'


def test_no_quality_qualified_continuation_is_not_silently_replaced():
    snap, evidence, policy = inputs()
    evidence['low']['quality_sufficient'] = evidence['mid']['quality_sufficient'] = False
    with pytest.raises(ValueError, match='No QCS-qualified'):
        prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)


def test_within_pool_selection_uses_completion_cost_not_cheap_first_call():
    from copy import deepcopy
    snap, evidence, policy = inputs()
    alternative = deepcopy(snap['pools']['low'][0])
    alternative['config_id'] = 'misleading-cheap-first-call'
    snap['pools']['low'].append(alternative)
    snap['snapshot_id'] = digest({k: v for k, v in snap.items() if k != 'snapshot_id'})
    row = deepcopy(evidence['low'])
    row.update(generation_cost_usd=interval('0.001'), complete_cost_usd=interval('0.5'))
    evidence[alternative['config_id']] = row
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    assert plan['profiles']['low']['capability_card']['score_selection']['config_id'] == 'low'


@pytest.mark.parametrize('missing', [True, False])
def test_missing_or_incompatible_low_pool_does_not_block_mid(missing):
    snap, evidence, policy = inputs()
    if missing:
        snap['pools']['low'] = []
    else:
        snap['pools']['low'][0]['capabilities'] = []
    snap['snapshot_id'] = digest({k: v for k, v in snap.items() if k != 'snapshot_id'})
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    assert 'low' not in plan['profiles']
    assert SelectionPolicy(plan).decide('low_mid', 'open_source')['selected_tier'] == 'mid'


def test_stage_direct_forecast_cannot_omit_known_complete_cost():
    snap, evidence, policy = inputs()
    evidence['low']['generation_cost_usd'] = interval('.01')
    evidence['low']['complete_cost_usd'] = interval('.2')
    policy['stages']['low_mid']['paths']['low']['cost_usd'] = interval('.01')
    with pytest.raises(ValueError, match='complete cost'):
        prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)


def test_expensive_configured_direct_tier_cannot_inflate_classifier_savings():
    snap, evidence, policy = economical()
    choice = SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)).decide('low_mid', 'open_source')
    assert choice['action'] == 'direct' and choice['selected_tier'] == 'low'


@pytest.mark.parametrize('mutation', ['population', 'version', 'contract', 'stage', 'previous',
                                     'fallback', 'horizon', 'component', 'expired'])
def test_conditional_savings_require_same_independent_population_and_protocol(mutation):
    snap, evidence, policy = classifiable()
    calibration = policy['stages']['low_mid']['classified_calibration']
    if mutation == 'population': calibration['raw_sha256'] = 'c' * 64
    if mutation == 'version': calibration['classifier_version'] = 'different-classifier'
    if mutation == 'contract': calibration['contract_sha256'] = 'c' * 64
    if mutation == 'stage': calibration['stage'] = 'mid_high'
    if mutation == 'previous': calibration['previous_tier'] = 'mid'
    if mutation == 'fallback': calibration['effective_probability_includes_fallback'] = False
    if mutation == 'horizon': calibration['horizon'] = 'first_generation_only'
    if mutation == 'component': calibration['includes'] = ['generation']
    if mutation == 'expired': calibration['valid_until'] = NOW
    with pytest.raises(ValueError):
        prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)


def test_unlabelled_optimistic_classified_paths_are_not_used():
    snap, evidence, policy = economical()
    policy['stages']['low_mid']['classified_paths'] = {'low': {'cost_usd': interval('0')},
                                                    'mid': {'cost_usd': interval('0')}}
    result = SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)).decide('low_mid', 'open_source')
    assert result['action'] == 'direct' and result['selected_tier'] == 'low'


def test_equal_cost_but_faster_low_is_not_dominated_by_mid_preference():
    snap, evidence, policy = inputs()
    policy['stages']['low_mid']['paths']['low']['flow_ms_upper'] = '50'
    choice = SelectionPolicy(prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)).decide('low_mid', 'open_source')
    assert choice['action'] == 'direct' and choice['selected_tier'] == 'low'


def test_missing_candidate_provenance_does_not_poison_other_pools():
    snap, evidence, policy = inputs()
    evidence['low']['provenance'] = None
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    assert not plan['eligible']['low'] and plan['eligible']['mid']


def test_retrieval_today_does_not_refresh_an_expired_source_observation():
    snap, evidence, policy = inputs()
    evidence['low']['source_metrics'] = [{'schema_version': 'qcs-source-v1', 'model_id': 'example/low',
        'provider': None, 'retrieved_at': NOW, 'as_of': '2025-01-01T00:00:00Z', 'raw_sha256': 'a' * 64}]
    plan = prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    assert not plan['eligible']['low']
    assert plan['excluded'][0]['reason'] == 'Source observation stale'


def test_remaining_stage_timings_cannot_claim_whole_task_speed_admission():
    snap, evidence, policy = inputs()
    policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': 'p95', 'upper': '1800'}
    with pytest.raises(ValueError, match='whole-task'):
        _prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)


def test_whole_task_flow_calibration_cannot_follow_changed_policy_without_rebinding():
    snap, evidence, policy = inputs()
    prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
    policy['minimum_saving_usd'] = '999'
    with pytest.raises(ValueError, match='whole-task'):
        _prepare_qcs(snap, REQUIREMENTS, evidence, policy, now=NOW)
