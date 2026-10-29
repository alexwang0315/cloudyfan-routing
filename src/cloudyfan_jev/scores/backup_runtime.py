"""Opt-in category backups with durable dispatch choices and bounded paid work.

Preparation is separate from dispatch. Independent candidate/whole-flow evidence
is required; a missing exact classifier calibration disables that classifier.
Health is availability evidence and never substitutes for public validation.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from itertools import product
from pathlib import Path
import re
from time import perf_counter

from .. import client as transport, routing
from ..engine import Core, INITIAL, _encoded, _lock, _read, _save
from ..generation_request import reasoning_matches
from .backup_pools import TIERS, _admit, verify_backup_pools
from .catalog import digest, timestamp
from .qcs import COMPLETE_PATH, HORIZON, _d, _interval, _provenance

VERSION = 'qcs-backup-policy-v1'
ENVELOPE = {'generation_calls': {'cloudyfan': 3, 'baseline': 2}, 'classifier_calls': 2,
            'fallback': 'same_pool_known_fee_no_answer_429_5xx', 'unknown_charge': 'stop',
            'quality_failure': 'next_sequential_quality_stage', 'parallel_dispatch': False}


def _pool_body(plan):
    return {k: v for k, v in plan.items() if k not in ('qcs_policy', 'backup_id')}


def backup_pool_contract_hash(plan):
    """Bind the whole allowlist/evidence and the global fallback/call envelope."""
    return digest({'pool': _pool_body(plan), 'envelope': ENVELOPE})


def backup_flow_contract_hash(plan, policy=None):
    """No embedded-policy/hash cycle; callers supply independent forecasts."""
    policy = plan['qcs_policy'] if policy is None else policy
    return digest({'pool_contract_sha256': backup_pool_contract_hash(plan),
                   'policy': {k: v for k, v in policy.items() if k != 'complete_policy_flow'}})


def _public_profile(profile):
    return {k: deepcopy(profile[k]) for k in (*routing.PROFILE_FIELDS, *routing.OPTIONAL_IDENTITY_FIELDS)
            if k in profile}


def backup_classifier_contract_hash(profiles, version, stage, previous_tier, previous_profile):
    """Previous actual identity is independent of the next pair's tier profile."""
    state = {'task': '<public-workload-task>', 'previous_answer': '<latest-public-answer>',
             'previous_tier': previous_tier, 'previous_configuration': _public_profile(previous_profile),
             'public_diagnostics': '<latest-public-diagnostics>', 'tool_results': []}
    _, _, payload = routing.build_request(stage, state, profiles, version, version, policy_version=VERSION)
    return digest({'payload': payload, 'effective_fallback': 'upper_on_invalid_abstain_or_tie',
                   'unknown_charge': 'stop', 'horizon': HORIZON})


def _rank(record):
    return (_interval(record['evidence']['complete_cost_usd'])[1],
            _d(record['evidence']['speed']['upper']), record['entry']['config_id'])


def _server_rejection(record, requested_model, accepted, provider):
    """A paid-known explicit no-answer 5xx with matching provider identity."""
    status = record.get('http_status')
    if type(status) is not int or not 500 <= status <= 599 or requested_model not in accepted:
        return False
    try:
        data = transport._strict_json(record.get('raw'))
        if not isinstance(data, dict) or data.get('choices') not in (None, []):
            return False
        error = data.get('error')
        if not isinstance(error, dict) or type(error.get('code')) is not int or error['code'] != status:
            return False
        metadata, route = error.get('metadata', {}), data.get('openrouter_metadata', {})
        if not isinstance(metadata, dict) or not isinstance(route, dict):
            return False
        info = transport.usage(record)
        providers = [v for v in (info['provider'], metadata.get('provider_name')) if v is not None]
        return (bool(providers) and all(v == provider for v in providers)
                and (info['model'] is None or info['model'] in accepted)
                and (route.get('requested') is None or route['requested'] in accepted))
    except (ValueError, TypeError, RecursionError):
        return False


