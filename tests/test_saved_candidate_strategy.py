"""Registered candidate strategies against saved decisions; no runner, network or answers."""
import json

import pytest

from cloudyfan_jev.saved_candidate_strategy import STRATEGIES, evaluate_candidate_strategy
from cloudyfan_jev.saved_policy_comparison import candidate_set_digest, reproduce_original_policy


def _values(*, policy='hierarchical-qcs-v1', calibration='present', direct_tier='mid',
            fee='.001', classify_flow='110', reply=True, low_complete='.2', low_classified='.01'):
    candidates = [
        {'tier': tier, 'config_id': 'synthetic-' + tier, 'configuration_sha256': identity * 64,
         'generation_cost_usd': {'lower': generation, 'upper': generation},
         'complete_cost_usd': {'lower': complete, 'upper': complete},
         'flow_ms_upper': '100', 'quality_score': None,
         'gate': {'state': 'eligible', 'reasons': [], 'evidence_sha256': 'c' * 64}}
        for tier, identity, generation, complete in [('low', 'd', '.01', low_complete), ('mid', 'e', '.1', '.1')]
    ]
    return {
        'schema_version': 'saved-policy-values-v1', 'policy_version': policy, 'mode': 'cloudyfan',
        'source': {**{key: 'a' * 64 for key in ('binding_sha256', 'request_sha256',
                    'registered_sha256', 'policy_sha256', 'checkpoint_sha256')},
                   'candidate_set_sha256': candidate_set_digest(candidates)},
        'context': {'stage': 'low_mid', 'previous_tier': 'open_source',
                    'previous_configuration_sha256': 'b' * 64, 'previous_public_passed': False,
                    'previous_stop': 'validation_failed', 'quality_phase': 0, 'generation_calls': 1,
                    'receipt_sha256': 'a' * 64, 'validation_sha256': 'b' * 64},
        'candidates': candidates,
        'economics': {'calibration': calibration, 'contract_sha256': 'f' * 64,
                      'expected_contract_sha256': 'f' * 64, 'direct_tier': direct_tier,
                      'paths': {'low': {'cost_usd': {'lower': low_complete, 'upper': low_complete},
                                        'flow_ms_upper': '100'},
                                'mid': {'cost_usd': {'lower': '.1', 'upper': '.1'}, 'flow_ms_upper': '100'}},
                      'classified_paths': {'low': {'cost_usd': {'lower': low_classified, 'upper': low_classified}},
                                           'mid': {'cost_usd': {'lower': '.1', 'upper': '.1'}}},
                      'classifier_cost_usd': {'lower': fee, 'upper': fee},
                      'lower_probability': {'lower': '.9', 'upper': '.9'},
                      'classify_flow_ms_upper': classify_flow, 'max_flow_ms': '2000',
                      'minimum_saving_usd': '0', 'minimum_saving_fraction': '0'},
        'classifier': None if not reply else {
            'stage': 'low_mid',
            **{key: 'f' * 64 for key in ('contract_sha256', 'expected_contract_sha256',
                'request_sha256', 'expected_request_sha256', 'response_sha256',
                'receipt_sha256', 'context_sha256', 'expected_context_sha256')},
            'cost_known': True, 'parse_status': 'valid', 'reported_choice': 'low',
            'probabilities': {'low': '.9', 'mid': '.1'}, 'abstain_reason': None,
            'probability_decimals': None},
    }


def test_direct_strategy_stops_at_the_first_divergence_from_a_saved_classification():
    values = _values()
    assert reproduce_original_policy(values)['action'] == 'classify'

    result = evaluate_candidate_strategy(values, 'qcs-direct-v1')

    assert (result['status'], result['action'], result['selected_tier']) == ('evaluated', 'direct', 'mid')
    assert result['diverged'] is True and result['stop'] == 'requires_unsaved_alternative_answer'
    assert result['original']['action'] == 'classify' and result['original']['selected_tier'] == 'low'
    assert result['forecast']['complete_flow_usd'] == {'lower': '0.1', 'upper': '0.1'}
    assert result['forecast']['classifier_fee_usd'] == {'lower': '0.001', 'upper': '0.001'}
    assert result['forecast']['calibration'] == 'present'
    assert result['execution_authorized'] is False and result['adopted'] is False


