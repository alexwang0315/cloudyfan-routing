"""Explicit three-arm sequential research over qualified QCS backup pools.

This is not a migration of old runs. Each dispatched answer consumes its phase;
availability failures never create an extra same-pool attempt. Healthy backups
are chosen before that phase's dispatch. Unknown fees still stop in the client.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from time import perf_counter

from .. import client as transport
from ..engine import Core, Limits, ToolExecutionError, _lock, _read, _save
from ..recovery_prompts import descriptor as recovery_descriptor, recovery_messages
from .backup_runtime import BackupCategoryCore, VERSION as BACKUP_VERSION
from .catalog import digest, timestamp
from .qcs import _d, _provenance

VERSION = 'sequential-qcs-research-v2'
MODES = {'cloudyfan': 'open_source', 'low_first': 'low', 'open_source_first': 'open_source'}
ENVELOPE = {
    'generation_calls': {'cloudyfan': 3, 'low_first': 2, 'open_source_first': 2},
    'classifier_calls': {'cloudyfan': 2, 'low_first': 0, 'open_source_first': 0},
    'availability': 'consume_attempt_then_next_sequential_stage',
    'backup': 'healthy_candidate_before_stage_dispatch',
    'quality_failure': 'next_sequential_stage',
    'quality_failure_allows_same_configuration_in_next_stage': True,
    'unknown_charge': 'stop',
    'same_pool_retry': False,
    'parallel_dispatch': False,
    'tools': False,
    'zero_first_model_fee_scenario': {'version': 'mrc-first-zero-v1',
                                     'modes': ['cloudyfan'], 'tier': 'open_source', 'attempt': 0},
}


def research_flow_contract_hash(backup_plan):
    """Bind independent whole-flow evidence to this exact narrower runtime."""
    return digest({'backup_plan': backup_plan, 'research_version': VERSION,
                   'envelope': ENVELOPE, 'recovery_prompt': recovery_descriptor()})


def policy_descriptor():
    return {'routing_version': VERSION, 'qcs_backup_version': BACKUP_VERSION,
            'recovery_prompt': recovery_descriptor(), 'envelope': deepcopy(ENVELOPE)}


class SequentialResearchCore(BackupCategoryCore):
    """Reuse real QCS/health/economics; never silently fall back to base Core.

    ``research_policy.whole_flow`` needs separately sourced, applicable evidence
    binding ``research_flow_contract_hash``. Missing evidence blocks construction.
    Synthetic test evidence does not authorize real candidates or API spending.
    """
    _research_version = VERSION

    def __init__(self, client, backup_plan, decision_profile, *, research_policy, **kwargs):
        self._research_policy = deepcopy(research_policy)
        limits = kwargs.get('limits') or Limits(max_turns=1)
        if limits.max_turns != 1:
            raise ValueError('Sequential research requires one tool-free generation per phase')
        if any(r['entry']['profile'].get('backend') == 'local'
               for pool in backup_plan.get('pools', {}).values() for r in pool):
            raise ValueError('This study uses API proxies; actual local runtime is a separate contract')
        kwargs['limits'] = limits
        super().__init__(client, backup_plan, decision_profile, **kwargs)
        self._validate_research_policy(None)

    def _validate_research_policy(self, now):
        policy = self._research_policy
        if (not isinstance(policy, dict) or policy.get('schema_version') != VERSION
                or policy.get('envelope') != ENVELOPE
                or policy.get('recovery_prompt') != recovery_descriptor()):
            raise ValueError('Explicit new research/prompt policy required; old runs cannot migrate')
        flow = policy.get('whole_flow', {})
        base = self._backup['qcs_policy']
        if (flow.get('contract_sha256') != research_flow_contract_hash(self._backup)
                or flow.get('metric') != 'whole_task_flow_ms'
                or flow.get('statistic') != base['speed_statistic']
                or _d(flow['upper']) > _d(base['max_flow_ms'])):
            raise ValueError('Applicable new sequential whole-flow evidence is missing')
        _provenance(flow.get('provenance'), base['workload_id'], now)
        if flow['provenance'].get('category_id') != self._backup['category_id']:
            raise ValueError('Research whole-flow evidence category mismatch')

    def _require_policy_fresh(self):
        super()._require_policy_fresh()
        self._validate_research_policy(self._now().isoformat())

    def configuration_descriptor(self):
        descriptor = super().configuration_descriptor()
        descriptor.update(engine_version='0.5.0', routing_version=VERSION,
                          generation_attempt_limits=deepcopy(ENVELOPE['generation_calls']),
                          research_policy=deepcopy(self._research_policy),
                          cost_comparison=deepcopy(ENVELOPE['zero_first_model_fee_scenario']))
        return descriptor

    def execution_contract_descriptor(self, contract_version):
        descriptor = super().execution_contract_descriptor(contract_version)
        descriptor['core'].pop('research_policy', None)
        descriptor['core'].pop('cost_comparison', None)
        descriptor['core'].update(research_envelope=deepcopy(ENVELOPE),
                                  recovery_prompt_policy=recovery_descriptor())
        return descriptor

    def budget_reservation_plan(self, mode='cloudyfan'):
        if mode not in MODES:
            raise ValueError('Only the three authorized research modes are supported')
        return super().budget_reservation_plan(mode)

    def _with_costs(self, result):
        result = super()._with_costs(result)
        # This latest study hypothesis applies only to CloudyFan MRC. It never
        # changes actual receipt amounts, shared-budget accounting or baselines.
        if result['mode'] != 'cloudyfan':
            scenario = result['costs']['api_usd']
        else:
            amount, unknown = Decimal(0), False
            refs = [(index, call) for index, attempt in enumerate(result['attempts'])
                    for call in attempt['calls']]
            refs += [(None, call) for call in (result.get('selection') or {}).get('calls', [])]
            for index, call in refs:
                if index == 0 and call['tier'] == 'open_source':
                    continue
                record = self.client._load(self.client._directory(call['call_id']))
                info = transport.usage(record)
                if info['cost_unknown']:
                    unknown = True
                else:
                    amount += Decimal(info['cost_usd'])
            scenario = None if unknown else str(amount)
        result['costs']['hypothetical_open_source_zero_usd'] = scenario
        result['costs']['cloudyfan_mrc_zero_first_usd'] = scenario
        result['costs']['cost_scenario_version'] = 'mrc-first-zero-v1'
        result['costs']['waived_call_ids'] = ([call['call_id'] for call in result['attempts'][0]['calls']
            if call['tier'] == 'open_source'] if result['mode'] == 'cloudyfan' else [])
        result['costs']['scenario'] = deepcopy(ENVELOPE['zero_first_model_fee_scenario'])
        return result

    def run(self, run_id, task, environment_factory, *, contract_version, mode='cloudyfan'):
        if run_id != self._bound_run_id or mode not in MODES:
            raise ValueError('New bound run ID and one of three research modes required')
        task = deepcopy(task)
        policy = self._backup['qcs_policy']
        if (not isinstance(task, dict) or not isinstance(task.get('prompt'), str)
                or not task['prompt'].strip() or task.get('workload_id') != policy['workload_id']
                or task.get('category', self._backup['category_id']) != self._backup['category_id']):
            raise ValueError('Explicit matching public task, workload and category required')
        if not isinstance(contract_version, str) or not contract_version.strip():
            raise ValueError('Explicit public validation contract required')
        task['category'] = self._backup['category_id']
        if policy.get('execution_contract') != self.execution_contract_descriptor(contract_version):
            raise ValueError('New calibrated execution/prompt contract differs from actual Core')
        directory = self._manifest.parent
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError('Run directory cannot use symlinks')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(path.is_symlink() for path in directory.iterdir()):
            raise ValueError('Run evidence cannot use symlinks')
        with _lock(directory / '.lock'):
            if (directory / '.retired.json').exists() or (directory / '.retired.json').is_symlink():
                raise ValueError('Retired run cannot be dispatched')
            self._validate_research_policy(None)
            if not self._manifest.exists():
                self._verify_backup_plan()
                self._validate_policy(None)
            frozen = {**self.configuration_descriptor(), 'task': task, 'mode': mode,
                      'contract_version': contract_version}
            _save(self._manifest, frozen)
            completed = directory / 'result.json'
            if completed.exists():
                return self._with_costs(_read(completed))
            timing = directory / 'research-deadline.json'
            if not timing.exists():
                _save(timing, {'started_at': self._now().isoformat(), 'deadline':
                    (self._now() + timedelta(milliseconds=float(_d(policy['max_flow_ms'])))).isoformat()})
            self._deadline = timestamp(_read(timing)['deadline'])
            self._task = task
            env = environment_factory()
            if env.tools_schema():
                raise ValueError('This research execution contract is tool-free')
            _save(directory / 'tool-replay-policy.json', {'replayable_tools': []})
            began, prefix = perf_counter(), 'run-' + run_id
            resumed = any((self.client.root / 'calls').glob(prefix + '-*'))
            attempts, actions, failed, excluded = [], [], set(), set()
            selection = {'decisions': [], 'calls': [], 'classifier_calls': 0}
            maximum, used = ENVELOPE['generation_calls'][mode], 0
            termination = 'generation_call_limit'
            for phase in range(maximum):
                previous = (None if not attempts else {'tier': attempts[-1]['tier'],
                    'profile': attempts[-1]['profile'], 'final': attempts[-1]['final'],
                    'public': attempts[-1]['public']})
                stage = ('low_mid' if phase == 1 else 'mid_high') if mode == 'cloudyfan' and phase else None
                tier = MODES[mode] if phase == 0 else 'high' if mode != 'cloudyfan' else None
                record, selected = self._choose(directory, phase, tier, stage, previous,
                    failed, excluded, task, actions, prefix)
                if record is None:
                    termination = 'no_eligible_candidate'
                    break
                for field in ('decisions', 'calls'):
                    selection[field] += selected[field]
                selection['classifier_calls'] += selected['classifier_calls']
                if selection['classifier_calls'] > ENVELOPE['classifier_calls'][mode]:
                    raise RuntimeError('Global classifier call limit exceeded')
                tier, profile = selected['selected_tier'], deepcopy(record['entry']['profile'])
                messages = recovery_messages(self.system, task['prompt'],
                    attempts[-1] if attempts else None, final_attempt=phase == maximum - 1)
                self._active_record = record
                self.profiles[tier] = profile
                try:
                    attempt = Core._attempt(self, _ToolFreeEnvironment(env), messages, tier,
                                            prefix, phase, actions, directory)
                finally:
                    self.profiles = deepcopy(self._frozen_profiles)
                if len(attempt['calls']) != 1 or used + len(attempt['calls']) > maximum:
                    raise RuntimeError('One actual generation per phase/global limit required')
                attempt.update(profile=profile, configuration_sha256=record['configuration_sha256'],
                               config_id=record['entry']['config_id'])
                for call in attempt['calls']:
                    call.update(profile=deepcopy(profile), configuration_sha256=record['configuration_sha256'])
                attempts.append(attempt)
                used += len(attempt['calls'])
                availability = attempt['stop'] in ('provider_rate_limited', 'provider_unavailable')
                event = 'availability:' + digest({'receipt_root': str(self.client.root.resolve()),
                    'run_id': run_id, 'attempt': phase, 'configuration_sha256': record['configuration_sha256']})
                self.health_store.record(event, record['configuration_sha256'], profile['provider'],
                                         'availability_failure' if availability else 'success')
                if availability:
                    failed.add(profile['provider'])
                    excluded.add(record['configuration_sha256'])
                if attempt['public']['passed'] is True:
                    termination = 'public_passed'
                    break
            if not attempts:
                raise RuntimeError('No healthy qualified initial candidate; no generation dispatched')
            passed = attempts[-1]['public']['passed']
            interrupted = termination == 'no_eligible_candidate'
            result = {'run_id': run_id, 'mode': mode, 'attempts': attempts, 'actions': actions,
                'selection': selection, 'first_public_pass': attempts[0]['public']['passed'],
                'final_public_pass': passed, 'status': 'passed' if passed is True else
                    ('failed' if passed is False and not interrupted else 'unverifiable'), 'resumed': resumed,
                'wall_seconds': None if resumed else perf_counter()-began,
                'config_sha256': digest(frozen), 'generation_calls': used,
                'backup_id': self._backup['backup_id'], 'termination_reason': termination,
                'research_policy_version': self._research_version, 'qcs_backup_version': BACKUP_VERSION,
                'recovery_prompt_policy': recovery_descriptor(),
                'execution_interrupted': interrupted,
                'interruption_kind': 'candidate_unavailable' if interrupted else None,
                'generation_failure_kinds': [
                    'provider_unavailable' if a['stop'] in ('provider_rate_limited', 'provider_unavailable')
                    else 'public_validation_failed' if a['public']['passed'] is False
                    else 'public_validation_unknown' if a['public']['passed'] is None else 'public_passed'
                    for a in attempts]}
            _save(completed, result)
            return self._with_costs(result)


def build_research_core(client, backup_plan, decision_profile, **kwargs):
    """An explicit constructor, with no legacy singleton or policy fallback."""
    return SequentialResearchCore(client, backup_plan, decision_profile, **kwargs)


class _ToolFreeEnvironment:
    """Unadvertised upstream tools cannot reach a caller's side-effecting methods."""
    replayable_tools = ()

    def __init__(self, environment):
        self.base = environment

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        raise ToolExecutionError('Unadvertised model tool call refused by tool-free study')

    def validate(self, answer):
        return self.base.validate(answer)
