"""Reapply one original decision from bounded, host-verified JSON values only.

The host authenticates and loads the original evidence before calling this module.
Hashes here are references, not authentication or permission to execute anything.
Actual selected tiers, prompts, answers and diagnostics are not input fields.
"""
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re


INPUT_SCHEMA = 'saved-policy-values-v1'
OUTPUT_SCHEMA = 'saved-policy-reproduction-v1'
COMPUTATION_VERSION = 'original-policy-values-v1'
BOUNDS = {'input_bytes': 131072, 'output_bytes': 16384, 'json_nodes': 8192,
          'json_depth': 12, 'array_items': 128, 'candidates': 12}
TIERS = ('open_source', 'low', 'mid', 'high')
STAGES = {'low_mid': ('low', 'mid'), 'mid_high': ('mid', 'high')}
POLICIES = ('hierarchical-sequential-v1', 'hierarchical-qcs-v1', 'qcs-backup-policy-v1')
INITIAL = {'cloudyfan': 'open_source', 'fixed_high': 'high',
           'low_first': 'low', 'open_source_first': 'open_source'}
CONTEXT_FIELDS = {'stage', 'previous_tier', 'previous_configuration_sha256',
                  'previous_public_passed', 'previous_stop', 'quality_phase',
                  'generation_calls', 'receipt_sha256', 'validation_sha256'}
SOURCE_FIELDS = {'binding_sha256', 'request_sha256', 'registered_sha256',
                 'policy_sha256', 'checkpoint_sha256', 'candidate_set_sha256'}
CANDIDATE_FIELDS = {'tier', 'config_id', 'configuration_sha256', 'generation_cost_usd',
                    'complete_cost_usd', 'flow_ms_upper', 'quality_score', 'gate'}
ECONOMIC_FIELDS = {'calibration', 'contract_sha256', 'expected_contract_sha256',
                   'direct_tier', 'paths', 'classified_paths', 'classifier_cost_usd',
                   'lower_probability', 'classify_flow_ms_upper', 'max_flow_ms',
                   'minimum_saving_usd', 'minimum_saving_fraction'}
CLASSIFIER_FIELDS = {'stage', 'contract_sha256', 'expected_contract_sha256',
                     'request_sha256', 'expected_request_sha256', 'response_sha256',
                     'receipt_sha256', 'context_sha256',
                     'expected_context_sha256', 'cost_known', 'parse_status',
                     'reported_choice', 'probabilities', 'abstain_reason',
                     'probability_decimals'}