class BackupCategoryCore(Core):
    """One run, frozen category/pools/policy, independently refreshed health.

    ``qcs_policy.complete_policy_flow`` is independently sourced, binds
    ``backup_flow_contract_hash`` and carries a whole-task upper forecast.
    Optional ``stages[stage].variants[exact_classifier_contract_hash]`` rows use
    ``calibration``, ``paths``, ``classified_paths``, ``classifier_cost_usd``,
    ``lower_probability`` and ``classify_flow_ms_upper``. Calibration must bind
    ``pool_contract_sha256``, previous actual configuration and exact protocol;
    it is supplied by callers, never manufactured by this runtime.
    """
    def __init__(self, client, backup_plan, decision_profile, *, run_id, health_store,
                 clock=lambda: datetime.now(timezone.utc), **kwargs):
        if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', run_id):
            raise ValueError('Invalid run id')
        self._backup = deepcopy(backup_plan)
        self._bound_run_id, self._clock, self.health_store = run_id, clock, health_store
        self._manifest = Path(client.root) / 'runs' / run_id / 'manifest.json'
        if self._manifest.is_symlink() or self._manifest.parent.is_symlink():
            raise ValueError('Run manifest cannot use symlinks')
        self._verify_backup_plan()
        policy = self._backup['qcs_policy']
        if policy.get('schema_version') != VERSION:
            raise ValueError('Explicit opt-in backup policy required; singleton QCS calibration is incompatible')
        if policy.get('health_policy') != health_store.policy:
            raise ValueError('Backup whole-flow policy must bind the actual normalized health policy')
        if kwargs.get('selection_policy') is not None:
            raise ValueError('Backup runtime owns its exact candidate selection policy')
        if not self._backup['pools']['open_source']:
            raise ValueError('Category has no qualified open-source first candidate')
        if not any(self._backup['pools'][t] for t in ('low', 'mid')) or not any(
                self._backup['pools'][t] for t in ('mid', 'high')):
            raise ValueError('Category has no qualified sequential continuation')
        # Core requires all four profiles; unavailable tiers use a placeholder
        # solely for Core validation and can never be selected by this runner.
        first = next((row['entry']['profile'] for pool in self._backup['pools'].values() for row in pool
                      if row['entry']['profile'].get('backend') != 'local'),
                     self._backup['pools']['open_source'][0]['entry']['profile'])
        profiles = {tier: deepcopy(pool[0]['entry']['profile'] if pool else first)
                    for tier, pool in self._backup['pools'].items()}
        super().__init__(client, profiles, decision_profile, **kwargs)
        self._frozen_profiles = deepcopy(self.profiles)
        self._base_limits = self.limits
        # 主機可在每個 run 綁定快取親和性 port；預設不啟用，排序與原政策完全相同。
        self.affinity, self._affinity_tools, self._affinity_decisions = None, None, {}
        self._deadline = None
        self._validate_policy(None)

    def _verify_backup_plan(self):
        """Strict by default; explicitly versioned research subclasses may differ."""
        verify_backup_pools(self._backup, now=None)

    def _rank_record(self, record):
        return _rank(record)

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError('Backup clock must return a timezone-aware datetime')
        return value.astimezone(timezone.utc)

    def configuration_descriptor(self):
        descriptor = super().configuration_descriptor()
        descriptor.update(engine_version='0.4.0', routing_version=VERSION,
                          backup_plan=deepcopy(self._backup), health_policy=deepcopy(self.health_store.policy),
                          backup_envelope=deepcopy(ENVELOPE))
        return descriptor

    def execution_contract_descriptor(self, contract_version):
        descriptor = super().execution_contract_descriptor(contract_version)
        # Pool/protocol are bound separately, avoiding a policy/descriptor cycle.
        for field in ('backup_plan', 'health_policy', 'backup_envelope'):
            descriptor['core'].pop(field, None)
        return descriptor

    def _validate_policy(self, now):
        policy = self._backup['qcs_policy']
        routing._reject_private(policy)
        for key in ('minimum_saving_usd', 'minimum_saving_fraction'):
            _d(policy.get(key, '0'))
        if _d(policy.get('minimum_saving_fraction', '0')) > 1:
            raise ValueError('Saving fraction out of range')
        flow = policy.get('complete_policy_flow', {})
        if (flow.get('metric') != 'whole_task_flow_ms' or flow.get('statistic') != policy['speed_statistic']
                or _d(flow['upper']) > _d(policy['max_flow_ms'])
                or flow.get('contract_sha256') != backup_flow_contract_hash(self._backup)):
            raise ValueError('Independent whole-task backup flow contract required')
        _provenance(flow['provenance'], policy['workload_id'], now)
        if flow['provenance'].get('category_id') != self._backup['category_id']:
            raise ValueError('Whole-flow category mismatch')
        if policy.get('classifier_version') not in (None, self.decision['version']):
            raise ValueError('Calibrated classifier version differs from Core')
        for stage, tiers in routing.STAGES.items():
            variants = policy.get('stages', {}).get(stage, {}).get('variants', {})
            if not isinstance(variants, dict):
                raise ValueError('Explicit exact-contract classifier variants required')
            previous_tiers = ('open_source',) if stage == 'low_mid' else ('low', 'mid')
            contracts = {}
            for previous_tier in previous_tiers:
                for previous, lower, upper in product(self._backup['pools'][previous_tier],
                        self._backup['pools'][tiers[0]], self._backup['pools'][tiers[1]]):
                    pair = {tiers[0]: lower['entry']['profile'], tiers[1]: upper['entry']['profile']}
                    key = backup_classifier_contract_hash(pair, self.decision['version'], stage,
                                                          previous_tier, previous['entry']['profile'])
                    contracts[key] = previous_tier, previous, {tiers[0]: lower, tiers[1]: upper}
            for key, variant in variants.items():
                if key not in contracts:
                    raise ValueError('Classifier variant is not an exact reachable pool contract')
                previous_tier, previous, pair = contracts[key]
                self._validate_variant(stage, key, variant, previous_tier, previous, pair, now)

    def _validate_variant(self, stage, key, variant, previous_tier, previous, pair, now):
        policy = self._backup['qcs_policy']
        calibration = variant['calibration']
        _provenance(calibration, policy['workload_id'], now)
        if (calibration.get('category_id') != self._backup['category_id']
                or calibration.get('raw_sha256') != policy['complete_policy_flow']['provenance']['raw_sha256']
                or calibration.get('contract_sha256') != key
                or calibration.get('pool_contract_sha256') != backup_pool_contract_hash(self._backup)
                or calibration.get('previous_configuration_sha256') != previous['configuration_sha256']
                or calibration.get('previous_tier') != previous_tier or calibration.get('stage') != stage
                or calibration.get('classifier_version') != self.decision['version']
                or policy.get('classifier_version') != self.decision['version']
                or calibration.get('context') != 'public_failure_or_unknown'
                or calibration.get('horizon') != HORIZON
                or set(calibration.get('includes', [])) != COMPLETE_PATH
                or calibration.get('effective_probability_includes_fallback') is not True):
            raise ValueError('Independent backup classifier calibration binding mismatch')
        if _interval(variant['lower_probability'])[1] > 1:
            raise ValueError('Classifier probability out of range')
        _interval(variant['classifier_cost_usd'])
        _d(variant['classify_flow_ms_upper'])
        for tier, record in pair.items():
            if (_interval(variant['paths'][tier]['cost_usd'])[1]
                    < _interval(record['evidence']['complete_cost_usd'])[1]):
                raise ValueError('Direct continuation omits complete cost')
            if (_interval(variant['classified_paths'][tier]['cost_usd'])[1]
                    < _interval(record['evidence']['generation_cost_usd'])[1]):
                raise ValueError('Classified continuation omits generation cost')
            if _d(variant['paths'][tier]['flow_ms_upper']) > _d(policy['max_flow_ms']):
                raise ValueError('Direct continuation exceeds flow limit')

    def _economic(self, stage, previous, pair):
        policy = self._backup['qcs_policy']
        profiles = {tier: record['entry']['profile'] for tier, record in pair.items()}
        key = (backup_classifier_contract_hash(profiles, self.decision['version'], stage,
                previous['tier'], previous['profile']) if len(pair) == 2 else None)
        variant = policy.get('stages', {}).get(stage, {}).get('variants', {}).get(key)
        paths = ({tier: {'cost_usd': record['evidence']['complete_cost_usd'],
                        'flow_ms_upper': record['evidence']['speed']['upper']} for tier, record in pair.items()}
                 if variant is None else variant['paths'])
        direct = min(pair, key=lambda tier: (_interval(paths[tier]['cost_usd'])[1],
                     _d(paths[tier]['flow_ms_upper']), tier))
        outcome = {'stage': stage, 'action': 'direct', 'selected_tier': direct,
                   'reason': 'exact_classifier_calibration_missing', 'contract_sha256': key}
        if variant is None:
            return outcome
        with localcontext() as context:
            context.prec = 512
            lower, upper = routing.STAGES[stage]
            p0, p1 = _interval(variant['lower_probability'])
            _, fee = _interval(variant['classifier_cost_usd'])
            costs = {tier: _interval(variant['classified_paths'][tier]['cost_usd'])[1] for tier in pair}
            expected = fee + max(p * costs[lower] + (1-p) * costs[upper] for p in (p0, p1))
            direct_lower = _interval(paths[direct]['cost_usd'])[0]
            saving = direct_lower - expected
            needed = max(_d(policy.get('minimum_saving_usd', '0')),
                         direct_lower * _d(policy.get('minimum_saving_fraction', '0')))
            outcome.update(reason='classification_not_economic', net_saving_lower_usd=str(saving))
            if (saving > needed and _d(variant['classify_flow_ms_upper']) <= _d(policy['max_flow_ms'])):
                outcome.update(action='classify', selected_tier=None, reason='conservative_net_saving_positive')
        return outcome

    def budget_reservation_plan(self, mode='cloudyfan'):
        if mode not in INITIAL:
            raise ValueError('Unknown workflow mode')
        tiers = TIERS if mode == 'cloudyfan' else (INITIAL[mode], 'high')
        classifiers = 0
        if mode == 'cloudyfan':
            for stage, pair_tiers in routing.STAGES.items():
                possible = False
                for previous_tier in (('open_source',) if stage == 'low_mid' else ('low', 'mid')):
                    for previous, low, high in product(self._backup['pools'][previous_tier],
                            self._backup['pools'][pair_tiers[0]], self._backup['pools'][pair_tiers[1]]):
                        choice = self._economic(stage, {'tier': previous_tier, 'profile': previous['entry']['profile']},
                                                {pair_tiers[0]: low, pair_tiers[1]: high})
                        possible |= choice['action'] == 'classify'
                classifiers += int(possible)
        profiles = [deepcopy(r['entry']['profile']) for tier in dict.fromkeys(tiers)
                    for r in self._backup['pools'][tier]]
        return {'generation_profiles': profiles, 'max_generation_calls': 3 if mode == 'cloudyfan' else 2,
                'classifier_calls': classifiers, 'classifier_profile': deepcopy(self.decision) if classifiers else None,
                'max_tool_calls': self._base_limits.max_tool_calls}

    @staticmethod
    def _equivalent(record, base):
        """同池、相同完整成本區間與整件速度統計量／區間；不採任何容差。"""
        def exact(value):
            return {key: (_d(item) if key in ('lower', 'upper') else item) for key, item in value.items()}
        return (exact(record['evidence']['complete_cost_usd']) == exact(base['evidence']['complete_cost_usd'])
                and exact(record['evidence']['speed']) == exact(base['evidence']['speed']))

    def _affinity_choice(self, tier, decision_key, eligible, base):
        """只在全部閘門之後、等價集合內，以主機的可信前綴提示作次要排序。"""
        equal = sorted((r for r in eligible if self._equivalent(r, base)), key=self._rank_record)
        decision = {'decision_key': decision_key, 'tier': tier, 'policy_version': None,
                    'equivalent_config_ids': [r['entry']['config_id'] for r in equal],
                    'original_config_id': base['entry']['config_id'], 'selected_config_id': base['entry']['config_id'],
                    'adopted': False, 'reason': 'no_equivalent_set', 'observed_at': None,
                    'matched_messages': 0, 'previous_started_at': None}
        if len(equal) < 2:
            return base, decision
        messages = [{'role': 'system', 'content': self.system}, {'role': 'user', 'content': self._task['prompt']}]
        try:
            tools = self._affinity_tools() if callable(self._affinity_tools) else (self._affinity_tools or [])
            answer = self.affinity({'decision_key': decision_key, 'tier': tier, 'candidates': [
                {'config_id': r['entry']['config_id'], 'configuration_sha256': r['configuration_sha256'],
                 'payload': self.generation_payload(messages, r['entry']['profile'],
                                                    'run-' + self._bound_run_id, tools)}
                for r in equal]})
        except Exception:
            return base, {**decision, 'reason': 'affinity_port_failed'}
        if not isinstance(answer, dict):
            return base, {**decision, 'reason': 'affinity_port_failed'}
        decision.update({key: answer.get(key) for key in ('policy_version', 'observed_at', 'previous_started_at')},
                        matched_messages=answer.get('matched_messages') or 0,
                        reason=answer.get('reason') or 'affinity_hint_unavailable')
        preferred = next((r for r in equal if r['entry']['config_id'] == answer.get('preferred')), None)
        if answer.get('preferred') is not None and preferred is None:
            return base, {**decision, 'reason': 'affinity_preference_outside_equivalent_set'}
        if preferred is None:
            return base, decision
        return preferred, {**decision, 'selected_config_id': preferred['entry']['config_id'],
                           'adopted': preferred is not base}

    def _expected_choice(self, eligible, decision):
        """重播：只接受保存的、仍在等價集合內的親和性選擇，否則就是原排序。"""
        base = min(eligible, key=self._rank_record)
        if not decision or not decision.get('adopted'):
            return base
        equal = [r for r in eligible if self._equivalent(r, base)]
        preferred = [r for r in equal if r['entry']['config_id'] == decision.get('selected_config_id')]
        if (len(preferred) != 1 or decision.get('equivalent_config_ids') != [r['entry']['config_id']
                for r in sorted(equal, key=self._rank_record)]):
            raise ValueError('Frozen backup affinity choice is outside the equivalent set')
        return preferred[0]

    def _health_choice(self, tier, failed_providers, excluded_configs, *, window_check=None, decision_key=None):
        eligible, observations = [], []
        for record in self._backup['pools'][tier]:
            profile = record['entry']['profile']
            health = self.health_store.assess(record['configuration_sha256'], profile['provider'])
            reasons = list(health['reasons'])
            try:
                self._require_candidate_fresh(record)
            except (ValueError, KeyError, TypeError, AttributeError):
                reasons.append('candidate_qualification_stale_or_invalid')
            if profile.get('backend') == 'local' and (self.local_backend is None
                    or not self.local_backend.matches_profile(profile)):
                reasons.append('local_backend_not_registered')
            if profile['provider'] in failed_providers:
                reasons.append('provider_failed_this_run')
            if record['configuration_sha256'] in excluded_configs:
                reasons.append('configuration_already_dispatched')
            if profile['model_version'].endswith(':free') and not (
                    self._backup['qcs_policy'].get('allow_cloud_free_trial') is True
                    and self._task.get('data_policy') in ('public', 'synthetic')):
                reasons.append('cloud_free_trial_not_explicitly_authorized')
            if self._deadline is not None and _d(record['evidence']['speed']['upper']) > Decimal(
                    str((self._deadline - self._now()).total_seconds() * 1000)):
                reasons.append('remaining_task_time_insufficient')
            window = None
            check_window = window_check or getattr(self.local_backend if profile.get('backend') == 'local' else self.client,
                                                   'candidate_window', None)
            if callable(check_window):
                window = check_window(profile)
                if window is not None and window['status'] != 'open':
                    reasons.append(window['reason_code'])
            observations.append({'config_id': record['entry']['config_id'], 'assessment': health, 'reasons': reasons,
                **({'capacity_window': window} if window is not None else {})})
            if not reasons:
                eligible.append(record)
        choice = min(eligible, key=self._rank_record) if eligible else None
        if choice is not None and decision_key is not None and self.affinity is not None:
            choice, self._affinity_decisions[decision_key] = self._affinity_choice(tier, decision_key, eligible, choice)
        return (deepcopy(choice) if choice is not None else None), observations

    def queue_accept(self, run_id, accepted_at):
        if run_id != self._bound_run_id:
            raise ValueError('Backup queue run identity changed')
        directory = self._manifest.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        timing = directory / 'backup-deadline.json'
        if not timing.exists():
            start = datetime.fromtimestamp(accepted_at, timezone.utc)
            _save(timing, {'started_at': start.isoformat(), 'deadline':
                (start + timedelta(milliseconds=float(_d(self._backup['qcs_policy']['max_flow_ms'])))).isoformat()})

    def _queue_deadline(self):
        from ..capacity_dispatch import CapacityDenied
        timing = self._manifest.parent / 'backup-deadline.json'
        if not timing.exists():
            raise CapacityDenied('capacity_original_deadline_unavailable')
        self._deadline = timestamp(_read(timing)['deadline'])
        if self._now() >= self._deadline:
            raise CapacityDenied('capacity_original_deadline_exceeded')

    def queue_initial(self, run_id, task, *, mode, tools, candidate_window=None):
        from ..capacity_dispatch import CapacityDenied
        self._queue_deadline()
        self._task = deepcopy(task)
        self._require_policy_fresh()
        self._affinity_tools = tools
        record, observations = self._health_choice(INITIAL[mode], set(), set(), window_check=candidate_window,
                                                   decision_key='0:' + INITIAL[mode])
        if record is None:
            raise CapacityDenied('capacity_original_candidate_unavailable')
        profile = record['entry']['profile']
        prefix = 'run-' + run_id
        messages = [{'role': 'system', 'content': self.system}, {'role': 'user', 'content': task['prompt']}]
        return {'call_id': prefix + '-a0-t0', 'payload': self.generation_payload(messages, profile, prefix, tools),
                'local': profile.get('backend') == 'local', 'gate': {'kind': 'generation', 'record': record}}

    def queue_validate(self, next_request):
        from ..capacity_dispatch import CapacityDenied
        self._queue_deadline()
        self._require_policy_fresh()
        gate = next_request.get('gate')
        try:
            if gate['kind'] == 'generation':
                record = gate['record']
                if not any(record in rows for rows in self._backup['pools'].values()):
                    raise ValueError('Saved candidate is outside frozen backup pool')
                if next_request['payload']['model'] != record['entry']['profile']['model_version']:
                    raise ValueError('Saved candidate differs from next model')
                self._active_record = record
                self._queue_location(record['entry']['profile'])
                self._require_active_health()
            elif gate['kind'] == 'classifier':
                self._active_selection = gate['selection']
                if self.execution_policy is not None:
                    self.execution_policy.require('classifier', self.execution_policy.classifier)
                if next_request['payload']['model'] != self.decision['version']:
                    raise ValueError('Saved classifier differs from exact version')
                for record in self._active_selection['pair'].values():
                    if not any(record in rows for rows in self._backup['pools'].values()):
                        raise ValueError('Saved classifier pair is outside frozen pool')
                self._require_selection_fresh()
            else:
                raise ValueError('Unknown original gate')
        except Exception:
            raise CapacityDenied('capacity_original_gate_denied') from None

    def _capacity_post_guard(self, generation):
        from ..capacity_dispatch import CapacityDenied
        if self._deadline is not None and self._now() >= self._deadline:
            raise CapacityDenied('capacity_original_deadline_exceeded')
        try:
            self._require_policy_fresh()
            if generation:
                self._require_active_health()
            else:
                self._require_selection_fresh()
        except Exception:
            raise CapacityDenied('capacity_original_gate_denied') from None
        return True

    def _checked(self, cid, url, payload, accepted, provider, *, allow_rejection=False):
        guard = getattr(self.client, 'guard_dispatch', None)
        if callable(guard):
            queue_enabled = getattr(self.client, 'queue_preparation_enabled', lambda: False)()
            evidence = ({'kind': 'generation', 'record': self._active_record} if allow_rejection else
                        {'kind': 'classifier', 'selection': self._active_selection}) if queue_enabled else None
            with guard(lambda: self._capacity_post_guard(allow_rejection),
                       **({'queue_evidence': evidence} if queue_enabled else {})):
                return self._checked_original(cid, url, payload, accepted, provider, allow_rejection=allow_rejection)
        return self._checked_original(cid, url, payload, accepted, provider, allow_rejection=allow_rejection)

    def _checked_original(self, cid, url, payload, accepted, provider, *, allow_rejection=False):
        directory = self.client._directory(cid)
        # Completed/intent receipts replay under the same ID; Client refuses an
        # unresolved earlier intent. A deadline never authorizes a new request.
        if not (directory / 'intent.json').exists():
            if self._deadline is not None and self._now() >= self._deadline:
                raise RuntimeError('Dispatch stopped: original whole-task deadline exceeded')
            self._require_policy_fresh()
            if allow_rejection:
                self._require_active_health()
            else:
                self._require_selection_fresh()
        if not allow_rejection:
            return super()._checked(cid, url, payload, accepted, provider, allow_rejection=False)
        record = self.client.call(cid, url, payload)
        info = transport.usage(record)
        if info['cost_unknown'] and info['generation_id']:
            record = self.client.reconcile(cid)
            info = transport.usage(record)
        if info['cost_unknown'] or not info['generation_id'] or record.get('duplicate_generation_id'):
            raise RuntimeError('Dispatch stopped: incomplete billing, identity or transport: ' + cid)
        rejected = transport.rate_limit_rejection(record, payload['model'], accepted, provider)
        unavailable = _server_rejection(record, payload['model'], accepted, provider)
        if rejected or unavailable:
            return record, {'call_id': cid, **info, 'requested_model': payload['model'],
                'requested_provider': provider, 'provider_rejected': True, 'provider_unavailable': unavailable}
        if record.get('http_status') != 200 or info['model'] not in accepted or info['provider'] != provider:
            raise RuntimeError('Dispatch stopped: model/provider mismatch or invalid response: ' + cid)
        return record, {'call_id': cid, **info}

    def _require_policy_fresh(self):
        policy = self._backup['qcs_policy']
        _provenance(policy['complete_policy_flow']['provenance'], policy['workload_id'], self._now().isoformat())

    def _require_candidate_fresh(self, record):
        _admit(record['entry'], record['evidence'], self._backup['snapshot'], self._backup['category_id'],
               self._backup['requirements'], self._backup['qcs_policy'], self._now().isoformat())
        snapshot = self._backup['snapshot']
        if not timestamp(snapshot['created_at']) <= self._now() < timestamp(snapshot['valid_until']):
            raise ValueError('Frozen candidate snapshot has expired')

    def _require_selection_fresh(self):
        selection = self._active_selection
        for record in selection['pair'].values():
            self._require_candidate_fresh(record)
            health = self.health_store.assess(record['configuration_sha256'], record['entry']['profile']['provider'])
            if not health['eligible']:
                raise RuntimeError('Dispatch stopped: classifier candidate health is no longer eligible')
        variant = self._backup['qcs_policy']['stages'][selection['stage']]['variants'][
            selection['economic']['contract_sha256']]
        _provenance(variant['calibration'], self._backup['qcs_policy']['workload_id'], self._now().isoformat())

    def _require_active_health(self):
        record = self._active_record
        self._require_policy_fresh()
        self._require_candidate_fresh(record)
        health = self.health_store.assess(record['configuration_sha256'], record['entry']['profile']['provider'])
        if not health['eligible']:
            raise RuntimeError('Dispatch stopped: selected health observation is no longer eligible')

    def _with_costs(self, result):
        result = deepcopy(result)
        refs = [c for a in result['attempts'] for c in a['calls']] + (result.get('selection') or {}).get('calls', [])
        cost, scenario, classifier, unknown = Decimal(0), Decimal(0), Decimal(0), 0
        for call in refs:
            profile = call['profile']
            if call['tier'] == 'classifier':
                if profile != self.decision:
                    raise RuntimeError('Saved classifier profile differs from frozen configuration')
            elif not any(profile == row['entry']['profile']
                         and call.get('configuration_sha256') == row['configuration_sha256']
                         for row in self._backup['pools'].get(call['tier'], [])):
                raise RuntimeError('Saved actual profile is outside frozen backup pool')
            if call.get('execution_kind') == 'local':
                record = self.local_backend._load(self.local_backend._directory(call['call_id']))
                info = self.local_backend.checked(record)
            else:
                call_directory = self.client._directory(call['call_id'])
                record = self.client._load(call_directory)
                payload = _read(call_directory / 'intent.json')['payload']
                if call['tier'] == 'classifier':
                    exact_request = payload.get('model') == profile['version']
                else:
                    exact_request = (payload.get('model') == profile['model_version']
                        and payload.get('provider') == {'only': [profile['provider']],
                            'allow_fallbacks': False, 'require_parameters': True}
                        and reasoning_matches(profile, payload))
                if not exact_request:
                    raise RuntimeError('Saved actual profile differs from immutable dispatch intent')
                info = transport.usage(record)
                is_classifier = call['tier'] == 'classifier'
                accepted = [profile['model'], profile['version']] if is_classifier else profile['accepted_models']
                rejected = not is_classifier and (transport.rate_limit_rejection(record, profile['model_version'],
                    accepted, profile['provider_name']) or _server_rejection(record, profile['model_version'],
                    accepted, profile['provider_name']))
                if (record.get('duplicate_generation_id') or not info['generation_id']
                        or (not rejected and (info['model'] not in accepted or info['provider'] != profile['provider_name']))):
                    raise RuntimeError('Reconciled actual-profile identity mismatch: ' + call['call_id'])
            call.update(info)
            if info['cost_unknown']:
                unknown += 1
            else:
                amount = Decimal(info['cost_usd'])
                cost += amount
                if call['tier'] != 'open_source': scenario += amount
                if call['tier'] == 'classifier': classifier += amount
        result['costs'] = {'known_api_usd': str(cost), 'unknown_calls': unknown,
            'api_usd': None if unknown else str(cost), 'classifier_usd': str(classifier),
            'hypothetical_open_source_zero_usd': None if unknown else str(scenario),
            'host_and_human_usd': None, 'calls': len(refs)}
        return result

    def _choose(self, directory, index, tier, stage, previous, failed, excluded, task, actions, prefix):
        checkpoint = directory / f'backup-selection-{index}.json'
        if checkpoint.exists():
            saved = _read(checkpoint)
        else:
            tiers = routing.STAGES[stage] if stage else (tier,)
            pair, health = {}, {}
            for candidate_tier in tiers:
                record, health[candidate_tier] = self._health_choice(candidate_tier, failed, excluded,
                    decision_key=f'{index}:{candidate_tier}')
                if record is not None:
                    pair[candidate_tier] = record
            affinity = {key: value for key, value in self._affinity_decisions.items()
                        if key in {f'{index}:{t}' for t in tiers}}
            if not pair:
                _save(checkpoint, {'pair': {}, 'health': health, 'economic': {
                    'action': 'stop', 'reason': 'no_eligible_candidate'},
                    'previous': deepcopy(previous), 'actions': deepcopy(actions)})
                return None, None
            economic = self._economic(stage, previous, pair) if stage else {
                'action': 'direct', 'selected_tier': tier, 'reason': 'same_pool_availability_or_initial'}
            saved = {'pair': pair, 'health': health, 'economic': economic,
                     'previous': deepcopy(previous), 'actions': deepcopy(actions),
                     **({'affinity': affinity} if affinity else {})}
            _save(checkpoint, saved)
        tiers = routing.STAGES[stage] if stage else (tier,)
        if set(saved['pair']) - set(tiers) or set(saved['health']) != set(tiers):
            raise ValueError('Frozen backup selection tier scope changed')
        for candidate_tier, candidate in saved['pair'].items():
            if candidate not in self._backup['pools'][candidate_tier]:
                raise ValueError('Frozen backup selection candidate is outside admitted pool')
            observations = saved['health'][candidate_tier]
            expected_ids = [r['entry']['config_id'] for r in self._backup['pools'][candidate_tier]]
            if [r['config_id'] for r in observations] != expected_ids:
                raise ValueError('Frozen backup selection health identity changed')
            eligible = [r for r in self._backup['pools'][candidate_tier]
                        if any(h['config_id'] == r['entry']['config_id'] and not h['reasons'] for h in observations)]
            if not eligible or candidate != self._expected_choice(
                    eligible, saved.get('affinity', {}).get(f'{index}:{candidate_tier}')):
                raise ValueError('Frozen backup selection differs from recorded health choice')
        expected_economic = (self._economic(stage, previous, saved['pair']) if stage and saved['pair'] else
            {'action': 'direct', 'selected_tier': tier, 'reason': 'same_pool_availability_or_initial'}
            if saved['pair'] else {'action': 'stop', 'reason': 'no_eligible_candidate'})
        if saved['economic'] != expected_economic:
            raise ValueError('Frozen backup selection economics changed')
        if saved['previous'] != previous or saved['actions'] != actions:
            raise ValueError('Frozen backup selection public inputs changed')
        economic, pair = saved['economic'], saved['pair']
        if not pair:
            return None, None
        selected = {'decisions': [economic], 'classifier_calls': 0, 'calls': [],
                    'selected_tier': economic['selected_tier']}
        if economic['action'] == 'classify':
            if self.execution_policy:
                location = self.execution_policy.classifier
                self.execution_policy.require('classifier', location)
                if not location.startswith('cloud:'):
                    raise ValueError('Classifier backend location mismatch')
            profiles = {t: r['entry']['profile'] for t, r in pair.items()}
            state = {'task': task['prompt'], 'previous_answer': previous['final'],
                'previous_tier': previous['tier'], 'previous_configuration': _public_profile(previous['profile']),
                'public_diagnostics': previous['public'],
                'tool_results': [{k: a[k] for k in ('name', 'arguments', 'result')} for a in actions]}
            calls = []
            self._active_selection = {'pair': pair, 'stage': stage, 'economic': economic}
            def decide(stage, context, questions, payload):
                record, info = self._checked(prefix + '-decision-' + stage, transport.DECISIONS, payload,
                    [self.decision['model'], self.decision['version']], self.decision['provider_name'])
                calls.append({'tier': 'classifier', 'profile': deepcopy(self.decision), **info})
                return record['raw']
            selected = routing.route(False, state, profiles, self.decision['version'], decide,
                model_version=self.decision['version'], allowed_response_models=(self.decision['version'],),
                stage=stage, policy_version=VERSION)
            selected['decisions'] = [economic, *selected['decisions']]
            selected['calls'] = calls
        return pair[selected['selected_tier']], selected

    def run(self, run_id, task, environment_factory, *, contract_version, mode='cloudyfan'):
        if run_id != self._bound_run_id or mode not in INITIAL:
            raise ValueError('Backup Core requires its bound run and a known mode')
        task = deepcopy(task)
        policy = self._backup['qcs_policy']
        if (not isinstance(task, dict) or not isinstance(task.get('prompt'), str) or not task['prompt'].strip()
                or task.get('workload_id') != policy['workload_id']
                or task.get('category', self._backup['category_id']) != self._backup['category_id']):
            raise ValueError('Explicit matching public task, workload and category required')
        if not isinstance(contract_version, str) or not contract_version.strip():
            raise ValueError('Explicit public validation contract required')
        task['category'] = self._backup['category_id']
        if policy.get('execution_contract') != self.execution_contract_descriptor(contract_version):
            raise ValueError('Backup calibrated execution contract differs from actual Core')
        directory = self._manifest.parent
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError('Run directory cannot use symlinks')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(path.is_symlink() for path in directory.iterdir()):
            raise ValueError('Run evidence cannot use symlinks')
        with _lock(directory / '.lock'):
            if (directory / '.retired.json').exists() or (directory / '.retired.json').is_symlink():
                raise ValueError('Retired run cannot be dispatched')
            if not self._manifest.exists():
                self._verify_backup_plan()
                self._validate_policy(None)
            frozen = {**self.configuration_descriptor(), 'task': task, 'mode': mode,
                      'contract_version': contract_version}
            _save(self._manifest, frozen)
            completed = directory / 'result.json'
            if completed.exists():
                return self._with_costs(_read(completed))
            timing = directory / 'backup-deadline.json'
            if not timing.exists():
                _save(timing, {'started_at': self._now().isoformat(), 'deadline':
                    (self._now() + timedelta(milliseconds=float(_d(policy['max_flow_ms'])))).isoformat()})
            self._deadline = timestamp(_read(timing)['deadline'])
            self._task = task
            env = environment_factory()
            replayable = getattr(env, 'replayable_tools', ())
            if not isinstance(replayable, (tuple, list, set, frozenset)) or any(
                    not isinstance(name, str) or not name for name in replayable):
                raise ValueError('Explicit replayable tools required')
            _save(directory / 'tool-replay-policy.json', {'replayable_tools': sorted(set(replayable))})
            # 只在親和性 port 真的需要時才取工具 schema，不改變原有呼叫時機。
            self._affinity_tools = env.tools_schema
            began, prefix = perf_counter(), 'run-' + run_id
            resumed = any((Path(self.client.root) / 'calls').glob(prefix + '-*'))
            messages = [{'role': 'system', 'content': self.system}, {'role': 'user', 'content': task['prompt']}]
            actions, attempts, failed, excluded = [], [], set(), set()
            selection = {'decisions': [], 'calls': [], 'classifier_calls': 0}
            maximum, used, phase, stage, previous = (3 if mode == 'cloudyfan' else 2), 0, 0, None, None
            tier, termination, choice_index = INITIAL[mode], 'generation_call_limit', 0
            while used < maximum:
                index = len(attempts)
                record, selected = self._choose(directory, choice_index, tier, stage, previous, failed,
                                                excluded, task, actions, prefix)
                choice_index += 1
                if (record is None and stage is None and attempts and attempts[-1]['stop'] in
                        ('provider_rate_limited', 'provider_unavailable')
                        and phase < (2 if mode == 'cloudyfan' else 1)):
                    phase += 1
                    stage = ('low_mid' if phase == 1 else 'mid_high') if mode == 'cloudyfan' else None
                    if mode != 'cloudyfan': tier = 'high'
                    continue
                if record is None:
                    termination = 'no_eligible_candidate'
                    break
                for field in ('decisions', 'calls'):
                    selection[field] += selected[field]
                selection['classifier_calls'] += selected['classifier_calls']
                if selection['classifier_calls'] > 2:
                    raise RuntimeError('Global classifier call limit exceeded')
                tier = selected['selected_tier']
                profile = deepcopy(record['entry']['profile'])
                if profile.get('backend') == 'local' and (self.local_backend is None
                        or not self.local_backend.matches_profile(profile)):
                    raise ValueError('Backup local profile requires its exact registered backend')
                if attempts:
                    messages = [{k: v for k, v in m.items() if k not in ('reasoning', 'reasoning_details')}
                                for m in messages]
                    messages.append({'role': 'user', 'content': 'Public validation did not establish completion. '
                        'Repair the task using these public diagnostics: ' + _encoded(attempts[-1]['public']).decode()})
                self._active_record = record
                self.profiles[tier] = profile
                self.limits = replace(self._base_limits, max_turns=min(self._base_limits.max_turns, maximum-used),
                    max_tool_calls=max(1, self._base_limits.max_tool_calls-len(actions)))
                backend = self.local_backend
                if profile.get('backend') == 'local':
                    self.local_backend = _LocalBound(backend, self)
                try:
                    attempt = super()._attempt(_ToolBound(env, actions, self._base_limits.max_tool_calls),
                                               messages, tier, prefix, index, actions, directory)
                finally:
                    self.local_backend = backend
                    self.profiles = deepcopy(self._frozen_profiles)
                    self.limits = self._base_limits
                attempt.update(profile=profile, configuration_sha256=record['configuration_sha256'],
                               config_id=record['entry']['config_id'])
                for call in attempt['calls']:
                    call.update(profile=deepcopy(profile), configuration_sha256=record['configuration_sha256'])
                attempts.append(attempt)
                used += len(attempt['calls'])
                excluded.add(record['configuration_sha256'])
                availability = attempt['stop'] in ('provider_rate_limited', 'provider_unavailable')
                health_event = 'availability:' + digest({'receipt_root': str(Path(self.client.root).resolve()),
                    'run_id': run_id, 'attempt': index, 'configuration_sha256': record['configuration_sha256']})
                self.health_store.record(health_event, record['configuration_sha256'],
                    profile['provider'], 'availability_failure' if availability else 'success')
                if attempt['public']['passed'] is True:
                    termination = 'public_passed'
                    break
                previous = {'tier': tier, 'profile': profile, 'final': attempt['final'], 'public': attempt['public']}
                if availability:
                    failed.add(profile['provider'])
                    stage = None
                else:
                    phase += 1
                    if phase >= (3 if mode == 'cloudyfan' else 2):
                        termination = 'quality_stage_limit'
                        break
                    stage = ('low_mid' if phase == 1 else 'mid_high') if mode == 'cloudyfan' else None
                    if mode != 'cloudyfan': tier = 'high'
            if not attempts:
                raise RuntimeError('No healthy qualified initial candidate; no generation dispatched')
            passed = attempts[-1]['public']['passed']
            result = {'run_id': run_id, 'mode': mode, 'attempts': attempts, 'actions': actions,
                'selection': selection, 'first_public_pass': attempts[0]['public']['passed'],
                'final_public_pass': passed, 'status': 'passed' if passed is True else
                    ('failed' if passed is False else 'unverifiable'), 'resumed': resumed,
                'wall_seconds': None if resumed else perf_counter()-began,
                'config_sha256': digest(frozen), 'generation_calls': used,
                'backup_id': self._backup['backup_id'], 'termination_reason': termination}
            _save(completed, result)
            return self._with_costs(result)


