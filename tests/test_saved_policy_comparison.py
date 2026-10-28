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
