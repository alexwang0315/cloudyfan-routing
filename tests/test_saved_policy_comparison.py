"""Public original-policy comparison contracts; no runner, network or private helpers."""
import pytest

from cloudyfan_jev.saved_policy_comparison import candidate_set_digest, reproduce_original_policy


@pytest.mark.parametrize('policy,calibration,status,reason,tier', [
    ('hierarchical-qcs-v1', 'missing', 'unknown', 'original_economics_unavailable', None),
    ('hierarchical-qcs-v1', 'unknown', 'unknown', 'original_economics_unavailable', None),
    ('hierarchical-qcs-v1', 'present', 'reproduced', 'classification_not_economic', 'low'),
    ('qcs-backup-policy-v1', 'missing', 'reproduced', 'exact_classifier_calibration_missing', 'low'),
], ids=['qcs-missing', 'qcs-unknown', 'qcs-present-control', 'backup-missing-control'])
def test_original_qcs_requires_present_calibration_without_changing_backup_fallback(
        policy, calibration, status, reason, tier):
    candidates = [
        {'tier': candidate_tier, 'config_id': 'synthetic-' + candidate_tier,
         'configuration_sha256': identity * 64,
         'generation_cost_usd': {'lower': '0.1', 'upper': '0.1'},
         'complete_cost_usd': {'lower': lower, 'upper': upper},
         'flow_ms_upper': '100', 'quality_score': None,
         'gate': {'state': 'eligible', 'reasons': [], 'evidence_sha256': 'c' * 64}}
        for candidate_tier, identity, lower, upper in [('low', 'd', '1', '2'), ('mid', 'e', '2', '3')]
    ]
    values = {
        'schema_version': 'saved-policy-values-v1', 'policy_version': policy, 'mode': 'cloudyfan',
        'source': {**{key: 'a' * 64 for key in ('binding_sha256', 'request_sha256',
                    'registered_sha256', 'policy_sha256', 'checkpoint_sha256')},
                   'candidate_set_sha256': candidate_set_digest(candidates)},
        'context': {'stage': 'low_mid', 'previous_tier': 'open_source',
                    'previous_configuration_sha256': 'b' * 64, 'previous_public_passed': False,
                    'previous_stop': 'validation_failed', 'quality_phase': 0, 'generation_calls': 1,
                    'receipt_sha256': 'a' * 64, 'validation_sha256': 'b' * 64},
        'candidates': candidates,
        # Numeric fields deliberately remain present when calibration is missing.
        'economics': {'calibration': calibration, 'contract_sha256': 'f' * 64,
                      'expected_contract_sha256': 'f' * 64, 'direct_tier': 'low',
                      'paths': {'low': {'cost_usd': {'lower': '1', 'upper': '2'}, 'flow_ms_upper': '100'},
                                'mid': {'cost_usd': {'lower': '2', 'upper': '3'}, 'flow_ms_upper': '100'}},
                      'classified_paths': None, 'classifier_cost_usd': {'lower': '1', 'upper': '1'},
                      'lower_probability': {'lower': '0.5', 'upper': '0.5'},
                      'classify_flow_ms_upper': '1000', 'max_flow_ms': '2000',
                      'minimum_saving_usd': '0', 'minimum_saving_fraction': '0'},
        'classifier': None,
    }

    result = reproduce_original_policy(values)

    assert (result['status'], result['reason'], result['selected_tier']) == (status, reason, tier)
    assert result['execution_authorized'] is False