class _ToolBound:
    """Bound total actual tool execution across model turns and fallback phases."""
    def __init__(self, env, actions, maximum):
        self.env, self.actions, self.maximum = env, actions, maximum

    def tools_schema(self):
        return self.env.tools_schema()

    def call(self, name, arguments):
        if len(self.actions) >= self.maximum:
            raise RuntimeError('Global tool execution limit exceeded')
        return self.env.call(name, arguments)

    def validate(self, final):
        return self.env.validate(final)

    def __getattr__(self, name):
        return getattr(self.env, name)


class _LocalBound:
    """Apply the same original deadline and health gate to each local dispatch."""
    def __init__(self, backend, core):
        self.backend, self.core = backend, core

    def call(self, cid, payload):
        if not (self.backend._directory(cid) / 'intent.json').exists():
            if self.core._deadline is not None and self.core._now() >= self.core._deadline:
                raise RuntimeError('Dispatch stopped: original whole-task deadline exceeded')
            self.core._require_active_health()
        guard = getattr(self.backend, 'guard_dispatch', None)
        if callable(guard):
            queued = getattr(self.backend, 'queue_preparation_enabled', lambda: False)()
            with guard(lambda: self.core._capacity_post_guard(True), **({'queue_evidence': {
                    'kind': 'generation', 'record': self.core._active_record}} if queued else {})):
                return self.backend.call(cid, payload)
        return self.backend.call(cid, payload)

    def __getattr__(self, name):
        return getattr(self.backend, name)