def test_classify_strategy_with_the_same_saved_request_reuses_the_saved_reply_rules():
    result = evaluate_candidate_strategy(_values(), 'qcs-classify-v1')

    assert (result['status'], result['action'], result['selected_tier']) == ('evaluated', 'classify', 'low')
    assert result['diverged'] is False and result['stop'] is None
    assert result['forecast']['complete_flow_usd'] == {'lower': '0.020', 'upper': '0.020'}


def test_classify_strategy_reports_insufficient_net_saving_without_an_unsaved_reply():
    # A costly classifier makes the original pick direct; the candidate would
    # need a Jev reply that was never requested, so it stops and stays unknown.
    values = _values(fee='.5', reply=False)
    original = reproduce_original_policy(values)
    assert (original['action'], original['policy_reason']) == ('direct', 'classification_not_economic')

    result = evaluate_candidate_strategy(values, 'qcs-classify-v1')

    assert (result['status'], result['action'], result['selected_tier']) == ('evaluated', 'classify', None)
    assert result['diverged'] is True and result['stop'] == 'requires_unsaved_classifier_reply'
    assert result['forecast']['net_saving_lower_usd'] == '-0.419'
    assert 'success' not in json.dumps(result) and 'win' not in json.dumps(result)


def test_missing_exact_calibration_is_not_reported_as_an_uneconomic_classifier():
    values = _values(policy='qcs-backup-policy-v1', calibration='missing', reply=False)
    assert reproduce_original_policy(values)['policy_reason'] == 'exact_classifier_calibration_missing'

    classify = evaluate_candidate_strategy(values, 'qcs-classify-v1')
    direct = evaluate_candidate_strategy(values, 'qcs-direct-v1')

    assert (classify['status'], classify['reason']) == ('not_evaluable', 'exact_classifier_calibration_missing')
    assert classify['forecast']['complete_flow_usd'] is None
    assert (direct['status'], direct['diverged'], direct['stop']) == ('evaluated', False, None)


def test_classification_over_the_original_speed_limit_is_not_adoptable():
    result = evaluate_candidate_strategy(_values(classify_flow='2001', reply=False), 'qcs-classify-v1')

    assert (result['status'], result['reason'], result['action']) == (
        'not_adoptable', 'classification_exceeds_speed_limit', None)


def test_decisions_outside_a_qcs_decision_point_match_the_original():
    values = _values(reply=False)
    values['context'] = {'stage': 'initial', 'previous_tier': None, 'previous_configuration_sha256': None,
                         'previous_public_passed': None, 'previous_stop': 'none', 'quality_phase': 0,
                         'generation_calls': 0, 'receipt_sha256': None, 'validation_sha256': None}
    values['economics'] = None
    values['candidates'] = [dict(values['candidates'][0], tier='open_source', config_id='synthetic-oss')]
    values['source']['candidate_set_sha256'] = candidate_set_digest(values['candidates'])

    for strategy in STRATEGIES:
        result = evaluate_candidate_strategy(values, strategy)
        assert (result['status'], result['reason'], result['selected_tier'], result['diverged']) == (
            'evaluated', 'same_as_original_outside_decision_point', 'open_source', False)


@pytest.mark.parametrize('strategy', ['original-policy-reproduction-v1', 'lambda: 1', None, 'QCS-DIRECT-V1'])
def test_only_registered_strategies_are_accepted(strategy):
    result = evaluate_candidate_strategy(_values(), strategy)

    assert (result['status'], result['reason'], result['strategy']) == (
        'not_evaluable', 'unregistered_candidate_strategy', None)


def test_hidden_answer_or_grader_fields_are_rejected_as_policy_input():
    for extra in ({'hidden_answer': '2'}, {'grader': {'passed': True}}, {'alternative_answer': 'x'}):
        values = {**_values(), **extra}
        result = evaluate_candidate_strategy(values, 'qcs-direct-v1')
        assert (result['status'], result['reason']) == ('not_evaluable', 'saved_policy_values_invalid')


def test_unreproduced_original_decision_is_not_evaluable():
    values = _values()
    values['economics']['calibration'] = 'unknown'

    result = evaluate_candidate_strategy(values, 'qcs-direct-v1')

    assert (result['status'], result['reason']) == ('not_evaluable', 'original_decision_not_reproduced')