def _saved_classified_values():
    # The original CategoryCore producer stores cost-only classified_paths and
    # one separate classify_flow_ms_upper, not a forecast for each branch.
    candidates = [
        {'tier': tier, 'config_id': 'synthetic-' + tier, 'configuration_sha256': identity * 64,
         'generation_cost_usd': {'lower': generation, 'upper': generation},
         'complete_cost_usd': {'lower': complete, 'upper': complete},
         'flow_ms_upper': '100', 'quality_score': None,
         'gate': {'state': 'eligible', 'reasons': [], 'evidence_sha256': 'c' * 64}}
        for tier, identity, generation, complete in [('low', 'd', '.01', '.2'), ('mid', 'e', '.1', '.1')]
    ]
    return {
        'schema_version': 'saved-policy-values-v1', 'policy_version': 'hierarchical-qcs-v1',
        'mode': 'cloudyfan',
        'source': {**{key: 'a' * 64 for key in ('binding_sha256', 'request_sha256',
                    'registered_sha256', 'policy_sha256', 'checkpoint_sha256')},
                   'candidate_set_sha256': candidate_set_digest(candidates)},
        'context': {'stage': 'low_mid', 'previous_tier': 'open_source',
                    'previous_configuration_sha256': 'b' * 64, 'previous_public_passed': False,
                    'previous_stop': 'validation_failed', 'quality_phase': 0, 'generation_calls': 1,
                    'receipt_sha256': 'a' * 64, 'validation_sha256': 'b' * 64},
        'candidates': candidates,
        'economics': {'calibration': 'present', 'contract_sha256': 'f' * 64,
                      'expected_contract_sha256': 'f' * 64, 'direct_tier': 'mid',
                      'paths': {'low': {'cost_usd': {'lower': '.2', 'upper': '.2'}, 'flow_ms_upper': '100'},
                                'mid': {'cost_usd': {'lower': '.1', 'upper': '.1'}, 'flow_ms_upper': '100'}},
                      'classified_paths': {'low': {'cost_usd': {'lower': '.01', 'upper': '.01'}},
                                           'mid': {'cost_usd': {'lower': '.1', 'upper': '.1'}}},
                      'classifier_cost_usd': {'lower': '.001', 'upper': '.001'},
                      'lower_probability': {'lower': '.9', 'upper': '.9'},
                      'classify_flow_ms_upper': '110', 'max_flow_ms': '2000',
                      'minimum_saving_usd': '0', 'minimum_saving_fraction': '0'},
        'classifier': {'stage': 'low_mid',
                       **{key: 'f' * 64 for key in ('contract_sha256', 'expected_contract_sha256',
                           'request_sha256', 'expected_request_sha256', 'response_sha256',
                           'receipt_sha256', 'context_sha256', 'expected_context_sha256')},
                       'cost_known': True, 'parse_status': 'valid', 'reported_choice': 'low',
                       'probabilities': {'low': '.9', 'mid': '.1'}, 'abstain_reason': None,
                       'probability_decimals': None},
    }


@pytest.mark.parametrize('flow', ['absent', None, '110'], ids=['original-cost-only', 'nullable-flow', 'legacy-flow'])
def test_original_classified_costs_use_the_separate_complete_flow_forecast(flow):
    values = _saved_classified_values()
    if flow != 'absent':
        for path in values['economics']['classified_paths'].values():
            path['flow_ms_upper'] = flow

    result = reproduce_original_policy(values)

    assert (result['status'], result['action'], result['selected_tier']) == ('reproduced', 'classify', 'low')
    assert result['policy_reason'] == 'conservative_net_saving_positive'
    assert result['net_saving_lower_usd'] == '0.080'
    assert result['execution_authorized'] is False


@pytest.mark.parametrize('section,path', [
    ('classified_paths', {'cost_usd': {'lower': '.01', 'upper': '.01'}, 'unexpected': '110'}),
    ('classified_paths', {'flow_ms_upper': '110'}),
    ('classified_paths', {'cost_usd': {'lower': '.01', 'upper': '.01'}, 'flow_ms_upper': '-1'}),
    ('classified_paths', {'cost_usd': {'lower': '.01', 'upper': '.01'}, 'flow_ms_upper': 110}),
    ('paths', {'cost_usd': {'lower': '.2', 'upper': '.2'}}),
], ids=['extra-field', 'missing-cost', 'negative-flow', 'numeric-flow', 'direct-flow-still-required'])
def test_original_classified_cost_shape_remains_strict_and_direct_flow_is_required(section, path):
    values = _saved_classified_values()
    values['economics'][section]['low'] = path

    result = reproduce_original_policy(values)

    assert (result['status'], result['reason'], result['selected_tier']) == (
        'unknown', 'saved_policy_values_invalid', None)
    assert result['execution_authorized'] is False
