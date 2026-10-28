"""Pure, bounded, observation-only recommendations from frozen safe values."""
import hashlib
import json
import math
import re
from decimal import Decimal, localcontext


SCHEMA = 'creation-routing-recommendation-v1'
ENVELOPE = 'creation-task-observation-v2'
RULE_VERSION = 'route-recommendation-rules-v1'
DATA_VERSION = 'creation-recommendation-data-v1'
BOUNDS = {
    'pure_context_bytes': 65536, 'candidate_summaries': 12, 'conditional_variants': 128,
    'recommendation_bytes': 8192, 'suggestions': 3, 'reasons_per_suggestion': 4,
    'ascii_code_characters': 80, 'new_envelope_bytes': 98304,
    'legacy_record_and_adoption_bytes': 16384, 'shared_total_new_pure_compute_milliseconds': 50,
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _identifier(value):
    return isinstance(value, str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value) is not None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def policy(value):
    """A fixed host configuration; no arbitrary trust flags or input features."""
    if (not isinstance(value, dict) or set(value) != {'enabled', 'rule_version', 'data_policy_version', 'bounds'}
            or type(value['enabled']) is not bool or value['rule_version'] != RULE_VERSION
            or value['data_policy_version'] != DATA_VERSION or not isinstance(value['bounds'], dict)
            or set(value['bounds']) != set(BOUNDS)
            or any(type(value['bounds'][key]) is not int or value['bounds'][key] != expected
                   for key, expected in BOUNDS.items())):
        raise ValueError('Unregistered recommendation policy')
    return json.loads(encoded(value))


def enabled_policy():
    return {'enabled': True, 'rule_version': RULE_VERSION,
            'data_policy_version': DATA_VERSION, 'bounds': dict(BOUNDS)}


def adoption(value):
    keys = {'rule_version', 'data_policy_version', 'policy_sha256', 'context_sha256'}
    if (not isinstance(value, dict) or set(value) != keys or value['rule_version'] != RULE_VERSION
            or value['data_policy_version'] != DATA_VERSION
            or value['policy_sha256'] != digest(enabled_policy()) or not _hash(value['context_sha256'])):
        raise ValueError('Invalid original recommendation adoption')
    return value


def context(value):
    keys = {'schema_version', 'contract_id', 'registered_sha256', 'category', 'mode',
            'calibration_status', 'source_kind'}
    if isinstance(value, dict) and value.get('schema_version') in ('creation-routing-context-v2', 'creation-routing-context-v3'):
        keys |= {'calibration_summary'}
        calibration_summary(value.get('calibration_summary'), candidate_time=value['schema_version'] == 'creation-routing-context-v3')
    if (not isinstance(value, dict) or set(value) != keys
            or value['schema_version'] not in ('creation-routing-context-v1', 'creation-routing-context-v2', 'creation-routing-context-v3')
            or not _identifier(value['contract_id']) or not _hash(value['registered_sha256'])
            or value['category'] is not None and not _identifier(value['category'])
            or value['mode'] not in ('cloudyfan', 'fixed_high', 'low_first', 'open_source_first')
            or value['calibration_status'] not in ('missing', 'unverified', 'verified', 'unsupported')
            or value['source_kind'] not in ('not_provided', 'observed_calibration', 'offline_fixture')
            or len(encoded(value)) > BOUNDS['pure_context_bytes']):
        raise ValueError('Invalid frozen recommendation context')
    return value


def _number(value, *, signed=False):
    pattern = r'-?(?:\d+(?:\.\d+)?|\.\d+)' if signed else r'(?:\d+(?:\.\d+)?|\.\d+)'
    if not isinstance(value, str) or len(value) > 128 or re.fullmatch(pattern, value) is None:
        raise ValueError('Bounded plain decimal required')
    result = Decimal(value)
    if result.adjusted() > 64 or result.as_tuple().exponent < -64:
        raise ValueError('Decimal precision outside bounds')
    return result


def _interval(value, *, signed=False):
    if not isinstance(value, dict) or set(value) != {'lower', 'upper'}:
        raise ValueError('Explicit complete interval required')
    low, high = _number(value['lower'], signed=signed), _number(value['upper'], signed=signed)
    if low > high:
        raise ValueError('Reversed interval')
    return low, high


def calibration_summary(value, *, candidate_time=False):
    fields = {'reason', 'valid_from', 'valid_until', 'sources', 'variants',
              'minimum_saving_usd', 'minimum_saving_fraction', 'max_flow_ms'}
    if candidate_time:
        fields.add('candidate_validity')
    if (not isinstance(value, dict) or set(value) != fields
            or not isinstance(value['reason'], str) or not re.fullmatch('[a-z0-9_]{1,80}', value['reason'])
            or not isinstance(value['sources'], list) or len(value['sources']) > 16
            or not isinstance(value['variants'], list) or len(value['variants']) > BOUNDS['conditional_variants']
            or any(item is not None and not _finite(item) for item in (value['valid_from'], value['valid_until']))):
        raise ValueError('Invalid safe exact calibration summary')
    if candidate_time and value['candidate_validity'] is not None:
        window = value['candidate_validity']
        if (not isinstance(window, dict) or set(window) != {'valid_from', 'valid_until', 'valid_through'}
                or not _finite(window['valid_from']) or not _finite(window['valid_until'])
                or window['valid_through'] is not None and not _finite(window['valid_through'])):
            raise ValueError('Invalid original candidate time bounds')
    for source in value['sources']:
        if (not isinstance(source, dict) or set(source) != {'sha256', 'bytes'} or not _hash(source['sha256'])
                or type(source['bytes']) is not int or not 0 <= source['bytes'] <= 262144):
            raise ValueError('Invalid actual source byte summary')
    if sum(row['bytes'] for row in value['sources']) > 1048576:
        raise ValueError('Actual source byte total exceeded')
    _number(value['minimum_saving_usd'])
    if _number(value['minimum_saving_fraction']) > 1:
        raise ValueError('Invalid minimum saving fraction')
    _number(value['max_flow_ms'])
    for row in value['variants']:
        if (not isinstance(row, dict) or set(row) != {'contract_sha256', 'stage', 'previous_tier',
                'paths', 'classified_paths', 'classifier_cost_usd', 'lower_probability', 'classify_flow_ms_upper'}
                or not _hash(row['contract_sha256']) or row['stage'] not in ('low_mid', 'mid_high')
                or row['previous_tier'] not in ('open_source', 'low', 'mid')):
            raise ValueError('Invalid exact conditional scope')
        tiers = ('low', 'mid') if row['stage'] == 'low_mid' else ('mid', 'high')
        if set(row['paths']) != set(tiers) or set(row['classified_paths']) != set(tiers):
            raise ValueError('Incomplete conditional continuation')
        for tier in tiers:
            if set(row['paths'][tier]) != {'cost_usd', 'flow_ms_upper'} or set(row['classified_paths'][tier]) != {'cost_usd'}:
                raise ValueError('Invalid complete continuation summary')
            _interval(row['paths'][tier]['cost_usd'])
            _number(row['paths'][tier]['flow_ms_upper'])
            _interval(row['classified_paths'][tier]['cost_usd'])
        _interval(row['classifier_cost_usd'])
        if _interval(row['lower_probability'])[1] > 1:
            raise ValueError('Invalid calibration distribution')
        _number(row['classify_flow_ms_upper'])
    return value


def frozen_input(value):
    keys = {'schema_version', 'rule_version', 'data_policy_version', 'captured_at',
            'feature_sha256', 'binding', 'context'}
    if isinstance(value, dict) and value.get('schema_version') == 'creation-routing-input-v2':
        keys |= {'feature_summary'}
        summary = value.get('feature_summary')
        if (not isinstance(summary, list) or not 1 <= len(summary) <= 16
                or any(not isinstance(item, dict) or set(item) != {'id', 'kind', 'source', 'unit', 'value', 'reasons'}
                       for item in summary)):
            raise ValueError('Invalid original feature summary')
    if (not isinstance(value, dict) or set(value) != keys
            or value['schema_version'] not in ('creation-routing-input-v1', 'creation-routing-input-v2')
            or value['rule_version'] != RULE_VERSION or value['data_policy_version'] != DATA_VERSION
            or not _finite(value['captured_at']) or not _hash(value['feature_sha256'])
            or len(encoded(value)) > BOUNDS['pure_context_bytes']):
        raise ValueError('Invalid frozen recommendation input')
    binding = value['binding']
    saved = context(value['context'])
    if (not isinstance(binding, dict) or set(binding) != {'category', 'mode', 'contract_id', 'registered_sha256'}
            or not _identifier(binding['category'])
            or any(binding[key] != saved[key] for key in ('mode', 'contract_id', 'registered_sha256'))
            or saved['category'] is not None and binding['category'] != saved['category']):
        raise ValueError('Frozen recommendation scope differs')
    return value


def unavailable():
    return {'schema_version': SCHEMA, 'status': 'unavailable', 'adopted': False,
            'reason': 'creation_recommendation_evidence_unavailable'}


def not_provided():
    return {'schema_version': SCHEMA, 'status': 'not_provided', 'adopted': False,
            'reason': 'creation_recommendation_not_provided'}


def public_recommendation(value):
    if value == unavailable() or value == not_provided():
        return value
    keys = {'schema_version', 'rule_version', 'data_policy_version', 'captured_at', 'status',
            'adopted', 'feature_sha256', 'context_sha256', 'input_sha256', 'suggestions'}
    if (not isinstance(value, dict) or set(value) != keys or value['schema_version'] != SCHEMA
            or value['rule_version'] != RULE_VERSION or value['data_policy_version'] != DATA_VERSION
            or value['status'] not in ('unknown', 'not_applicable', 'available', 'unsupported') or value['adopted'] is not False
            or not _finite(value['captured_at'])
            or any(not _hash(value[key]) for key in ('feature_sha256', 'context_sha256', 'input_sha256'))
            or len(encoded(value)) > BOUNDS['recommendation_bytes']):
        raise ValueError('Invalid saved recommendation')
    suggestions = value['suggestions']
    if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= BOUNDS['suggestions']:
        raise ValueError('Unsupported saved recommendation kinds')
    kinds, seen = {'observe_original_route', 'need_capability_context_evidence', 'compare_skipping_classification'}, set()
    reasons = {'exact_classifier_calibration_missing', 'exact_classifier_calibration_unverified',
               'original_mode_has_no_classification', 'exact_input_tokens_unknown',
               'declared_context_is_not_measured_usage', 'classification_proven_not_economic',
               'conditional_saving_estimated_positive', 'conditional_saving_interval_uncertain',
               'exact_classifier_calibration_expired_at_acceptance', 'exact_classifier_calibration_later_than_acceptance',
               'exact_calibration_source_unavailable', 'exact_calibration_scope_or_cost_invalid',
               'exact_calibration_bounds_unsupported', 'classification_flow_time_exceeds_original_limit',
               'exact_calibration_source_schema_unsupported'}
    reasons |= {'candidate_evidence_expired_at_acceptance', 'candidate_evidence_later_than_acceptance',
                'exact_calibration_execution_contract_mismatch', 'exact_calibration_execution_adapter_unsupported'}
    for row in suggestions:
        if (not isinstance(row, dict) or set(row) not in ({'kind', 'economics', 'reasons'}, {'kind', 'economics', 'reasons', 'comparison'})
                or row['kind'] not in kinds or row['kind'] in seen
                or row['economics'] not in ('unknown', 'proven_insufficient', 'estimated_positive', 'uncertain')
                or not isinstance(row['reasons'], list) or not 1 <= len(row['reasons']) <= BOUNDS['reasons_per_suggestion']
                or any(reason not in reasons for reason in row['reasons'])):
            raise ValueError('Invalid safe recommendation reasons')
        if 'comparison' in row:
            comparison = row['comparison']
            if (not isinstance(comparison, dict) or set(comparison) != {'source_kind', 'conditional_scopes',
                    'net_saving_usd', 'source_sha256', 'distribution_is_answer_probability'}
                    or comparison['source_kind'] not in ('offline_fixture', 'observed_calibration')
                    or type(comparison['conditional_scopes']) is not int or not 1 <= comparison['conditional_scopes'] <= 128
                    or comparison['distribution_is_answer_probability'] is not False
                    or not isinstance(comparison['source_sha256'], list) or not 1 <= len(comparison['source_sha256']) <= 16
                    or any(not _hash(item) for item in comparison['source_sha256'])):
                raise ValueError('Invalid conditional economic comparison')
            _interval(comparison['net_saving_usd'], signed=True)
        seen.add(row['kind'])
    return json.loads(encoded(value))


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def client_summary(value):
    """Own-task kinds/reasons only; full calibration parameters are operator data."""
    safe = public_recommendation(value)
    if 'suggestions' not in safe:
        return dict(safe)
    return {**safe, 'suggestions': [{key: row[key] for key in ('kind', 'economics', 'reasons')}
                                  for row in safe['suggestions']]}


def _economics(value):
    current = value['context']
    summary = current['calibration_summary']
    if summary['valid_from'] is None or summary['valid_until'] is None or not summary['variants'] or not summary['sources']:
        raise ValueError('Verified exact scope unavailable')
    if value['captured_at'] < summary['valid_from']:
        return None, 'exact_classifier_calibration_later_than_acceptance'
    if value['captured_at'] >= summary['valid_until']:
        return None, 'exact_classifier_calibration_expired_at_acceptance'
    window = summary.get('candidate_validity')
    if window is not None:
        if value['captured_at'] < window['valid_from']:
            return None, 'candidate_evidence_later_than_acceptance'
        if (value['captured_at'] >= window['valid_until']
                or window['valid_through'] is not None and value['captured_at'] > window['valid_through']):
            return None, 'candidate_evidence_expired_at_acceptance'
    intervals, profitable, insufficient = [], [], []
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        for row in summary['variants']:
            tiers = ('low', 'mid') if row['stage'] == 'low_mid' else ('mid', 'high')
            direct = min(tiers, key=lambda tier: (_interval(row['paths'][tier]['cost_usd'])[1],
                _number(row['paths'][tier]['flow_ms_upper']), tier))
            d0, d1 = _interval(row['paths'][direct]['cost_usd'])
            p0, p1 = _interval(row['lower_probability'])
            f0, f1 = _interval(row['classifier_cost_usd'])
            l0, l1 = _interval(row['classified_paths'][tiers[0]]['cost_usd'])
            u0, u1 = _interval(row['classified_paths'][tiers[1]]['cost_usd'])
            c0 = f0 + min(p*l0 + (1-p)*u0 for p in (p0, p1))
            c1 = f1 + max(p*l1 + (1-p)*u1 for p in (p0, p1))
            low, high = d0-c1, d1-c0
            # Original frozen QCS policy applies the fraction to direct lower cost.
            margin = max(_number(summary['minimum_saving_usd']), _number(summary['minimum_saving_fraction'])*d0)
            intervals.append((low, high))
            profitable.append(low > margin)
            insufficient.append(high <= margin)
            if _number(row['classify_flow_ms_upper']) > _number(summary['max_flow_ms']):
                return None, 'classification_flow_time_exceeds_original_limit'
    result = {'source_kind': current['source_kind'], 'conditional_scopes': len(intervals),
        'net_saving_usd': {'lower': _text(min(row[0] for row in intervals)), 'upper': _text(max(row[1] for row in intervals))},
        'source_sha256': sorted({row['sha256'] for row in summary['sources']}), 'distribution_is_answer_probability': False}
    reason = ('classification_proven_not_economic' if all(insufficient) else
              'conditional_saving_estimated_positive' if all(profitable) else 'conditional_saving_interval_uncertain')
    return result, reason


def recommend(value):
    """No sources are verified by this rule; absence never proves a loss."""
    value = frozen_input(value)
    applicable = value['context']['mode'] == 'cloudyfan'
    reason = ('exact_classifier_calibration_missing' if value['context']['calibration_status'] == 'missing'
              else 'exact_classifier_calibration_unverified')
    if not applicable:
        reason = 'original_mode_has_no_classification'
    suggestions = [{'kind': 'observe_original_route', 'economics': 'unknown', 'reasons': [reason]}]
    status = 'unknown' if applicable else 'not_applicable'
    if applicable and value['context']['schema_version'] in ('creation-routing-context-v2', 'creation-routing-context-v3'):
        calibration = value['context']['calibration_status']
        if calibration == 'verified':
            comparison, reason = _economics(value)
            if comparison is not None:
                kind, economics, status = ('compare_skipping_classification', 'proven_insufficient', 'available') if reason == 'classification_proven_not_economic' else (
                    'observe_original_route', 'estimated_positive' if reason == 'conditional_saving_estimated_positive' else 'uncertain',
                    'available' if reason == 'conditional_saving_estimated_positive' else 'unknown')
                suggestions[0] = {'kind': kind, 'economics': economics, 'reasons': [reason], 'comparison': comparison}
            else:
                suggestions[0]['reasons'] = [reason]
        elif calibration in ('unsupported', 'unverified'):
            status = 'unsupported' if calibration == 'unsupported' else 'unknown'
            suggestions[0]['reasons'] = [value['context']['calibration_summary']['reason']]
    features = {item['id']: item for item in value.get('feature_summary', [])}
    needs = []
    if features.get('input_tokens', {}).get('kind') == 'unknown':
        needs.append('exact_input_tokens_unknown')
        if features.get('context_requirements', {}).get('kind') == 'explicit':
            needs.append('declared_context_is_not_measured_usage')
    if needs:
        suggestions.append({'kind': 'need_capability_context_evidence', 'economics': 'unknown', 'reasons': needs})
    return public_recommendation({
        'schema_version': SCHEMA, 'rule_version': value['rule_version'],
        'data_policy_version': value['data_policy_version'], 'captured_at': value['captured_at'],
        'status': status, 'adopted': False, 'feature_sha256': value['feature_sha256'],
        'context_sha256': digest(value['context']), 'input_sha256': digest(value),
        'suggestions': suggestions,
    })