class _Unavailable(ValueError):
    pass


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _copy_json(value):
    nodes = 0

    def copy(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > BOUNDS['json_nodes'] or depth > BOUNDS['json_depth']:
            raise _Unavailable('saved_policy_values_invalid')
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int and abs(item) <= 2**53 - 1:
            return item
        if kind is str and len(item) <= 256:
            return item
        if kind is list and len(item) <= BOUNDS['array_items']:
            return [copy(child, depth + 1) for child in item]
        if kind is dict and len(item) <= 64:
            if any(type(key) is not str or len(key) > 80 for key in item):
                raise _Unavailable('saved_policy_values_invalid')
            return {key: copy(child, depth + 1) for key, child in item.items()}
        raise _Unavailable('saved_policy_values_invalid')

    saved = copy(value, 0)
    if len(_encoded(saved)) > BOUNDS['input_bytes']:
        raise _Unavailable('saved_policy_values_invalid')
    return saved


def _keys(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise _Unavailable('saved_policy_values_invalid')


def _hash(value, *, nullable=False):
    if value is None and nullable:
        return
    if type(value) is not str or re.fullmatch(r'[a-f0-9]{64}', value) is None:
        raise _Unavailable('saved_policy_values_invalid')


def _identifier(value):
    if (type(value) is not str
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value) is None
            or '..' in value):
        raise _Unavailable('saved_policy_values_invalid')


def _decimal(value, *, signed=False, probability=False):
    pattern = r'-?(?:\d+(?:\.\d+)?|\.\d+)' if signed else r'(?:\d+(?:\.\d+)?|\.\d+)'
    if type(value) is not str or len(value) > 128 or re.fullmatch(pattern, value, re.ASCII) is None:
        raise _Unavailable('saved_policy_values_invalid')
    number = Decimal(value)
    if (not number.is_finite() or number.adjusted() > 64
            or number.as_tuple().exponent < -64 or probability and not 0 <= number <= 1):
        raise _Unavailable('saved_policy_values_invalid')
    return number


def _interval(value, *, probability=False):
    _keys(value, {'lower', 'upper'})
    lower, upper = (_decimal(value[key], probability=probability) for key in ('lower', 'upper'))
    if lower > upper:
        raise _Unavailable('saved_policy_values_invalid')
    return lower, upper


def _paths(value, *, nullable=False):
    if value is None and nullable:
        return
    if type(value) is not dict or len(value) > 2 or set(value) - set(TIERS):
        raise _Unavailable('saved_policy_values_invalid')
    for path in value.values():
        _keys(path, {'cost_usd', 'flow_ms_upper'})
        _interval(path['cost_usd'])
        _decimal(path['flow_ms_upper'])


def _candidate(value):
    _keys(value, CANDIDATE_FIELDS)
    if value['tier'] not in TIERS:
        raise _Unavailable('saved_policy_values_invalid')
    _identifier(value['config_id'])
    _hash(value['configuration_sha256'])
    generation = _interval(value['generation_cost_usd']) if value['generation_cost_usd'] is not None else None
    complete = _interval(value['complete_cost_usd']) if value['complete_cost_usd'] is not None else None
    if complete is not None and generation is not None and complete[1] < generation[1]:
        raise _Unavailable('policy_inputs_inconsistent')
    if value['flow_ms_upper'] is not None:
        _decimal(value['flow_ms_upper'])
    if value['quality_score'] is not None:
        _decimal(value['quality_score'], signed=True)
    gate = value['gate']
    _keys(gate, {'state', 'reasons', 'evidence_sha256'})
    reasons = gate['reasons']
    if (gate['state'] not in ('eligible', 'excluded', 'unknown') or type(reasons) is not list
            or len(reasons) > 8 or any(type(reason) is not str
                or re.fullmatch(r'[a-z0-9_]{1,80}', reason, re.ASCII) is None for reason in reasons)
            or gate['state'] == 'eligible' and reasons
            or gate['state'] == 'excluded' and not reasons):
        raise _Unavailable('saved_policy_values_invalid')
    _hash(gate['evidence_sha256'], nullable=True)


def _candidate_digest(candidates):
    identities = [{key: row[key] for key in ('tier', 'config_id', 'configuration_sha256')}
                  for row in candidates]
    identities.sort(key=lambda row: (row['tier'], row['config_id'], row['configuration_sha256']))
    return _digest(identities)


def candidate_set_digest(candidates: list) -> str:
    """Hash bounded candidate identities; the host must supply the original full set."""
    try:
        saved = _copy_json(candidates)
        if type(saved) is not list or len(saved) > BOUNDS['candidates']:
            raise _Unavailable('saved_policy_values_invalid')
        for row in saved:
            _candidate(row)
        return _candidate_digest(saved)
    except (ValueError, TypeError, KeyError, RuntimeError, ArithmeticError, RecursionError):
        raise ValueError('saved_policy_values_invalid') from None


def _context(value):
    _keys(value, CONTEXT_FIELDS)
    if (value['stage'] not in ('initial', 'same_pool', 'low_mid', 'mid_high', 'after_validation')
            or value['previous_tier'] is not None and value['previous_tier'] not in TIERS
            or value['previous_public_passed'] is not None and type(value['previous_public_passed']) is not bool
            or value['previous_stop'] not in ('none', 'validation_passed', 'validation_failed', 'validation_unknown',
                'provider_rate_limited', 'provider_unavailable', 'unknown_charge')
            or type(value['quality_phase']) is not int or not 0 <= value['quality_phase'] <= 2
            or type(value['generation_calls']) is not int or not 0 <= value['generation_calls'] <= 128):
        raise _Unavailable('saved_policy_values_invalid')
    for key in ('previous_configuration_sha256', 'receipt_sha256', 'validation_sha256'):
        _hash(value[key], nullable=True)


def _economics(value):
    if value is None:
        return
    _keys(value, ECONOMIC_FIELDS)
    if (value['calibration'] not in ('missing', 'present', 'unknown')
            or value['direct_tier'] is not None and value['direct_tier'] not in TIERS):
        raise _Unavailable('saved_policy_values_invalid')
    for key in ('contract_sha256', 'expected_contract_sha256'):
        _hash(value[key], nullable=True)
    _paths(value['paths'], nullable=True)
    _paths(value['classified_paths'], nullable=True)
    for key in ('classifier_cost_usd', 'lower_probability'):
        if value[key] is not None:
            _interval(value[key], probability=key == 'lower_probability')
    for key in ('classify_flow_ms_upper', 'max_flow_ms', 'minimum_saving_usd', 'minimum_saving_fraction'):
        if value[key] is None and key == 'classify_flow_ms_upper':
            continue
        _decimal(value[key], probability=key == 'minimum_saving_fraction')


def _classifier(value):
    if value is None:
        return
    _keys(value, CLASSIFIER_FIELDS)
    if (value['stage'] not in STAGES or type(value['cost_known']) is not bool
            or value['parse_status'] not in ('valid', 'invalid')
            or value['reported_choice'] is not None and value['reported_choice'] not in TIERS
            or value['abstain_reason'] not in (None, 'distribution_missing', 'explicit_abstention_choice')
            or value['probability_decimals'] is not None and (
                type(value['probability_decimals']) is not int or not 0 <= value['probability_decimals'] <= 18)):
        raise _Unavailable('saved_policy_values_invalid')
    for key in ('contract_sha256', 'expected_contract_sha256', 'request_sha256',
                'expected_request_sha256', 'response_sha256', 'receipt_sha256',
                'context_sha256', 'expected_context_sha256'):
        _hash(value[key], nullable=True)
    probabilities = value['probabilities']
    if probabilities is not None:
        _keys(probabilities, set(STAGES[value['stage']]))
        for probability in probabilities.values():
            _decimal(probability, probability=True)


def _validate(value):
    _keys(value, {'schema_version', 'policy_version', 'mode', 'source', 'context',
                  'candidates', 'economics', 'classifier'})
    if value['schema_version'] != INPUT_SCHEMA:
        raise _Unavailable('saved_policy_values_invalid')
    if value['policy_version'] not in POLICIES:
        raise _Unavailable('unsupported_policy_version')
    if value['mode'] not in INITIAL:
        raise _Unavailable('saved_policy_values_invalid')
    _keys(value['source'], SOURCE_FIELDS)
    for key in SOURCE_FIELDS:
        _hash(value['source'][key], nullable=True)
        if key != 'candidate_set_sha256' and value['source'][key] is None:
            raise _Unavailable('original_binding_unavailable')
    _context(value['context'])
    rows = value['candidates']
    if type(rows) is not list or len(rows) > BOUNDS['candidates']:
        raise _Unavailable('saved_policy_values_invalid')
    seen = set()
    for row in rows:
        _candidate(row)
        key = row['tier'], row['config_id']
        if key in seen:
            raise _Unavailable('candidate_scope_incomplete')
        seen.add(key)
    expected = value['source']['candidate_set_sha256']
    if (rows and expected is None) or expected is not None and expected != _candidate_digest(rows):
        raise _Unavailable('candidate_scope_incomplete')
    _economics(value['economics'])
    _classifier(value['classifier'])


def _result():
    return {'schema_version': OUTPUT_SCHEMA, 'computation_version': COMPUTATION_VERSION,
            'status': 'unknown', 'scope': 'one_saved_decision', 'execution_authorized': False,
            'policy_version': None, 'input_sha256': None, 'stage': None, 'action': None,
            'selected_tier': None, 'selected_configuration_sha256': None,
            'reason': 'saved_policy_values_invalid', 'policy_reason': None,
            'net_saving_lower_usd': None, 'classifier_interpretation': 'not_used'}


def _rank(row, policy):
    base = (_interval(row['complete_cost_usd'])[1], _decimal(row['flow_ms_upper']))
    score = _decimal(row['quality_score'], signed=True) if row['quality_score'] is not None else Decimal(0)
    return (*base, -score, row['config_id']) if policy == 'hierarchical-qcs-v1' else (*base, row['config_id'])


def _choices(value, tiers):
    rows = value['candidates']
    if value['source']['candidate_set_sha256'] is None:
        raise _Unavailable('candidate_scope_incomplete')
    if any(row['tier'] not in tiers for row in rows):
        raise _Unavailable('policy_inputs_inconsistent')
    if any(row['gate']['state'] == 'unknown' or row['gate']['evidence_sha256'] is None for row in rows):
        raise _Unavailable('candidate_gate_evidence_unavailable')
    result = {}
    for tier in tiers:
        eligible = [row for row in rows if row['tier'] == tier and row['gate']['state'] == 'eligible']
        if value['policy_version'] == 'hierarchical-sequential-v1' and len(eligible) > 1:
            raise _Unavailable('policy_inputs_inconsistent')
        if eligible:
            if value['policy_version'] == 'hierarchical-sequential-v1':
                result[tier] = eligible[0]
            else:
                if any(row[key] is None for row in eligible
                       for key in ('generation_cost_usd', 'complete_cost_usd', 'flow_ms_upper')):
                    raise _Unavailable('original_economics_unavailable')
                result[tier] = min(eligible, key=lambda row: _rank(row, value['policy_version']))
    return result


def _selected(result, tier, pair, reason):
    result.update(status='reproduced', action='direct', selected_tier=tier,
                  selected_configuration_sha256=pair[tier]['configuration_sha256'],
                  reason=reason, policy_reason=reason)
    return result


def _stopped(result, reason):
    result.update(status='reproduced', action='stop', reason=reason, policy_reason=reason)
    return result


def _economic_choice(value, stage, pair):
    economic = value['economics']
    if economic is None or economic['calibration'] == 'unknown':
        raise _Unavailable('original_economics_unavailable')
    backup = value['policy_version'] == 'qcs-backup-policy-v1'
    missing = backup and (economic['calibration'] == 'missing' or len(pair) == 1)
    if missing:
        paths = {tier: {'cost_usd': row['complete_cost_usd'], 'flow_ms_upper': row['flow_ms_upper']}
                 for tier, row in pair.items()}
    else:
        paths = economic['paths']
        if paths is None or not set(pair) <= set(paths) or set(paths) - set(STAGES[stage]):
            raise _Unavailable('original_economics_unavailable')
    for tier, row in pair.items():
        if (_interval(paths[tier]['cost_usd'])[1] < _interval(row['complete_cost_usd'])[1]
                or _decimal(paths[tier]['flow_ms_upper']) > _decimal(economic['max_flow_ms'])):
            raise _Unavailable('policy_inputs_inconsistent')
    preferred = economic['direct_tier']
    if not backup and preferred not in STAGES[stage]:
        raise _Unavailable('policy_inputs_inconsistent')
    direct = min(pair, key=lambda tier: (
        _interval(paths[tier]['cost_usd'])[1], _decimal(paths[tier]['flow_ms_upper']),
        tier != preferred, tier) if not backup else (
        _interval(paths[tier]['cost_usd'])[1], _decimal(paths[tier]['flow_ms_upper']), tier))
    if missing:
        return direct, 'exact_classifier_calibration_missing', None
    if not backup and len(pair) == 1:
        return direct, 'single_eligible_candidate', None
    if economic['classifier_cost_usd'] is None or economic['lower_probability'] is None:
        if backup:
            raise _Unavailable('original_economics_unavailable')
        return direct, 'classification_estimate_unknown', None
    conditional = economic['classified_paths']
    if backup or conditional is not None:
        if (economic['contract_sha256'] is None or economic['expected_contract_sha256'] is None
                or economic['contract_sha256'] != economic['expected_contract_sha256']):
            raise _Unavailable('original_economics_unavailable')
        if conditional is None or not set(pair) <= set(conditional) or set(conditional) - set(STAGES[stage]):
            raise _Unavailable('original_economics_unavailable')
        if any(_interval(conditional[tier]['cost_usd'])[1] < _interval(row['generation_cost_usd'])[1]
               for tier, row in pair.items()):
            raise _Unavailable('policy_inputs_inconsistent')
    classified = conditional if conditional is not None else paths
    lower, upper = STAGES[stage]
    if set(pair) != {lower, upper}:
        raise _Unavailable('policy_inputs_inconsistent')
    p0, p1 = _interval(economic['lower_probability'], probability=True)
    fee = _interval(economic['classifier_cost_usd'])[1]
    costs = {tier: _interval(classified[tier]['cost_usd'])[1] for tier in pair}
    expected = fee + max(p * costs[lower] + (1 - p) * costs[upper] for p in (p0, p1))
    direct_lower = _interval(paths[direct]['cost_usd'])[0]
    saving = direct_lower - expected
    needed = max(_decimal(economic['minimum_saving_usd']),
                 direct_lower * _decimal(economic['minimum_saving_fraction'], probability=True))
    elapsed = economic['classify_flow_ms_upper']
    if elapsed is None:
        if backup:
            raise _Unavailable('original_economics_unavailable')
        return direct, 'classified_flow_estimate_unknown', saving
    if _decimal(elapsed) > _decimal(economic['max_flow_ms']):
        return direct, 'classification_not_economic' if backup else 'classification_exceeds_speed_limit', saving
    return ((None, 'conservative_net_saving_positive', saving) if saving > needed
            else (direct, 'classification_not_economic', saving))


def _classified(value, stage, pair, result):
    saved = value['classifier']
    result.update(action='classify', classifier_interpretation='unavailable')
    if saved is None:
        result.update(status='partial', reason='classifier_evidence_unavailable')
        return result
    hashes = ('contract_sha256', 'expected_contract_sha256', 'request_sha256',
              'expected_request_sha256', 'response_sha256', 'receipt_sha256',
              'context_sha256', 'expected_context_sha256')
    if (saved['stage'] != stage or any(saved[key] is None for key in hashes)
            or saved['contract_sha256'] != saved['expected_contract_sha256']
            or saved['request_sha256'] != saved['expected_request_sha256']
            or saved['context_sha256'] != saved['expected_context_sha256']
            or value['economics'] is not None and value['economics']['contract_sha256'] is not None
            and saved['contract_sha256'] != value['economics']['contract_sha256']):
        result.update(status='partial', reason='classifier_binding_mismatch')
        return result
    if not saved['cost_known']:
        result.update(status='unknown', action='stop', reason='original_unknown_charge')
        return result
    lower, upper = STAGES[stage]
    if set(pair) != {lower, upper}:
        raise _Unavailable('policy_inputs_inconsistent')
    reason, tier = 'parse_failure', upper
    if saved['parse_status'] == 'invalid':
        if any(saved[key] is not None for key in ('reported_choice', 'probabilities', 'abstain_reason')):
            raise _Unavailable('policy_inputs_inconsistent')
    else:
        choice, probabilities, abstain = saved['reported_choice'], saved['probabilities'], saved['abstain_reason']
        if choice not in (lower, upper):
            raise _Unavailable('policy_inputs_inconsistent')
        if probabilities is None:
            if abstain not in ('distribution_missing', 'explicit_abstention_choice'):
                raise _Unavailable('policy_inputs_inconsistent')
        else:
            decimals = saved['probability_decimals']
            tolerance = Decimal('0.000001') + (Decimal(10) ** -decimals if decimals is not None else 0)
            if abs(sum(_decimal(item, probability=True) for item in probabilities.values()) - 1) > tolerance:
                raise _Unavailable('policy_inputs_inconsistent')
            if abstain == 'distribution_missing':
                raise _Unavailable('policy_inputs_inconsistent')
        if abstain is not None:
            reason = abstain
        elif _decimal(probabilities[choice], probability=True) <= Decimal('0.5'):
            reason = 'tied_or_inconsistent_distribution'
        else:
            reason, tier = 'reported_choice_applied', choice
    result.update(status='reproduced', selected_tier=tier,
                  selected_configuration_sha256=pair[tier]['configuration_sha256'],
                  reason=reason, classifier_interpretation='saved_response_rules_reapplied')
    return result


def _reproduce(value, result):
    context, mode, policy = value['context'], value['mode'], value['policy_version']
    stage = context['stage']
    result.update(policy_version=policy, stage=stage)
    if policy == 'hierarchical-qcs-v1' and mode != 'cloudyfan':
        raise _Unavailable('policy_inputs_inconsistent')
    if stage == 'initial':
        if (context['previous_tier'] is not None or context['previous_configuration_sha256'] is not None
                or context['previous_public_passed'] is not None or context['previous_stop'] != 'none'
                or context['quality_phase'] != 0 or context['generation_calls'] != 0
                or context['receipt_sha256'] is not None or context['validation_sha256'] is not None
                or value['economics'] is not None or value['classifier'] is not None):
            raise _Unavailable('original_context_unavailable')
        tier = INITIAL[mode]
        pair = _choices(value, (tier,))
        if tier not in pair:
            return _stopped(result, 'no_eligible_candidate')
        return _selected(result, tier, pair, 'same_pool_availability_or_initial'
                         if policy == 'qcs-backup-policy-v1' else 'original_mode_initial_tier')
    if context['previous_stop'] == 'unknown_charge':
        result.update(action='stop', reason='original_unknown_charge')
        return result
    if (context['previous_tier'] is None or context['previous_configuration_sha256'] is None
            or context['receipt_sha256'] is None or context['generation_calls'] < 1
            or context['previous_public_passed'] is not None and context['validation_sha256'] is None):
        raise _Unavailable('original_context_unavailable')
    if mode != 'cloudyfan' and context['quality_phase'] > 1:
        raise _Unavailable('policy_inputs_inconsistent')
    if context['previous_public_passed'] is True:
        if context['previous_stop'] not in ('none', 'validation_passed'):
            raise _Unavailable('policy_inputs_inconsistent')
        return _stopped(result, 'public_validation_passed')
    if (context['previous_stop'] == 'validation_failed' and context['previous_public_passed'] is not False
            or context['previous_stop'] == 'validation_unknown' and context['previous_public_passed'] is not None
            or context['previous_stop'] in ('none', 'validation_passed')):
        raise _Unavailable('policy_inputs_inconsistent')
    availability = context['previous_stop'] in ('provider_rate_limited', 'provider_unavailable')
    backup = policy == 'qcs-backup-policy-v1'
    if availability and context['previous_public_passed'] is not None:
        raise _Unavailable('policy_inputs_inconsistent')
    if backup and context['generation_calls'] > (3 if mode == 'cloudyfan' else 2):
        raise _Unavailable('policy_inputs_inconsistent')
    if backup and context['generation_calls'] >= (3 if mode == 'cloudyfan' else 2):
        return _stopped(result, 'generation_call_limit')
    if not backup and mode == 'fixed_high' and context['previous_stop'] == 'provider_rate_limited':
        return _stopped(result, 'provider_rate_limited')
    if stage == 'same_pool':
        if not backup or not availability or context['previous_public_passed'] is not None:
            raise _Unavailable('original_context_unavailable')
        tier = context['previous_tier']
        pair = _choices(value, (tier,))
        if tier in pair:
            return _selected(result, tier, pair, 'same_pool_availability_or_initial')
        if context['quality_phase'] < (2 if mode == 'cloudyfan' else 1):
            result.update(status='reproduced', action='advance_quality_stage',
                          reason='same_pool_exhausted', policy_reason='same_pool_exhausted')
            return result
        return _stopped(result, 'no_eligible_candidate')
    if backup and availability:
        raise _Unavailable('original_context_unavailable')
    if context['validation_sha256'] is None:
        raise _Unavailable('original_context_unavailable')
    if context['quality_phase'] >= (2 if mode == 'cloudyfan' else 1):
        return _stopped(result, 'quality_stage_limit' if backup else 'generation_attempt_limit')
    if mode != 'cloudyfan':
        if stage != 'after_validation':
            raise _Unavailable('policy_inputs_inconsistent')
        pair = _choices(value, ('high',))
        return (_selected(result, 'high', pair, 'original_baseline_high_recovery') if 'high' in pair
                else _stopped(result, 'no_eligible_candidate'))
    expected_stage = 'low_mid' if context['quality_phase'] == 0 else 'mid_high'
    if stage not in (expected_stage, 'after_validation') or (
            expected_stage == 'low_mid' and context['previous_tier'] != 'open_source'
            or expected_stage == 'mid_high' and context['previous_tier'] not in ('low', 'mid')):
        raise _Unavailable('policy_inputs_inconsistent')
    stage = expected_stage
    result['stage'] = stage
    pair = _choices(value, STAGES[stage])
    if not pair:
        return _stopped(result, 'no_eligible_candidate')
    if policy == 'hierarchical-sequential-v1':
        if value['economics'] is not None:
            raise _Unavailable('policy_inputs_inconsistent')
        result['policy_reason'] = 'original_sequential_classifier'
        return _classified(value, stage, pair, result)
    direct, reason, saving = _economic_choice(value, stage, pair)
    result.update(policy_reason=reason, net_saving_lower_usd=None if saving is None else format(saving, 'f'))
    if direct is not None:
        return _selected(result, direct, pair, reason)
    return _classified(value, stage, pair, result)


def reproduce_original_policy(values: dict) -> dict:
    """Recompute one decision; unavailable evidence yields stable, content-free reasons.

    The caller must verify original provenance before constructing values. This
    function cannot authenticate hashes or confer current execution authority.
    """
    result = _result()
    try:
        saved = _copy_json(values)
        _validate(saved)
        result['input_sha256'] = _digest(saved)
        with localcontext() as arithmetic:
            arithmetic.prec = 512
            result = _reproduce(saved, result)
        if len(_encoded(result)) > BOUNDS['output_bytes']:
            raise _Unavailable('saved_policy_values_invalid')
        return result
    except _Unavailable as error:
        reason = error.args[0]
    except (ValueError, TypeError, KeyError, RuntimeError, InvalidOperation, ArithmeticError, RecursionError):
        reason = 'saved_policy_values_invalid'
    failed = _result()
    failed['reason'] = reason
    return failed
