"""Bounded host loading of original frozen sequential/QCS decision inputs.

This replays the admitted singleton per tier, not a missing source snapshot's
whole-pool qualification. Original prose stays here while request/context hashes
are verified; only the Core value DTO crosses into the pure comparator.
The first unavailable or divergent choice terminates reproduction, so no later
actual branch answer can stand in for an unobserved reproduced branch.
A cfg-<digest> identifies fixed executable content when no original config ID
exists. It never claims quality evidence, qualification or new admission.
"""
from datetime import datetime, timezone
from decimal import Decimal
import re
from urllib.parse import urlsplit

from cloudyfan_jev import routing
from cloudyfan_jev.client import DECISIONS, usage
from cloudyfan_jev.decisions import choice_probability_decimals
from cloudyfan_jev.saved_policy_comparison import candidate_set_digest

from .saved_comparisons import LIMITS, TIERS, SourceUnavailable, digest, identifier, original_digest


VERSION = 'hierarchical-qcs-v1'
HORIZON = 'remaining_after_previous_validation'
COMPLETE_PATH = {'generation', 'failure', 'retry', 'repair', 'escalation',
                 'later_classification', 'cache', 'reasoning'}
HASH = re.compile(r'[a-f0-9]{64}')


def _decimal(value, *, probability=False):
    if (type(value) is not str or len(value) > 128
            or re.fullmatch(r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)', value) is None):
        raise SourceUnavailable('original_economics_unavailable')
    number = Decimal(value)
    if number.adjusted() > 64 or number.as_tuple().exponent < -64 or probability and number > 1:
        raise SourceUnavailable('original_economics_unavailable')
    return value


def _interval(value, *, probability=False):
    if type(value) is not dict or set(value) != {'lower', 'upper'}:
        raise SourceUnavailable('original_economics_unavailable')
    result = {key: _decimal(value[key], probability=probability) for key in ('lower', 'upper')}
    if Decimal(result['lower']) > Decimal(result['upper']):
        raise SourceUnavailable('original_economics_unavailable')
    return result


def _paths(value, stage, *, conditional=False):
    if type(value) is not dict or len(value) > 2 or set(value) - set(routing.STAGES[stage]):
        raise SourceUnavailable('original_economics_unavailable')
    paths = {}
    for tier, row in value.items():
        fields = {'cost_usd'} if conditional and type(row) is dict and set(row) == {'cost_usd'} else {
            'cost_usd', 'flow_ms_upper'}
        if type(row) is not dict or set(row) != fields:
            raise SourceUnavailable('original_economics_unavailable')
        paths[tier] = {'cost_usd': _interval(row['cost_usd'])}
        if 'flow_ms_upper' in row:
            paths[tier]['flow_ms_upper'] = (None if conditional and row['flow_ms_upper'] is None
                                           else _decimal(row['flow_ms_upper']))
    return paths


def attempt_identity(manifest, attempt):
    tier = attempt['tier']
    profile = manifest['profiles'][tier]
    card = profile.get('capability_card', {})
    selection = card.get('score_selection', {}) if type(card) is dict else {}
    config_id = selection.get('config_id') if type(selection) is dict else None
    # Match the fixed scores/configuration.py identity contract without loading
    # scores/__init__ and its optional source/network adapters in the worker.
    identity = {key: profile.get(key) for key in ('model', 'model_version', 'accepted_models',
        'effort', 'provider', 'provider_name', 'endpoint', 'quantization', 'engine', 'weights_digest')}
    if 'reasoning_parameter' in profile:
        from cloudyfan_jev.generation_request import reasoning_fields
        reasoning_fields(profile)
        identity['reasoning_parameter'] = profile['reasoning_parameter']
    configuration_sha = digest(identity)
    plan = manifest.get('selection_policy')
    if config_id is None and plan is None and manifest['routing_version'] == routing.VERSION:
        config_id = 'cfg-' + configuration_sha
    if identifier(config_id) is None:
        raise SourceUnavailable('original_configuration_identity_unavailable')
    if plan is not None and (plan['profiles'][tier] != profile
            or card['qcs']['configuration_sha256'] != configuration_sha
            or plan['records'][tier]['configuration_sha256'] != configuration_sha):
        raise SourceUnavailable('original_configuration_identity_invalid')
    return {'config_id': config_id, 'configuration_sha256': configuration_sha}


def _provenance(value, policy, created):
    if (type(value) is not dict or value.get('basis') != 'independent_calibration'
            or value.get('workload_id') != policy['workload_id']
            or HASH.fullmatch(str(value.get('raw_sha256'))) is None):
        raise SourceUnavailable('original_economics_unavailable')
    url = urlsplit(value.get('source_url', ''))
    start, end = (datetime.fromisoformat(value[key]) for key in ('retrieved_at', 'valid_until'))
    if (url.scheme != 'https' or not url.netloc or start.tzinfo is None or end.tzinfo is None
            or not start <= datetime.fromtimestamp(created, timezone.utc) < end):
        raise SourceUnavailable('original_economics_unavailable')


def _plan(manifest):
    plan = manifest['selection_policy']
    if (manifest['routing_version'] != VERSION or plan['schema_version'] != VERSION
            or plan['plan_id'] != digest({key: value for key, value in plan.items() if key != 'plan_id'})
            or plan['profiles'] != manifest['profiles'] or set(plan['eligible']) != set(TIERS)
            or any(type(value) is not bool for value in plan['eligible'].values())
            or len(plan['evidence']) > LIMITS['max_candidate_configurations']
            or len(plan['excluded']) > LIMITS['max_candidate_configurations']
            or plan['policy']['schema_version'] != 'qcs-policy-v1'):
        raise SourceUnavailable('original_frozen_plan_invalid')
    category = manifest['task'].get('category_binding')
    if category is not None and (category.get('schema_version') != 'category-qcs-v1'
            or category.get('snapshot_id') != plan['snapshot_id']
            or category.get('category_id') != plan['policy'].get('category_id')
            or manifest['task'].get('category') != category['category_id']):
        raise SourceUnavailable('original_frozen_plan_invalid')
    return plan


def _candidates(plan, manifest, tiers):
    candidates = []
    for tier in tiers:
        if not plan['eligible'][tier]:
            if tier in plan['profiles'] or tier in plan['records']:
                raise SourceUnavailable('original_frozen_plan_invalid')
            continue
        profile, record = plan['profiles'][tier], plan['records'][tier]
        identity = attempt_identity(manifest, {'tier': tier})
        card = profile['capability_card']['qcs']
        selection = profile['capability_card']['score_selection']
        if (record != plan['evidence'][identity['config_id']] or card['evidence_sha256'] != digest(record)
                or record['quality_sufficient'] is not True or card['quality_sufficient'] is not True
                or card['workload_id'] != plan['policy']['workload_id']
                or card['generation_cost_usd'] != record['generation_cost_usd']
                or card['complete_cost_usd'] != record['complete_cost_usd'] or card['speed'] != record['speed']
                or selection['snapshot_id'] != plan['snapshot_id']
                or selection['requirements'] != plan['requirements']):
            raise SourceUnavailable('original_candidate_evidence_invalid')
        score = profile.get('external_score')
        if score is not None:
            if type(score) not in (str, int, float) or len(str(score)) > 128:
                raise SourceUnavailable('original_candidate_evidence_invalid')
            try:
                score = Decimal(str(score))
            except ArithmeticError:
                raise SourceUnavailable('original_candidate_evidence_invalid') from None
            if not score.is_finite() or score.adjusted() > 64 or score.as_tuple().exponent < -64:
                raise SourceUnavailable('original_candidate_evidence_invalid')
            score = format(score, 'f')
        candidates.append({'tier': tier, **identity,
            'generation_cost_usd': _interval(record['generation_cost_usd']),
            'complete_cost_usd': _interval(record['complete_cost_usd']),
            'flow_ms_upper': _decimal(record['speed']['upper']), 'quality_score': score,
            'gate': {'state': 'eligible', 'reasons': [], 'evidence_sha256': digest({
                'plan_id': plan['plan_id'], 'tier': tier, 'eligible': True,
                'record_sha256': digest(record), 'requirements_sha256': digest(plan['requirements'])})}})
    return candidates


def _fixed_candidates(manifest, tiers):
    # Plain Core's original profile is fixed execution configuration, not a QCS
    # candidate qualification. Unknown forecasts stay absent, not estimated.
    return [{'tier': tier, **attempt_identity(manifest, {'tier': tier}),
        'generation_cost_usd': None, 'complete_cost_usd': None, 'flow_ms_upper': None, 'quality_score': None,
        'gate': {'state': 'eligible', 'reasons': [], 'evidence_sha256': digest({
            'tier': tier, 'routing_version': manifest['routing_version'],
            'fixed_profile_sha256': digest(manifest['profiles'][tier])})}} for tier in tiers]


def _contract(profiles, version, stage, previous_tier, *, policy_version=VERSION):
    state = {'task': '<public-workload-task>', 'previous_answer': '<latest-public-answer>',
        'previous_tier': previous_tier, 'tool_results': [],
        'previous_configuration': {key: profiles[previous_tier][key]
            for key in (*routing.PROFILE_FIELDS, *routing.OPTIONAL_IDENTITY_FIELDS) if key in profiles[previous_tier]},
        'public_diagnostics': '<latest-public-diagnostics>'}
    _, _, payload = routing.build_request(stage, state, profiles, version, version, policy_version=policy_version)
    return digest({'payload': payload, 'effective_fallback': 'upper_on_invalid_abstain_or_tie',
                   'unknown_charge': 'stop', 'horizon': HORIZON})


def _economics(plan, manifest, binding, stage, previous_tier):
    policy = plan['policy']
    calibration = policy['calibration']
    _provenance(calibration, policy, binding['created'])
    excluded = {'profiles', 'decision', 'selection_policy', 'engine_version', 'routing_version',
                'generation_attempt_limits', 'task', 'mode', 'contract_version'}
    execution = {'core': {key: value for key, value in manifest.items() if key not in excluded},
                 'validation_contract': manifest['contract_version']}
    flow = policy['complete_policy_flow']
    flow_hash = digest({'snapshot_id': plan['snapshot_id'], 'requirements': plan['requirements'],
        'evidence': plan['evidence'], 'policy': {key: value for key, value in policy.items() if key != 'complete_policy_flow'}})
    _provenance(flow['provenance'], policy, binding['created'])
    if (set(calibration['includes']) != COMPLETE_PATH or calibration['horizon'] != HORIZON
            or policy['execution_contract'] != execution or flow['contract_sha256'] != flow_hash
            or flow['metric'] != 'whole_task_flow_ms' or flow['statistic'] != policy['speed_statistic']
            or flow['provenance']['raw_sha256'] != calibration['raw_sha256']
            or Decimal(_decimal(flow['upper'])) > Decimal(_decimal(policy['max_flow_ms']))
            or any(calibration['configurations'].get(tier) != attempt_identity(manifest, {'tier': tier})['configuration_sha256']
                   for tier in plan['profiles'])):
        raise SourceUnavailable('original_economics_unavailable')
    key = stage if stage == 'low_mid' else stage + ':' + previous_tier
    forecast = policy['stages'][key]
    conditional = forecast.get('classified_calibration')
    contract = expected = None
    if conditional is not None:
        _provenance(conditional, policy, binding['created'])
        expected = _contract(plan['profiles'], manifest['decision']['version'], stage, previous_tier)
        contract = conditional.get('contract_sha256')
        if (conditional['raw_sha256'] != calibration['raw_sha256']
                or conditional.get('stage') != stage or conditional.get('previous_tier') != previous_tier
                or conditional.get('context') != 'public_failure_or_unknown' or conditional.get('horizon') != HORIZON
                or set(conditional.get('includes', [])) != COMPLETE_PATH
                or conditional.get('effective_probability_includes_fallback') is not True
                or conditional.get('classifier_version') != policy.get('classifier_version')
                or conditional.get('classifier_version') != manifest['decision']['version'] or contract != expected):
            raise SourceUnavailable('original_economics_unavailable')
    return {'calibration': 'present', 'contract_sha256': contract, 'expected_contract_sha256': expected,
        'direct_tier': forecast['direct_tier'] if forecast['direct_tier'] in routing.STAGES[stage] else None,
        'paths': _paths(forecast['paths'], stage),
        'classified_paths': _paths(forecast['classified_paths'], stage, conditional=True) if conditional is not None else None,
        'classifier_cost_usd': _interval(forecast['classifier_cost_usd']) if forecast.get('classifier_cost_usd') is not None else None,
        'lower_probability': _interval(forecast['lower_probability'], probability=True) if forecast.get('lower_probability') is not None else None,
        'classify_flow_ms_upper': _decimal(forecast['classify_flow_ms_upper']) if forecast.get('classify_flow_ms_upper') is not None else None,
        'max_flow_ms': _decimal(policy['max_flow_ms']), 'minimum_saving_usd': _decimal(policy['minimum_saving_usd']),
        'minimum_saving_fraction': _decimal(policy['minimum_saving_fraction'], probability=True)}


def _values(binding, job, manifest, checkpoint, candidates, context, economics=None):
    return {'schema_version': 'saved-policy-values-v1', 'policy_version': manifest['routing_version'], 'mode': binding['mode'],
        'source': {'binding_sha256': digest(binding), 'request_sha256': original_digest(job['request']),
            'registered_sha256': original_digest(job['registered']), 'policy_sha256': original_digest(manifest),
            'checkpoint_sha256': digest(checkpoint), 'candidate_set_sha256': candidate_set_digest(candidates)},
        'context': context, 'candidates': candidates, 'economics': economics, 'classifier': None}


def _context(index, result, original_path, receipts, stage):
    attempt, receipt = result['attempts'][index], receipts[index]
    passed = attempt['public']['passed']
    stop = ('validation_passed' if passed is True else 'validation_failed' if passed is False else 'validation_unknown')
    if attempt['stop'] in ('provider_rate_limited', 'provider_unavailable', 'unknown_charge'):
        stop = attempt['stop']
    if receipt['calls'] and any(call['cost_known'] is not True for call in receipt['calls']):
        stop = 'unknown_charge'
    return {'stage': stage, 'previous_tier': attempt['tier'],
        'previous_configuration_sha256': original_path[index]['configuration_sha256'],
        'previous_public_passed': passed, 'previous_stop': stop, 'quality_phase': index,
        'generation_calls': sum(len(row['calls']) for row in result['attempts'][:index + 1]),
        'receipt_sha256': receipt['receipts_sha256'] if receipt['status'] == 'verified' else None,
        'validation_sha256': receipt['validation_sha256'] if receipt['status'] == 'verified' else None}


def _classifier(source, manifest, result, run, stage, index):
    cid = 'run-' + result['run_id'] + '-decision-' + stage
    calls = result['selection']['calls']
    matches = [row for row in calls if row.get('call_id') == cid]
    if len(calls) > 2 or len(matches) != 1 or identifier(cid) is None:
        raise SourceUnavailable('classifier_evidence_unavailable')
    call = matches[0]
    root = run.parent.parent / 'calls' / cid
    intent = source.read(root / 'intent.json', 'original_classifier_intent')
    intent_sha = source.evidence[-1]['sha256']
    response = source.read(root / 'response.json', 'original_classifier_response')
    response_sha = source.evidence[-1]['sha256']
    previous = result['attempts'][index]
    count, actions = previous['cumulative_actions'], result['actions']
    if type(count) is not int or type(actions) is not list or not 0 <= count <= len(actions) <= 128:
        raise SourceUnavailable('classifier_binding_mismatch')
    profiles, decision = manifest['profiles'], manifest['decision']
    state = {'task': manifest['task']['prompt'], 'previous_answer': previous['final'],
        'previous_tier': previous['tier'],
        'previous_configuration': {key: profiles[previous['tier']][key]
            for key in (*routing.PROFILE_FIELDS, *routing.OPTIONAL_IDENTITY_FIELDS) if key in profiles[previous['tier']]},
        'tool_results': [{key: action[key] for key in ('name', 'arguments', 'result')} for action in actions[:count]],
        'public_diagnostics': previous['public']}
    # Ordinary Core omitted the optional step.policy_version wire field. Its
    # default context version is still routing.VERSION; preserve both details.
    classifier_policy = manifest['routing_version'] if manifest.get('selection_policy') is not None else None
    context, questions, payload = routing.build_request(stage, state, profiles,
        decision['version'], decision['version'], policy_version=classifier_policy)
    request_sha = original_digest({'url': DECISIONS, 'payload': payload})
    actual = usage(response)
    if (intent.get('call_id') != cid or response.get('call_id') != cid or intent.get('url') != DECISIONS
            or type(response.get('http_status')) is not int or response['http_status'] != 200
            or intent.get('payload') != payload or intent.get('request_sha256') != request_sha
            or response.get('request_sha256') != request_sha
            or any(call.get(key) != actual.get(key) for key in ('model', 'provider', 'generation_id', 'cost_unknown', 'cost_usd'))
            or actual['model'] not in (decision['model'], decision['version'])
            or actual['provider'] != decision['provider_name']):
        raise SourceUnavailable('classifier_binding_mismatch')
    parsed = routing.parse_choice(response['raw'], context, questions,
        decision['version'], (decision['version'],))
    if parsed != result['selection']['decisions'][index]:
        raise SourceUnavailable('classifier_binding_mismatch')
    contract = _contract(profiles, decision['version'], stage, previous['tier'],
                         policy_version=classifier_policy)
    valid = parsed['reported_choice'] is not None
    return {'stage': stage, 'contract_sha256': contract, 'expected_contract_sha256': contract,
        'request_sha256': intent['request_sha256'], 'expected_request_sha256': request_sha,
        'response_sha256': response_sha, 'receipt_sha256': digest([intent_sha, response_sha]),
        'context_sha256': digest(intent['payload']['state']), 'expected_context_sha256': digest(payload['state']),
        'cost_known': actual['cost_unknown'] is False, 'parse_status': 'valid' if valid else 'invalid',
        'reported_choice': parsed['reported_choice'] if valid else None,
        'probabilities': {tier: _decimal(format(Decimal(value), 'f'), probability=True)
            for tier, value in parsed['probabilities'].items()} if valid and parsed['probabilities'] is not None else None,
        'abstain_reason': parsed['fallback_reason'] if valid and parsed['fallback_reason'] in (
            'distribution_missing', 'explicit_abstention_choice') else None,
        'probability_decimals': choice_probability_decimals(model=decision['version'],
            response_model=actual['model'], provider=actual['provider'])}


def _public(index, stage, reproduced, candidates, actual):
    selected = reproduced['selected_configuration_sha256']
    match = None
    if reproduced['status'] == 'reproduced':
        match = (reproduced['action'] == 'stop' if actual is None else (
            reproduced['selected_tier'] == actual['tier'] and selected == actual['configuration_sha256']))
    return {'index': index, 'stage': stage, 'action': reproduced['action'],
        'selected_tier': reproduced['selected_tier'],
        'config_id': next((row['config_id'] for row in candidates if row['configuration_sha256'] == selected), None),
        'reason': reproduced['reason'], 'policy_reason': reproduced['policy_reason'],
        'net_saving_lower_usd': reproduced['net_saving_lower_usd'], 'match': match}


def _unknown(index, stage, reason):
    return {'index': index, 'stage': stage, 'action': None, 'selected_tier': None,
        'config_id': None, 'reason': reason, 'policy_reason': None, 'net_saving_lower_usd': None, 'match': None}


def _same_economic_outcome(saved, expected):
    if (type(saved) is not dict or set(saved) != set(expected)
            or any(saved[key] != value for key, value in expected.items() if key != 'net_saving_lower_usd')):
        return False
    left, right = saved['net_saving_lower_usd'], expected['net_saving_lower_usd']
    if left is None or right is None:
        return left is None and right is None
    numbers = []
    for value in (left, right):
        if (type(value) is not str or len(value) > 256
                or re.fullmatch(r'-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?', value) is None):
            return False
        number = Decimal(value)
        # Original cost/probability inputs have at most 64 decimal places;
        # their product can have 128. The finite difference can reach 2e65.
        if not number.is_finite() or number.adjusted() > 65 or number.as_tuple().exponent < -128:
            return False
        numbers.append(number)
    return numbers[0] == numbers[1]


def reproduce_saved_policy(source, binding, job, manifest, result, run, trusted, *, original_path, receipts):
    inputs, decisions = [], []
    policy = manifest['routing_version']
    if policy not in (VERSION, routing.VERSION):
        raise SourceUnavailable('original_frozen_plan_invalid')
    plan = _plan(manifest) if policy == VERSION else None
    mode = binding['mode']
    if (plan is not None and mode != 'cloudyfan' or mode not in (
            'cloudyfan', 'fixed_high', 'low_first', 'open_source_first') or not original_path
            or len(original_path) > (3 if mode == 'cloudyfan' else 2)):
        raise SourceUnavailable('original_frozen_plan_invalid')
    tier = {'cloudyfan': 'open_source', 'fixed_high': 'high', 'low_first': 'low',
            'open_source_first': 'open_source'}[mode]
    candidates = _candidates(plan, manifest, (tier,)) if plan is not None else _fixed_candidates(manifest, (tier,))
    checkpoint = {'stage': 'initial', 'frozen_profiles_sha256': digest(manifest['profiles'])}
    if plan is not None:
        checkpoint['plan_id'] = plan['plan_id']
    initial = _values(binding, job, manifest, checkpoint, candidates,
        {'stage': 'initial', 'previous_tier': None, 'previous_configuration_sha256': None,
         'previous_public_passed': None, 'previous_stop': 'none', 'quality_phase': 0,
         'generation_calls': 0, 'receipt_sha256': None, 'validation_sha256': None})
    inputs.append(initial)
    decisions.append(_public(0, 'initial', source.reproduce(initial), candidates, original_path[0]))
    if decisions[-1]['match'] is not True:
        return inputs, decisions
    for index, previous in enumerate(result['attempts']):
        following = index + 1 < len(original_path)
        stage = ('low_mid' if index == 0 else 'mid_high') if following and mode == 'cloudyfan' else 'after_validation'
        try:
            context = _context(index, result, original_path, receipts, stage)
            if stage == 'after_validation':
                candidates = _fixed_candidates(manifest, ('high',)) if following else []
                values = _values(binding, job, manifest, {'stage': stage, 'attempt_index': index}, candidates, context)
                reproduced = source.reproduce(values)
                inputs.append(values)
                decisions.append(_public(len(decisions), stage, reproduced, candidates,
                    original_path[index + 1] if following else None))
                if decisions[-1]['match'] is not True:
                    break
                continue
            if plan is not None:
                checkpoint = source.read(run / ('economics-' + stage + '.json'), 'original_economic_checkpoint')
                if (checkpoint.get('previous_tier') != previous['tier'] or checkpoint.get('previous_answer') != previous['final']
                        or checkpoint.get('public_diagnostics') != previous['public']):
                    raise SourceUnavailable('original_context_unavailable')
                candidates = _candidates(plan, manifest, routing.STAGES[stage])
                economic = _economics(plan, manifest, binding, stage, previous['tier'])
            else:
                checkpoint = {'stage': stage, 'frozen_profiles_sha256': digest(manifest['profiles'])}
                candidates = _fixed_candidates(manifest, routing.STAGES[stage])
                economic = None
            values = _values(binding, job, manifest, checkpoint, candidates, context, economic)
            reproduced = source.reproduce(values)
            if reproduced['status'] not in ('reproduced', 'partial'):
                inputs.append(values)
                decisions.append(_public(len(decisions), stage, reproduced, candidates, original_path[index + 1]))
                break
            if plan is not None:
                expected = {'stage': stage, 'previous_tier': previous['tier'], 'action': reproduced['action'],
                    'selected_tier': reproduced['selected_tier'] if reproduced['action'] == 'direct' else None,
                    'reason': reproduced['policy_reason'], 'net_saving_lower_usd': reproduced['net_saving_lower_usd'],
                    'plan_id': plan['plan_id']}
                if not _same_economic_outcome(checkpoint.get('outcome'), expected):
                    raise SourceUnavailable('original_economic_checkpoint_invalid')
            if reproduced['action'] == 'classify':
                try:
                    values['classifier'] = _classifier(source, manifest, result, run, stage, index)
                    reproduced = source.reproduce(values)
                except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, SourceUnavailable) as error:
                    if isinstance(error, SourceUnavailable) and error.args in (
                            ('comparison_compute_limit',), ('source_work_limit',)):
                        raise
                    reproduced['reason'] = ('classifier_binding_mismatch' if isinstance(error, SourceUnavailable)
                        and error.args == ('classifier_binding_mismatch',) else 'classifier_evidence_unavailable')
            elif plan is not None and not _same_economic_outcome(result['selection']['decisions'][index], expected):
                raise SourceUnavailable('original_economic_checkpoint_invalid')
            inputs.append(values)
            decisions.append(_public(len(decisions), stage, reproduced, candidates, original_path[index + 1]))
            if decisions[-1]['match'] is not True:
                break
        except (OSError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, SourceUnavailable) as error:
            if isinstance(error, SourceUnavailable) and error.args in (
                    ('comparison_compute_limit',), ('source_work_limit',)):
                raise
            reason = (error.args[0] if isinstance(error, SourceUnavailable) and len(error.args) == 1
                and error.args[0] in {'original_context_unavailable', 'original_economics_unavailable',
                    'original_economic_checkpoint_invalid', 'original_candidate_evidence_invalid',
                    'original_frozen_plan_invalid'} else 'original_decision_inputs_unavailable')
            decisions.append(_unknown(len(decisions), stage, reason))
            break
    return inputs, decisions
