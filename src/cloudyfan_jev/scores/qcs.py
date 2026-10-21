"""Versioned task-specific quality/cost/speed admission and classifier economics.

Independent calibration is trusted operator evidence, not a supplier guarantee.
Complete continuation forecasts must include later failures/repairs/decisions.
No benchmark score or Decisions distribution is used as a success probability.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
import re
from urllib.parse import urlsplit

from .catalog import digest, timestamp, verify_snapshot, _positive
from .configuration import configuration_digest
from ..routing import STAGES, _reject_private, build_request, PROFILE_FIELDS, OPTIONAL_IDENTITY_FIELDS

VERSION = 'hierarchical-qcs-v1'
COMPLETE_PATH = {'generation', 'failure', 'retry', 'repair', 'escalation',
                 'later_classification', 'cache', 'reasoning'}
HORIZON = 'remaining_after_previous_validation'


def flow_contract_hash(snapshot, requirements, evidence, policy):
    """A whole-task forecast is specific to this complete frozen policy/input."""
    return digest({'snapshot_id': snapshot['snapshot_id'], 'requirements': requirements,
                   'evidence': evidence, 'policy': {k: v for k, v in policy.items() if k != 'complete_policy_flow'}})


def classifier_contract_hash(profiles, version, stage, previous_tier):
    """Bind calibration to the real binary payload contract and candidate cards.

    Variable task/answer/diagnostics are represented by public placeholders; the
    workload/failure population is separately bound by calibration provenance.
    """
    state = {'task': '<public-workload-task>', 'previous_answer': '<latest-public-answer>',
             'previous_tier': previous_tier, 'tool_results': [],
             'previous_configuration': {k: profiles[previous_tier][k]
                 for k in (*PROFILE_FIELDS, *OPTIONAL_IDENTITY_FIELDS) if k in profiles[previous_tier]},
             'public_diagnostics': '<latest-public-diagnostics>'}
    _, _, payload = build_request(stage, state, profiles, version, version, policy_version=VERSION)
    return digest({'payload': payload, 'effective_fallback': 'upper_on_invalid_abstain_or_tie',
                   'unknown_charge': 'stop', 'horizon': HORIZON})


def _d(value):
    if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(r'(?:\d+(?:\.\d+)?|\.\d+)', value):
        raise ValueError('Explicit nonnegative plain decimal string required')
    result = Decimal(value)
    if result.adjusted() > 64 or result.as_tuple().exponent < -64:
        raise ValueError('Estimate precision out of bounds')
    return result


def _interval(value):
    if not isinstance(value, dict) or set(value) != {'lower', 'upper'}:
        raise ValueError('Explicit estimate interval required')
    low, high = _d(value['lower']), _d(value['upper'])
    if low > high:
        raise ValueError('Reversed estimate interval')
    return low, high


def _provenance(value, workload, now):
    if (not isinstance(value, dict) or value.get('basis') != 'independent_calibration' or value.get('workload_id') != workload
            or urlsplit(value.get('source_url', '')).scheme != 'https'
            or not urlsplit(value['source_url']).netloc
            or not re.fullmatch('[a-f0-9]{64}', value.get('raw_sha256', ''))):
        raise ValueError('Applicable independent calibration provenance required')
    start, end = timestamp(value['retrieved_at']), timestamp(value['valid_until'])
    if not start < end or (now is not None and not start <= timestamp(now) < end):
        raise ValueError('Calibration stale or from the future')


def _candidate(entry, evidence, policy, now):
    if evidence['configuration_sha256'] != configuration_digest(entry):
        raise ValueError('Candidate identity mismatch')
    _provenance(evidence['provenance'], policy['workload_id'], now)
    if policy.get('category_id') is not None:
        from .categories import category_matches
        if evidence['provenance'].get('category_id') != policy['category_id']:
            raise ValueError('Independent category quality missing')
        if any(row.get('quality') and not category_matches(row, policy['category_id'])
               for row in evidence.get('source_metrics', []) if isinstance(row, dict)):
            raise ValueError('Source quality category mismatch')
    if evidence['quality_sufficient'] is not True:
        raise ValueError('Task quality sufficiency unproven')
    _interval(evidence['generation_cost_usd'])
    if _interval(evidence['complete_cost_usd'])[1] < _interval(evidence['generation_cost_usd'])[1]:
        raise ValueError('Complete cost omits generation')
    speed = evidence['speed']
    if (speed['metric'] != 'flow_ms' or speed['statistic'] != policy['speed_statistic']
            or _d(speed['upper']) > _d(policy['max_flow_ms'])):
        raise ValueError('Applicable flow speed requirement not met')
    for row in evidence['source_metrics']:
        if not isinstance(row, dict) or row.get('schema_version') != 'qcs-source-v1':
            raise ValueError('Normalized source metrics required')
        identities = {value for value in (entry.get('source_model_id'), entry['profile']['model'], entry['profile']['model_version'])
                      if isinstance(value, str) and value}
        if row['model_id'] not in identities:
            raise ValueError('Source model association mismatch')
        if row.get('effort') not in (None, entry['profile']['effort']):
            raise ValueError('Source effort mismatch')
        if row.get('provider') not in (None, entry['profile']['provider']):
            raise ValueError('Source provider mismatch')
        if row.get('service_tier') not in (None, 'default', 'standard'):
            raise ValueError('Source tier mismatch')
        if entry['profile'].get('quantization') and row.get('provider') is not None and row.get('quantization') != entry['profile']['quantization']:
            raise ValueError('Source quantization mismatch')
        if not re.fullmatch('[a-f0-9]{64}', row.get('raw_sha256', '')):
            raise ValueError('Missing source hash')
        if now is not None:
            age = (timestamp(now) - timestamp(row['retrieved_at'])).total_seconds()
            if not 0 <= age <= _d(policy.get('max_source_age_hours', '24')) * 3600:
                raise ValueError('Source metrics stale')
            if row.get('as_of') is not None:
                age = (timestamp(now) - timestamp(row['as_of'])).total_seconds()
                if not 0 <= age <= _d(policy.get('max_source_age_hours', '24')) * 3600:
                    raise ValueError('Source observation stale')


def prepare_qcs(snapshot, requirements, evidence, policy, *, now):
    """Prepare explicit candidates and a frozen policy; never dispatch or approve.

    Base snapshot qualification/capability constraints still apply. Bad optional
    candidates are excluded individually, not allowed to poison every pool.
    Independent workload estimates provide the cost/speed dimension when public
    benchmark/endpoint observations alone cannot describe the task's full path.
    """
    verify_snapshot(snapshot, now=now)
    _reject_private(evidence)
    _reject_private(policy)
    if policy.get('schema_version') != 'qcs-policy-v1' or policy.get('speed_statistic') not in ('mean', 'p50', 'p95', 'p99'):
        raise ValueError('Explicit QCS policy/statistic required')
    if not isinstance(policy.get('workload_id'), str) or not policy['workload_id'].strip():
        raise ValueError('Workload identity required')
    for key in ('max_flow_ms', 'minimum_saving_usd', 'minimum_saving_fraction'):
        _d(policy[key])
    if _d(policy['minimum_saving_fraction']) > 1:
        raise ValueError('Saving fraction out of range')
    execution = policy.get('execution_contract')
    if (not isinstance(execution, dict) or not isinstance(execution.get('core'), dict)
            or not isinstance(execution.get('validation_contract'), str) or not execution['validation_contract'].strip()):
        raise ValueError('Explicit calibrated execution contract required')
    _provenance(policy['calibration'], policy['workload_id'], now)
    if (set(policy['calibration']['includes']) != COMPLETE_PATH
            or policy['calibration'].get('horizon') != HORIZON):
        raise ValueError('Complete continuation costs required')
    for stage in policy['stages'].values():
        if stage.get('classified_calibration') is not None:
            _provenance(stage['classified_calibration'], policy['workload_id'], now)
            if not isinstance(policy.get('classifier_version'), str) or not policy['classifier_version'].strip():
                raise ValueError('Pinned calibrated classifier required')
    flow = policy.get('complete_policy_flow', {})
    if (flow.get('metric') != 'whole_task_flow_ms' or flow.get('statistic') != policy['speed_statistic']
            or _d(flow['upper']) > _d(policy['max_flow_ms'])
            or flow.get('contract_sha256') != flow_contract_hash(snapshot, requirements, evidence, policy)):
        raise ValueError('Applicable complete whole-task flow forecast required')
    _provenance(flow['provenance'], policy['workload_id'], now)
    if flow['provenance']['raw_sha256'] != policy['calibration']['raw_sha256']:
        raise ValueError('Whole-task flow calibration population mismatch')
    allowed = {'domain', 'capabilities', 'context_tokens', 'allowed_providers'}
    if set(requirements) - allowed or requirements['domain'] != snapshot['policy']['domain']:
        raise ValueError('Applicable public task requirements required')
    required = requirements['capabilities']
    providers = requirements.get('allowed_providers')
    if not isinstance(required, list) or any(not isinstance(x, str) or not x for x in required):
        raise ValueError('Required capabilities must be a list of strings')
    _positive(requirements['context_tokens'])
    if providers is not None and (not isinstance(providers, list) or not providers or
            any(not isinstance(x, str) or not x for x in providers)):
        raise ValueError('Explicit nonempty provider restriction required')
    selected, eligible, excluded, records = {}, {}, [], {}
    for tier in ('open_source', 'low', 'mid', 'high'):
        pool = snapshot.get('open_source_pool', [snapshot['open_source']]) if tier == 'open_source' else snapshot['pools'][tier]
        compatible = [e for e in pool if set(requirements['capabilities']) <= set(e['capabilities'])
            and requirements['context_tokens'] <= e['context_tokens']
            and (requirements.get('allowed_providers') is None or e['profile']['provider'] in requirements['allowed_providers'])]
        excluded.extend({'tier': tier, 'config_id': e['config_id'],
                         'reason': 'capability_context_or_provider_mismatch'} for e in pool if e not in compatible)
        good = []
        for entry in compatible:
            try:
                row = evidence[entry['config_id']]
                _candidate(entry, row, policy, now)
                if policy.get('category_id') and any(cap.startswith('sql') for cap in required):
                    from .catalog import _catalog_eligible
                    qualification = entry.get('sql_qualification', {})
                    if now is not None:
                        reason = _catalog_eligible(entry, {**snapshot['policy'], 'score_role': 'family_effort_prior'}, timestamp(now))
                        if reason:
                            raise ValueError(reason)
                    elif (not isinstance(qualification, dict) or qualification.get('configuration_sha256') != configuration_digest(entry)
                          or not isinstance(qualification.get('report_id'), str) or len(qualification['report_id']) != 64):
                        raise ValueError('SQL qualification missing')
                good.append(entry)
            except (ValueError, KeyError, TypeError) as error:
                excluded.append({'tier': tier, 'config_id': entry['config_id'],
                                 'reason': str(error) if isinstance(error, ValueError) else 'missing_or_invalid_dimensions'})
        eligible[tier] = bool(good)
        if good:
            entry = min(good, key=lambda e: (_interval(evidence[e['config_id']]['complete_cost_usd'])[1],
                _d(evidence[e['config_id']]['speed']['upper']), -e['profile'].get('external_score', 0)
                if e['profile'].get('external_score') is not None else 0, e['config_id']))
            selected[tier] = deepcopy(entry['profile'])
            records[tier] = deepcopy(evidence[entry['config_id']])
            selected[tier]['capability_card']['qcs'] = {
                'workload_id': policy['workload_id'], 'configuration_sha256': configuration_digest(entry),
                'quality_sufficient': True, 'generation_cost_usd': row_cost(evidence[entry['config_id']]),
                'complete_cost_usd': deepcopy(evidence[entry['config_id']]['complete_cost_usd']),
                'speed': deepcopy(evidence[entry['config_id']]['speed']),
                'evidence_sha256': digest(evidence[entry['config_id']]), 'estimates_are_billed_costs': False}
            selected[tier]['capability_card']['score_selection'] = {
                'snapshot_id': snapshot['snapshot_id'], 'policy_id': snapshot['policy']['id'],
                'config_id': entry['config_id'], 'requirements': deepcopy(requirements)}
        if good and policy['calibration']['configurations'].get(tier) != configuration_digest({'profile': selected[tier]}):
            raise ValueError('Continuation calibration identity mismatch')
    body = {'schema_version': VERSION, 'snapshot_id': snapshot['snapshot_id'],
            'requirements': deepcopy(requirements), 'evidence': deepcopy(evidence),
            'policy': deepcopy(policy), 'profiles': selected, 'eligible': eligible,
            'records': records, 'excluded': excluded}
    plan = {**body, 'plan_id': digest(body)}
    selector = SelectionPolicy(plan)
    selector.reservation_plan()  # fail before OSS if a required continuation is unsafe
    return plan


def row_cost(row):
    return deepcopy(row['generation_cost_usd'])


class SelectionPolicy:
    """Immutable, version-bound forecasts; no API/hidden outcome dependency."""
    def __init__(self, plan):
        if (plan.get('schema_version') != VERSION
                or plan.get('plan_id') != digest({k: v for k, v in plan.items() if k != 'plan_id'})):
            raise ValueError('QCS plan integrity mismatch')
        self._plan = deepcopy(plan)

    @property
    def plan(self):
        return deepcopy(self._plan)

    def descriptor(self):
        return deepcopy(self.plan)

    def decide(self, stage, previous_tier):
        with localcontext() as context:
            context.prec = 512
            return self._decide(stage, previous_tier)

    def _decide(self, stage, previous_tier):
        if stage not in STAGES or (stage == 'low_mid' and previous_tier != 'open_source') or (
                stage == 'mid_high' and previous_tier not in ('low', 'mid')):
            raise ValueError('QCS stage does not match previous generation')
        policy = self.plan['policy']
        key = stage if stage == 'low_mid' else stage + ':' + previous_tier
        stage_policy = policy['stages'][key]
        lower, upper = STAGES[stage]
        candidates = [t for t in (lower, upper) if self.plan['eligible'][t]]
        if not candidates:
            raise ValueError('No QCS-qualified continuation')
        preferred = stage_policy['direct_tier']
        if preferred not in (lower, upper):
            raise ValueError('Direct tier outside stage')
        paths = stage_policy['paths']
        for tier in candidates:
            low, high = _interval(paths[tier]['cost_usd'])
            if high < _interval(self.plan['records'][tier]['complete_cost_usd'])[1]:
                raise ValueError('Continuation omits complete cost')
            if _d(paths[tier]['flow_ms_upper']) > _d(policy['max_flow_ms']):
                raise ValueError('Continuation exceeds flow speed constraint')
        # The configured preference is only a tie-break, never an expensive
        # counterfactual chosen to manufacture classification savings.
        direct = min(candidates, key=lambda t: (_interval(paths[t]['cost_usd'])[1],
                     _d(paths[t]['flow_ms_upper']), t != preferred, t))
        result = {'stage': stage, 'previous_tier': previous_tier, 'action': 'direct',
                  'selected_tier': direct, 'reason': 'single_eligible_candidate',
                  'net_saving_lower_usd': None, 'plan_id': self.plan['plan_id']}
        if len(candidates) == 1:
            return result
        if stage_policy.get('classifier_cost_usd') is None or stage_policy.get('lower_probability') is None:
            return {**result, 'reason': 'classification_estimate_unknown'}
        _, classifier = _interval(stage_policy['classifier_cost_usd'])
        p0, p1 = _interval(stage_policy['lower_probability'])
        if p1 > 1:
            raise ValueError('Calibration probability out of range')
        costs = {t: _interval(paths[t]['cost_usd']) for t in candidates}
        conditional = stage_policy.get('classified_calibration')
        if conditional is not None:
            _provenance(conditional, policy['workload_id'], None)
            if (conditional.get('raw_sha256') != policy['calibration']['raw_sha256']
                    or conditional.get('stage') != stage or conditional.get('previous_tier') != previous_tier
                    or conditional.get('context') != 'public_failure_or_unknown'
                    or conditional.get('horizon') != HORIZON
                    or set(conditional.get('includes', [])) != COMPLETE_PATH
                    or conditional.get('effective_probability_includes_fallback') is not True
                    or conditional.get('classifier_version') != policy.get('classifier_version')
                    or conditional.get('contract_sha256') != classifier_contract_hash(
                        self.plan['profiles'], policy['classifier_version'], stage, previous_tier)):
                raise ValueError('Conditional classifier calibration binding mismatch')
            costs = {t: _interval(stage_policy['classified_paths'][t]['cost_usd']) for t in candidates}
            for tier in candidates:
                if costs[tier][1] < _interval(self.plan['records'][tier]['generation_cost_usd'])[1]:
                    raise ValueError('Conditional continuation omits generation cost')
        expected_upper = classifier + max(p * costs[lower][1] + (1-p) * costs[upper][1] for p in (p0, p1))
        direct_lower = _interval(paths[direct]['cost_usd'])[0]
        saving = direct_lower - expected_upper
        result.update(net_saving_lower_usd=str(saving), reason='classification_not_economic')
        # Quantiles do not add. Require a complete classified-flow forecast,
        # measured under the same statistic/workload, never sum per-call p95s.
        if stage_policy.get('classify_flow_ms_upper') is None:
            return {**result, 'reason': 'classified_flow_estimate_unknown'}
        if _d(stage_policy['classify_flow_ms_upper']) > _d(policy['max_flow_ms']):
            return {**result, 'reason': 'classification_exceeds_speed_limit'}
        needed = max(_d(policy['minimum_saving_usd']), direct_lower * _d(policy['minimum_saving_fraction']))
        if saving > needed:
            result.update(action='classify', selected_tier=None, reason='conservative_net_saving_positive')
        return result

    def reservation_plan(self):
        if not self.plan['eligible']['open_source']:
            raise ValueError('Open-source first candidate lacks QCS evidence')
        first = self.decide('low_mid', 'open_source')
        options = list(STAGES['low_mid']) if first['action'] == 'classify' else [first['selected_tier']]
        final, classifiers = set(), 0
        for tier in options:
            choice = self.decide('mid_high', tier)
            final.update(STAGES['mid_high'] if choice['action'] == 'classify' else [choice['selected_tier']])
            classifiers = max(classifiers, int(choice['action'] == 'classify'))
        return {'phases': [['open_source'], options, sorted(final)],
                'classifier_calls': int(first['action'] == 'classify') + classifiers}
