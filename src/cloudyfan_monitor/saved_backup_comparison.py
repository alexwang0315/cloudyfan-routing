"""Reapply saved Backup decision points without a runner or current observation."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from cloudyfan_jev.saved_policy_comparison import candidate_set_digest

from .saved_comparisons import SourceUnavailable, digest, original_digest, LIMITS

POLICY = 'qcs-backup-policy-v1'
INITIAL = {'cloudyfan': 'open_source', 'fixed_high': 'high', 'low_first': 'low', 'open_source_first': 'open_source'}
PAIRS = {'low_mid': ('low', 'mid'), 'mid_high': ('mid', 'high')}
AVAILABILITY = {'provider_rate_limited', 'provider_unavailable'}
COMPLETE_PATH = {'generation', 'failure', 'retry', 'repair', 'escalation', 'later_classification', 'cache', 'reasoning'}
ENVELOPE = {'generation_calls': {'cloudyfan': 3, 'baseline': 2}, 'classifier_calls': 2,
    'fallback': 'same_pool_known_fee_no_answer_429_5xx', 'unknown_charge': 'stop',
    'quality_failure': 'next_sequential_quality_stage', 'parallel_dispatch': False}
FROZEN_EXTRA_REASONS = {'candidate_qualification_stale_or_invalid', 'local_backend_not_registered',
    'cloud_free_trial_not_explicitly_authorized', 'remaining_task_time_insufficient'}
ERRORS = (OSError, ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error, SourceUnavailable)


def _require(value, reason):
    if not value:
        raise SourceUnavailable(reason)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _rank(record):
    return (Decimal(record['evidence']['complete_cost_usd']['upper']),
            Decimal(record['evidence']['speed']['upper']), record['entry']['config_id'])


def _candidates(source, trusted, manifest, saved, tiers, used, failed):
    pools = manifest['backup_plan']['pools']
    _require(set(saved['health']) == set(tiers) and not set(saved['pair']) - set(tiers),
             'original_candidate_scope_invalid')
    _require(sum(len(pools[tier]) for tier in tiers) <= LIMITS['max_candidate_configurations'],
             'original_candidate_scope_invalid')
    candidates, pair = [], {}
    for tier in tiers:
        rows, observations = pools[tier], saved['health'][tier]
        _require([row['entry']['config_id'] for row in rows] == [row['config_id'] for row in observations],
                 'original_candidate_scope_invalid')
        eligible = []
        for record, observation in zip(rows, observations, strict=True):
            health, profile = observation['assessment'], record['entry']['profile']
            _require(isinstance(health, dict) and _hash(health.get('snapshot_id')),
                     'original_health_evidence_invalid')
            historical = source.rows(trusted['health_path'], 'SELECT payload FROM snapshots WHERE id=?',
                (health['snapshot_id'],), kind='original_backup_health_snapshot', maximum=1048576, max_rows=1)
            _require(len(historical) == 1, 'original_health_evidence_invalid')
            # source.read/rows already applies strict bounded JSON and immutable DB capture.
            from .saved_comparisons import strict_json
            snapshot = strict_json(historical[0]['payload'])
            _require(digest(snapshot) == health['snapshot_id']
                and snapshot.get('schema_version') == 'provider-health-v1'
                and snapshot['policy_sha256'] == digest(manifest['health_policy'])
                and health['observation'] in snapshot['observations']
                and health['observation']['configuration_sha256'] == record['configuration_sha256']
                and health['observation']['provider'] == profile['provider']
                and type(health['eligible']) is bool and health['eligible'] == (not health['reasons']),
                'original_health_evidence_invalid')
            reasons = observation['reasons']
            _require(isinstance(reasons, list) and len(reasons) <= 8
                and all(isinstance(reason, str) and re.fullmatch(r'[a-z0-9_]{1,80}', reason) for reason in reasons)
                and len(reasons) == len(set(reasons)), 'original_gate_evidence_invalid')
            expected = set(health['reasons'])
            if profile['provider'] in failed:
                expected.add('provider_failed_this_run')
            if record['configuration_sha256'] in used:
                expected.add('configuration_already_dispatched')
            window = observation.get('capacity_window')
            if window is not None:
                from .saved_window_comparison import verify_recorded_window
                window_reason = verify_recorded_window(source, trusted, window, profile)
                if window_reason is not None:
                    expected.add(window_reason)
            _require(expected <= set(reasons) and not (set(reasons) - expected - FROZEN_EXTRA_REASONS),
                     'original_gate_evidence_invalid')
            if not reasons:
                eligible.append(record)
            candidates.append({'tier': tier, 'config_id': record['entry']['config_id'],
                'configuration_sha256': record['configuration_sha256'],
                'generation_cost_usd': record['evidence']['generation_cost_usd'],
                'complete_cost_usd': record['evidence']['complete_cost_usd'],
                'flow_ms_upper': record['evidence']['speed']['upper'], 'quality_score': None,
                'gate': {'state': 'excluded' if reasons else 'eligible', 'reasons': reasons,
                         'evidence_sha256': digest(observation)}})
        if eligible:
            pair[tier] = min(eligible, key=_rank)
    _require(saved['pair'] == pair, 'original_candidate_choice_inconsistent')
    return candidates, pair


def _contract(manifest, stage, previous, pair):
    # Root routing imports only decisions/errors/stdlib, unlike scores.__init__.
    # This hash template receives public placeholders, never an original answer.
    from cloudyfan_jev import routing
    fields = (*routing.PROFILE_FIELDS, *routing.OPTIONAL_IDENTITY_FIELDS)
    public = lambda value: {key: deepcopy(value[key]) for key in fields if key in value}
    state = {'task': '<public-workload-task>', 'previous_answer': '<latest-public-answer>',
        'previous_tier': previous['tier'], 'previous_configuration': public(previous['profile']),
        'public_diagnostics': '<latest-public-diagnostics>', 'tool_results': []}
    _, _, payload = routing.build_request(stage, state,
        {tier: record['entry']['profile'] for tier, record in pair.items()},
        manifest['decision']['version'], manifest['decision']['version'], policy_version=POLICY)
    key = digest({'payload': payload, 'effective_fallback': 'upper_on_invalid_abstain_or_tie',
                  'unknown_charge': 'stop', 'horizon': 'remaining_after_previous_validation'})
    return key, payload


def _economics(manifest, stage, previous, pair, saved):
    policy, plan = manifest['backup_plan']['qcs_policy'], manifest['backup_plan']
    key, template = _contract(manifest, stage, previous, pair) if len(pair) == 2 else (None, None)
    _require(saved['economic'].get('contract_sha256') == key, 'original_classifier_contract_mismatch')
    variant = policy.get('stages', {}).get(stage, {}).get('variants', {}).get(key)
    if variant is not None:
        calibration = variant['calibration']
        pool = {key: value for key, value in plan.items() if key not in ('qcs_policy', 'backup_id')}
        _require(calibration['category_id'] == plan['category_id']
            and calibration['workload_id'] == policy['workload_id']
            and calibration['raw_sha256'] == policy['complete_policy_flow']['provenance']['raw_sha256']
            and calibration['contract_sha256'] == key
            and calibration['pool_contract_sha256'] == digest({'pool': pool, 'envelope': ENVELOPE})
            and calibration['previous_configuration_sha256'] == previous['configuration_sha256']
            and calibration['previous_tier'] == previous['tier'] and calibration['stage'] == stage
            and calibration['classifier_version'] == manifest['decision']['version'] == policy['classifier_version']
            and calibration['context'] == 'public_failure_or_unknown'
            and calibration['horizon'] == 'remaining_after_previous_validation'
            and set(calibration['includes']) == COMPLETE_PATH
            and calibration['effective_probability_includes_fallback'] is True,
            'original_classifier_contract_mismatch')
    return {'calibration': 'present' if variant is not None else 'missing',
        'contract_sha256': key, 'expected_contract_sha256': key, 'direct_tier': None,
        'paths': deepcopy(variant['paths']) if variant is not None else None,
        'classified_paths': deepcopy(variant['classified_paths']) if variant is not None else None,
        'classifier_cost_usd': deepcopy(variant['classifier_cost_usd']) if variant is not None else None,
        'lower_probability': deepcopy(variant['lower_probability']) if variant is not None else None,
        'classify_flow_ms_upper': variant['classify_flow_ms_upper'] if variant is not None else None,
        'max_flow_ms': policy['max_flow_ms'], 'minimum_saving_usd': policy.get('minimum_saving_usd', '0'),
        'minimum_saving_fraction': policy.get('minimum_saving_fraction', '0')}, template


def _generation_verified(bundle):
    return isinstance(bundle, dict) and _hash(bundle.get('receipts_sha256')) and bool(bundle.get('calls'))


def _number(value, *, probability=False):
    _require(type(value) in (str, int, float, Decimal), 'classifier_response_invalid')
    number = Decimal(str(value))
    _require(number.is_finite() and number >= 0 and (not probability or number <= 1),
             'classifier_response_invalid')
    return number


def _response_values(raw, stage, version, provider):
    """Extract only reply enums/numbers; no raw response is passed to Core."""
    invalid = {'parse_status': 'invalid', 'reported_choice': None, 'probabilities': None,
               'abstain_reason': None, 'probability_decimals': None}
    try:
        def pairs(rows):
            result = {}
            for key, value in rows:
                _require(key not in result, 'classifier_response_invalid')
                result[key] = value
            return result

        def constant(_):
            raise SourceUnavailable('classifier_response_invalid')

        data = json.loads(raw, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=constant)
        _require(isinstance(data, dict) and 'error' not in data and data.get('model') == version
            and isinstance(data.get('answers'), dict) and set(data['answers']) == {stage},
            'classifier_response_invalid')
        answer = data['answers'][stage]
        _require(isinstance(answer, dict) and answer.get('type') == 'choice'
            and not set(answer) - {'type', 'choice', 'probabilities', 'confidence'}
            and answer.get('choice') in PAIRS[stage], 'classifier_response_invalid')
        if 'confidence' in answer:
            _number(answer['confidence'], probability=True)
        decimals = 2 if version in {'typesafe/jev-1.13', 'typesafe/jev-1.13-20260917'} and provider in {'TypeSafe', 'typesafe'} else None
        probabilities = answer.get('probabilities')
        if 'probabilities' in answer:
            _require(isinstance(probabilities, dict) and set(probabilities) == set(PAIRS[stage]),
                     'classifier_response_invalid')
            probabilities = {key: _number(value, probability=True) for key, value in probabilities.items()}
            tolerance = Decimal('0.000001') + (Decimal(10) ** -decimals if decimals is not None else 0)
            _require(abs(sum(probabilities.values())-1) <= tolerance, 'classifier_response_invalid')
        usage = data.get('usage')
        _require(isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0
            for key in ('input_tokens', 'output_tokens')), 'classifier_response_invalid')
        if 'cost' in usage:
            _number(usage['cost'])
        return {'parse_status': 'valid', 'reported_choice': answer['choice'],
            'probabilities': {key: format(value, 'f') for key, value in probabilities.items()} if probabilities is not None else None,
            'abstain_reason': 'distribution_missing' if probabilities is None else None,
            'probability_decimals': decimals}
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, SourceUnavailable):
        return invalid


def _classifier(source, registered, manifest, result, saved, stage, previous, template, contract_sha):
    profile = manifest['decision']
    cid = 'run-' + result['run_id'] + '-decision-' + stage
    calls = [call for call in result['selection']['calls'] if call.get('call_id') == cid]
    _require(len(calls) == 1 and type(result['selection']['classifier_calls']) is int
        and result['selection']['classifier_calls'] == len(result['selection']['calls']),
             'classifier_evidence_unavailable')
    call = calls[0]
    _require(call.get('tier') == 'classifier' and call.get('profile') == profile, 'classifier_binding_mismatch')
    # receipt_root comes from the original registered descriptor, never HTTP.
    root = Path(registered['receipt_root']) / 'calls' / cid
    intent = source.read(root / 'intent.json', 'original_backup_classifier_intent')
    response = source.read(root / 'response.json', 'original_backup_classifier_response')
    response_sha = source.evidence[-1]['sha256']
    raw = response.get('raw')
    _require(intent.get('call_id') == response.get('call_id') == cid and isinstance(raw, str)
        and intent.get('url') == 'https://openrouter.ai/api/alpha/decisions'
        and original_digest({'url': intent['url'], 'payload': intent['payload']}) == intent.get('request_sha256')
        and response.get('request_sha256') == intent['request_sha256'] and response.get('http_status') == 200,
        'classifier_binding_mismatch')
    expected = deepcopy(template)
    expected['state']['task'] = manifest['task']['prompt']
    expected['state']['observations']['previous_answer'] = previous['final']
    expected['state']['observations']['public_diagnostics'] = previous['public']
    expected['state']['observations']['tool_results'] = [
        {key: action[key] for key in ('name', 'arguments', 'result')} for action in saved['actions']]
    _require(intent['payload'] == expected, 'classifier_binding_mismatch')
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        decoded = {}
    _require(isinstance(decoded, dict) and call.get('generation_id') == decoded.get('id')
        and call.get('model') == decoded.get('model')
        and call.get('provider') == decoded.get('provider')
        and decoded.get('model') in {profile['model'], profile['version']}
        and decoded.get('provider') == profile['provider_name'], 'classifier_binding_mismatch')
    usage = decoded.get('usage')
    cost_known = isinstance(usage, dict) and usage.get('cost') is not None
    if cost_known:
        _require(_number(usage['cost']) == _number(call.get('cost_usd')), 'classifier_binding_mismatch')
    _require(type(call.get('cost_unknown')) is bool and call['cost_unknown'] is not cost_known,
             'classifier_binding_mismatch')
    parsed = _response_values(raw, stage, profile['version'], decoded.get('provider'))
    return {'stage': stage, 'contract_sha256': contract_sha, 'expected_contract_sha256': contract_sha,
        'request_sha256': intent['request_sha256'], 'expected_request_sha256': original_digest({
            'url': intent['url'], 'payload': expected}),
        'response_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'receipt_sha256': response_sha,
        'context_sha256': digest(intent['payload']['state']), 'expected_context_sha256': digest(expected['state']),
        'cost_known': cost_known, **parsed}


def _classifier_saved_agrees(saved, classifier, reproduced, stage):
    _require(saved.get('stage') == stage and saved.get('selected_tier') == reproduced['selected_tier']
        and saved.get('reported_choice') == classifier['reported_choice']
        and saved.get('probabilities') == classifier['probabilities']
        and type(saved.get('fallback')) is bool
        and saved['fallback'] == (reproduced['reason'] != 'reported_choice_applied')
        and saved.get('fallback_reason') == (None if reproduced['reason'] == 'reported_choice_applied'
                                             else reproduced['reason']), 'original_saved_decision_mismatch')


def _context(path, attempts, receipts, index, stage, phase):
    if not index:
        return {'stage': 'initial', 'previous_tier': None, 'previous_configuration_sha256': None,
            'previous_public_passed': None, 'previous_stop': 'none', 'quality_phase': 0,
            'generation_calls': 0, 'receipt_sha256': None, 'validation_sha256': None}
    previous, bundle = attempts[index-1], receipts[index-1]
    _require(bundle['status'] == 'verified' and _hash(bundle['validation_sha256']),
             'original_context_receipts_unavailable')
    passed = previous['public']['passed']
    stop = (previous['stop'] if previous['stop'] in AVAILABILITY else
            'validation_passed' if passed is True else 'validation_failed' if passed is False else 'validation_unknown')
    if not all(call['cost_known'] for call in bundle['calls']):
        stop = 'unknown_charge'
    return {'stage': stage, 'previous_tier': path[index-1]['tier'],
        'previous_configuration_sha256': path[index-1]['configuration_sha256'],
        'previous_public_passed': passed, 'previous_stop': stop, 'quality_phase': phase,
        'generation_calls': sum(len(attempt['calls']) for attempt in attempts[:index]),
        'receipt_sha256': bundle['receipts_sha256'], 'validation_sha256': bundle['validation_sha256']}


def _point(index, stage, reproduced, candidates, match):
    return {'index': index, 'stage': stage, 'action': reproduced['action'],
        'selected_tier': reproduced['selected_tier'],
        'config_id': next((row['config_id'] for row in candidates
            if row['configuration_sha256'] == reproduced['selected_configuration_sha256']), None),
        'reason': reproduced['reason'], 'policy_reason': reproduced['policy_reason'], 'match': match}


def _unknown_tail(decisions, path, stage, reason, extra_points=0):
    total = min(len(path)+1+extra_points, LIMITS['max_decision_points'])
    if len(decisions) >= total:
        return
    decisions.append({'index': len(decisions), 'stage': stage, 'action': None,
        'selected_tier': None, 'config_id': None, 'reason': reason, 'match': None})
    while len(decisions) < total:
        decisions.append({'index': len(decisions), 'stage': None, 'action': None,
            'selected_tier': None, 'config_id': None, 'reason': 'original_context_unavailable', 'match': None})


def reproduce_saved_policy(source, binding, job, manifest, result, run, trusted, *, original_path, receipts):
    _require(manifest['routing_version'] == POLICY, 'unsupported_policy_version')
    attempts, path, values, decisions = result['attempts'], original_path, [], []
    used, failed, phase, selected_count = set(), set(), 0, 0
    attempt_index, checkpoint_index, extra_points, exhaustion_proof = 0, 0, 0, None
    # The original producer advances its checkpoint cursor even when an empty
    # same-pool choice dispatches nothing. Its result selection list omits that
    # choice, so neither cursor can be derived from the generation attempt.
    while attempt_index < len(path) or result['termination_reason'] == 'no_eligible_candidate':
        actual = path[attempt_index] if attempt_index < len(path) else None
        stage = 'initial' if not attempt_index else ('same_pool' if not exhaustion_proof
            and attempts[attempt_index-1]['stop'] in AVAILABILITY else
            ('low_mid' if phase == 0 else 'mid_high') if binding['mode'] == 'cloudyfan' else 'after_validation')
        try:
            _require(len(decisions) < LIMITS['max_decision_points'], 'comparison_decision_limit')
            checkpoint = source.read(run / f'backup-selection-{checkpoint_index}.json', 'original_backup_selection')
            checkpoint_index += 1
            checkpoint_sha = source.evidence[-1]['sha256']
            empty_same_pool = stage == 'same_pool' and checkpoint['pair'] == {}
            if empty_same_pool and (actual is not None or phase < (2 if binding['mode'] == 'cloudyfan' else 1)):
                extra_points += 1
            previous = attempts[attempt_index-1] if attempt_index else None
            expected_previous = {key: previous[key] for key in ('tier', 'profile', 'final', 'public')} if previous else None
            _require(checkpoint['previous'] == expected_previous
                and checkpoint['actions'] == result['actions'][:len(checkpoint['actions'])], 'original_context_unavailable')
            tiers = (INITIAL[binding['mode']],) if not attempt_index else (
                (path[attempt_index-1]['tier'],) if stage == 'same_pool' else PAIRS.get(stage, ('high',)))
            candidates, pair = _candidates(source, trusted, manifest, checkpoint, tiers, used, failed)
            economics, template = _economics(manifest, stage, previous, pair, checkpoint) if stage in PAIRS else (None, None)
            context = _context(path, attempts, receipts, attempt_index, stage, phase)
            value = {'schema_version': 'saved-policy-values-v1', 'policy_version': POLICY, 'mode': binding['mode'],
                'source': {'binding_sha256': digest(binding), 'request_sha256': original_digest(job['request']),
                    'registered_sha256': original_digest(job['registered']), 'policy_sha256': original_digest(manifest),
                    'checkpoint_sha256': checkpoint_sha, 'candidate_set_sha256': candidate_set_digest(candidates)},
                'context': context, 'candidates': candidates, 'economics': economics, 'classifier': None}
            if exhaustion_proof is not None:
                _require(stage in PAIRS or binding['mode'] != 'cloudyfan' and stage == 'after_validation',
                         'original_context_unavailable')
                value['source']['availability_exhaustion_input_sha256'] = exhaustion_proof
            classifier_failure = None
            if checkpoint['economic']['action'] == 'classify':
                try:
                    value['classifier'] = _classifier(source, job['registered'], manifest, result, checkpoint,
                        stage, previous, template, economics['expected_contract_sha256'])
                except ERRORS as error:
                    if source.limited:
                        raise
                    # Missing/bad original classifier evidence leaves the known
                    # classify gate partial; it must not unlock a later decision.
                    value['classifier'] = None
                    classifier_failure = ('classifier_binding_mismatch' if isinstance(error, SourceUnavailable)
                        and error.args == ('classifier_binding_mismatch',) else 'classifier_evidence_unavailable')
            values.append(value)
            reproduced = source.reproduce(value)
            if classifier_failure and reproduced['status'] != 'reproduced':
                reproduced = {**reproduced, 'reason': classifier_failure}
            if empty_same_pool:
                _require(checkpoint['economic'] == {'action': 'stop', 'reason': 'no_eligible_candidate'},
                         'original_saved_decision_mismatch')
                if reproduced['status'] == 'reproduced' and reproduced['action'] == 'advance_quality_stage':
                    # This is the original non-generating continue branch, not
                    # a selected candidate or an entry in selection.decisions.
                    next_tiers = PAIRS['low_mid' if phase == 0 else 'mid_high'] if binding['mode'] == 'cloudyfan' else ('high',)
                    match = actual is None or actual['tier'] in next_tiers
                    decisions.append(_point(len(decisions), stage, reproduced, candidates, match))
                    if match:
                        _require(_hash(reproduced['input_sha256']), 'original_context_unavailable')
                        exhaustion_proof = reproduced['input_sha256']
                        continue
                elif actual is None and result['termination_reason'] == 'no_eligible_candidate':
                    _require(selected_count == len(result['selection']['decisions']), 'original_saved_decision_mismatch')
                    match = reproduced['status'] == 'reproduced' and reproduced['action'] == 'stop' and (
                        reproduced['reason'] == 'no_eligible_candidate')
                    decisions.append(_point(len(decisions), stage, reproduced, candidates,
                        match if reproduced['status'] == 'reproduced' else None))
                    if match:
                        return values, decisions
                else:
                    decisions.append(_point(len(decisions), stage, reproduced, candidates, None))
                _unknown_tail(decisions, path, None, 'original_context_unavailable', extra_points)
                return values, decisions
            if actual is None:
                # An original terminal empty quality checkpoint is also absent
                # from selection.decisions; its gates still need verification.
                _require(checkpoint['economic'] == {'action': 'stop', 'reason': 'no_eligible_candidate'}
                    and selected_count == len(result['selection']['decisions']), 'original_saved_decision_mismatch')
                match = reproduced['status'] == 'reproduced' and reproduced['action'] == 'stop' and (
                    reproduced['reason'] == result['termination_reason'])
                decisions.append(_point(len(decisions), stage, reproduced, candidates,
                    match if reproduced['status'] == 'reproduced' else None))
                return values, decisions
            _require(result['selection']['decisions'][selected_count] == checkpoint['economic'],
                     'original_saved_decision_mismatch')
            selected_count += 1
            if reproduced['policy_reason'] is not None:
                _require(reproduced['policy_reason'] == checkpoint['economic']['reason'], 'original_saved_decision_mismatch')
            if reproduced['status'] in ('reproduced', 'partial'):
                _require(reproduced['action'] == checkpoint['economic']['action'], 'original_saved_decision_mismatch')
            if checkpoint['economic']['action'] == 'classify' and reproduced['status'] == 'reproduced':
                _classifier_saved_agrees(result['selection']['decisions'][selected_count],
                    value['classifier'], reproduced, stage)
                selected_count += 1
            match = None
            if reproduced['status'] == 'reproduced' and _generation_verified(receipts[attempt_index]):
                match = reproduced['selected_tier'] == actual['tier'] and (
                    reproduced['selected_configuration_sha256'] == actual['configuration_sha256'])
            decisions.append(_point(len(decisions), stage, reproduced, candidates, match))
            if match is not True:
                _unknown_tail(decisions, path, None, 'original_context_unavailable', extra_points)
                return values, decisions
            used.add(actual['configuration_sha256'])
            if attempts[attempt_index]['stop'] in AVAILABILITY:
                failed.add(attempts[attempt_index]['profile']['provider'])
            if stage in PAIRS:
                phase += 1
            attempt_index += 1
            exhaustion_proof = None
        except ERRORS as error:
            reason = error.args[0] if isinstance(error, SourceUnavailable) and error.args else ''
            reason = reason if isinstance(reason, str) and re.fullmatch(r'[a-z0-9_]{1,80}', reason) else 'original_decision_inputs_unavailable'
            _unknown_tail(decisions, path, stage, reason, extra_points)
            return values, decisions
    # This block is reachable only after every earlier choice matched. No later
    # actual answer is used to bridge a missing receipt, gate, or policy choice.
    try:
        _require(selected_count == len(result['selection']['decisions']), 'original_saved_decision_mismatch')
        context = _context(path, attempts, receipts, len(path), 'after_validation', phase)
        stopped = deepcopy(values[-1])
        stopped.update(context=context, candidates=[], economics=None, classifier=None)
        stopped['source']['candidate_set_sha256'] = None
        stopped['source'].pop('availability_exhaustion_input_sha256', None)
        values.append(stopped)
        reproduced = source.reproduce(stopped)
        expected = 'public_validation_passed' if result['termination_reason'] == 'public_passed' else result['termination_reason']
        match = reproduced['status'] == 'reproduced' and reproduced['action'] == 'stop' and reproduced['reason'] == expected
        decisions.append(_point(len(decisions), 'after_validation', reproduced, [], match if reproduced['status'] == 'reproduced' else None))
    except ERRORS as error:
        reason = error.args[0] if isinstance(error, SourceUnavailable) and error.args else ''
        reason = reason if isinstance(reason, str) and re.fullmatch(r'[a-z0-9_]{1,80}', reason) else 'original_context_receipts_unavailable'
        _unknown_tail(decisions, path, 'after_validation', reason, extra_points)
    return values, decisions
