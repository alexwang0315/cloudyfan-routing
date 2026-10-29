"""Evaluate one registered candidate strategy against one saved original decision.

Pure and bounded like saved_policy_comparison: the host verifies provenance and
passes the same value DTO. Only server-registered QCS direct/classify judgements
exist; there is no code, model, tool, validator or network capability here.
Forecasts are the original saved cost model, never observed alternative results.
"""
from decimal import InvalidOperation, localcontext

from .saved_policy_comparison import (
    BOUNDS, STAGES, _Unavailable, _choices, _copy_json, _decimal, _digest, _encoded,
    _interval, _validate, reproduce_original_policy,
)


OUTPUT_SCHEMA = 'saved-candidate-strategy-v1'
COMPUTATION_VERSION = 'candidate-strategy-divergence-v1'
STRATEGIES = {
    'qcs-direct-v1': '在可付費分類的決策點一律直接選模',
    'qcs-classify-v1': '有精確校準與估計時一律付費分類',
}
# Original QCS reasons that mark a saved direct/classify economic decision point.
DECISION_POINT_REASONS = {
    'exact_classifier_calibration_missing', 'single_eligible_candidate',
    'classification_estimate_unknown', 'classified_flow_estimate_unknown',
    'classification_exceeds_speed_limit', 'classification_not_economic',
    'conservative_net_saving_positive',
}


def _result(strategy):
    return {'schema_version': OUTPUT_SCHEMA, 'computation_version': COMPUTATION_VERSION,
            'strategy': strategy, 'status': 'not_evaluable', 'reason': 'saved_policy_values_invalid',
            'input_sha256': None,
            'action': None, 'selected_tier': None, 'selected_configuration_sha256': None,
            'original': None, 'diverged': None, 'stop': None,
            'forecast': {'complete_flow_usd': None, 'classifier_fee_usd': None,
                         'net_saving_lower_usd': None, 'calibration': None,
                         'calibration_contract_sha256': None},
            'execution_authorized': False, 'adopted': False}


def _text(value):
    return format(value, 'f')


def _span(lower, upper):
    return {'lower': _text(lower), 'upper': _text(upper)}


def _economics(value, stage, pair):
    """Forecast components from the original saved QCS cost model only."""
    economic = value['economics']
    backup = value['policy_version'] == 'qcs-backup-policy-v1'
    missing = economic['calibration'] == 'missing' or backup and len(pair) == 1
    if missing:
        paths = {tier: {'cost_usd': row['complete_cost_usd'], 'flow_ms_upper': row['flow_ms_upper']}
                 for tier, row in pair.items()}
    else:
        paths = economic['paths']
    # Same ordering as the original _economic_choice direct selection.
    preferred = economic['direct_tier']
    direct = min(pair, key=lambda tier: (
        _interval(paths[tier]['cost_usd'])[1], _decimal(paths[tier]['flow_ms_upper']),
        False if backup else tier != preferred, tier))
    parts = {'calibration': economic['calibration'], 'direct': direct,
             'direct_flow': _interval(paths[direct]['cost_usd']),
             'contract': economic['contract_sha256'], 'classified': None, 'fee': None,
             'flow_ms': economic['classify_flow_ms_upper'], 'max_flow_ms': economic['max_flow_ms']}
    lower, upper = STAGES[stage]
    if (missing or set(pair) != {lower, upper} or economic['classifier_cost_usd'] is None
            or economic['lower_probability'] is None):
        return parts
    classified = economic['classified_paths'] if economic['classified_paths'] is not None else paths
    if not set(pair) <= set(classified):
        return parts
    p0, p1 = _interval(economic['lower_probability'], probability=True)
    fee = _interval(economic['classifier_cost_usd'])
    low, up = _interval(classified[lower]['cost_usd']), _interval(classified[upper]['cost_usd'])
    # Linear in p, so both probability endpoints bound the mixture.
    parts['fee'] = fee
    parts['classified'] = (fee[0] + min(p * low[0] + (1 - p) * up[0] for p in (p0, p1)),
                           fee[1] + max(p * low[1] + (1 - p) * up[1] for p in (p0, p1)))
    return parts


def _forecast(result, parts, *, flow):
    forecast = result['forecast']
    forecast.update(calibration=parts['calibration'], calibration_contract_sha256=parts['contract'],
                    complete_flow_usd=None if flow is None else _span(*flow))
    if parts['fee'] is not None:
        forecast['classifier_fee_usd'] = _span(*parts['fee'])
        forecast['net_saving_lower_usd'] = _text(parts['direct_flow'][0] - parts['classified'][1])


def _evaluate(value, strategy, result):
    original = reproduce_original_policy(value)
    result['original'] = {key: original[key] for key in ('status', 'stage', 'action', 'selected_tier', 'reason',
                                                          'policy_reason')}
    # A partial original still fixes its action when only the saved reply is missing.
    if original['status'] != 'reproduced' and not (original['status'] == 'partial' and original['action'] == 'classify'):
        result['reason'] = 'original_decision_not_reproduced'
        return result
    stage = original['stage']
    point = (value['policy_version'] in ('hierarchical-qcs-v1', 'qcs-backup-policy-v1')
             and stage in STAGES and original['policy_reason'] in DECISION_POINT_REASONS)
    if not point:
        # The registered strategies only change the QCS direct/classify judgement.
        result.update(status='evaluated', reason='same_as_original_outside_decision_point',
                      action=original['action'], selected_tier=original['selected_tier'],
                      selected_configuration_sha256=original['selected_configuration_sha256'], diverged=False)
        return result
    pair = _choices(value, STAGES[stage])
    parts = _economics(value, stage, pair)
    if strategy == 'qcs-direct-v1':
        tier = parts['direct']
        result.update(status='evaluated', reason='registered_direct_choice', action='direct', selected_tier=tier,
                      selected_configuration_sha256=pair[tier]['configuration_sha256'])
        _forecast(result, parts, flow=parts['direct_flow'])
    else:
        if parts['calibration'] != 'present':
            result['reason'] = 'exact_classifier_calibration_missing'
            _forecast(result, parts, flow=None)
            return result
        if len(pair) == 1:
            result['reason'] = 'single_eligible_candidate'
            _forecast(result, parts, flow=None)
            return result
        if parts['classified'] is None or parts['flow_ms'] is None:
            result['reason'] = 'classification_estimate_unknown'
            _forecast(result, parts, flow=None)
            return result
        if _decimal(parts['flow_ms']) > _decimal(parts['max_flow_ms']):
            # The original speed limit is a gate, not a preference to relax.
            result.update(status='not_adoptable', reason='classification_exceeds_speed_limit')
            _forecast(result, parts, flow=None)
            return result
        result.update(status='evaluated', reason='registered_classification', action='classify')
        _forecast(result, parts, flow=parts['classified'])
    if result['action'] == original['action'] == 'classify':
        # Same classification request; the original saved reply rules decide the tier.
        result.update(selected_tier=original['selected_tier'],
                      selected_configuration_sha256=original['selected_configuration_sha256'], diverged=False)
    elif result['action'] == original['action']:
        result['diverged'] = result['selected_tier'] != original['selected_tier']
    else:
        result['diverged'] = True
    if result['diverged']:
        result['stop'] = ('requires_unsaved_classifier_reply' if result['action'] == 'classify'
                          else 'requires_unsaved_alternative_answer')
    return result


def evaluate_candidate_strategy(values: dict, strategy: str) -> dict:
    """Compare one registered strategy with one saved decision; no execution authority."""
    result = _result(strategy if strategy in STRATEGIES else None)
    if strategy not in STRATEGIES:
        result['reason'] = 'unregistered_candidate_strategy'
        return result
    try:
        saved = _copy_json(values)
        _validate(saved)
        result['input_sha256'] = _digest(saved)
        with localcontext() as arithmetic:
            arithmetic.prec = 512
            result = _evaluate(saved, strategy, result)
        if len(_encoded(result)) > BOUNDS['output_bytes']:
            raise _Unavailable('saved_policy_values_invalid')
        return result
    except _Unavailable as error:
        reason = error.args[0]
    except (ValueError, TypeError, KeyError, RuntimeError, InvalidOperation, ArithmeticError, RecursionError):
        reason = 'saved_policy_values_invalid'
    failed = _result(strategy)
    failed['reason'] = reason
    return failed


__all__ = ['COMPUTATION_VERSION', 'OUTPUT_SCHEMA', 'STRATEGIES', 'evaluate_candidate_strategy']
