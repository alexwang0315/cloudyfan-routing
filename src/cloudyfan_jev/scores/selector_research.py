"""Opt-in research selectors: missing evidence is never a zero-fee direct policy.

Old research classes and frozen runs are unchanged. Calibration is supplied by
the caller; this module neither calls a model nor invents success probabilities.
"""
from copy import deepcopy
from decimal import localcontext
import hashlib
from itertools import product
from pathlib import Path

from .. import routing
from .backup_runtime import backup_classifier_contract_hash, backup_pool_contract_hash
from .catalog import digest
from .qcs import COMPLETE_PATH, HORIZON, _d, _interval, _provenance
from .workflow_research import ENVELOPE, SequentialResearchCore
from ..recovery_prompts import descriptor as recovery_descriptor

VERSION = 'sequential-evidence-selectors-v1'


class CalibrationRequired(RuntimeError):
    """A preparation stop, not an inference that classification is unprofitable."""
    control_plane_error = True

    def __init__(self, report):
        self.report = deepcopy(report)
        super().__init__('Selector calibration required: ' + ','.join(report['missing_stages']))


def required_contracts(plan, decision):
    """Explicit configuration combinations, not a demand for 50 tests per model."""
    rows = {}
    for stage, tiers in routing.STAGES.items():
        for previous_tier in (('open_source',) if stage == 'low_mid' else ('low', 'mid')):
            for previous, lower, upper in product(plan['pools'][previous_tier],
                    plan['pools'][tiers[0]], plan['pools'][tiers[1]]):
                pair = {tiers[0]: lower['entry']['profile'], tiers[1]: upper['entry']['profile']}
                key = backup_classifier_contract_hash(pair, decision['version'], stage,
                    previous_tier, previous['entry']['profile'])
                rows[key] = {'stage': stage, 'previous_tier': previous_tier,
                    'previous_config_id': previous['entry']['config_id'],
                    'candidate_ids': [lower['entry']['config_id'], upper['entry']['config_id']]}
    return rows


def calibration_readiness(plan, decision):
    required = required_contracts(plan, decision)
    missing = []
    for key, row in required.items():
        variant = plan['qcs_policy'].get('stages', {}).get(row['stage'], {}).get('variants', {}).get(key)
        if variant is None:
            missing.append({'contract_sha256': key, **row, 'reason': 'exact_calibration_missing'})
    return {'ready': not missing, 'required_contracts': len(required),
        'missing_stages': sorted({r['stage'] for r in missing}), 'missing': missing,
        'single_model_perfect_score_required': False,
        'missing_calibration_is_not_economic_rejection': True}


def selector_flow_contract_hash(plan):
    return digest({'backup_plan': plan, 'research_version': VERSION,
        'envelope': ENVELOPE, 'recovery_prompt': recovery_descriptor()})


def validate_selector_calibration(plan, decision, *, data_kind, now=None):
    """Validate supplied data without turning historical evaluation into training.

    Offline fixtures deliberately cannot certify a live comparison. Observed
    records need hash-bound raw sources; operator provenance is not a supplier
    guarantee or proof of a universal success rate.
    """
    routing._reject_private(plan['qcs_policy'])
    report = calibration_readiness(plan, decision)
    if not report['ready']:
        raise CalibrationRequired(report)
    if data_kind not in ('observed_calibration', 'offline_fixture'):
        raise ValueError('Explicit calibration data kind required')
    contracts = required_contracts(plan, decision)
    rows = {r['entry']['config_id']: r for pool in plan['pools'].values() for r in pool}
    for key, needed in contracts.items():
        variant = plan['qcs_policy']['stages'][needed['stage']]['variants'][key]
        calibration = variant['calibration']
        _provenance(calibration, plan['qcs_policy']['workload_id'], now)
        if (calibration.get('dataset_partition') != 'independent_calibration'
                or calibration.get('data_kind') != data_kind
                or calibration.get('uses_evaluation_answers') is not False
                or type(calibration.get('sample_count')) is not int
                or calibration['sample_count'] < 1):
            raise ValueError('Independent calibration partition and sample evidence required')
        previous = rows[needed['previous_config_id']]
        if (calibration.get('contract_sha256') != key
                or calibration.get('selector_policy_version') != VERSION
                or calibration.get('execution_contract_sha256') != digest(plan['qcs_policy']['execution_contract'])
                or calibration.get('pool_contract_sha256') != backup_pool_contract_hash(plan)
                or calibration.get('previous_configuration_sha256') != previous['configuration_sha256']
                or calibration.get('previous_tier') != needed['previous_tier']
                or calibration.get('classifier_version') != decision['version']
                or calibration.get('category_id') != plan['category_id']
                or calibration.get('stage') != needed['stage']
                or calibration.get('context') != 'public_failure_or_unknown'
                or calibration.get('horizon') != HORIZON
                or set(calibration.get('includes', [])) != COMPLETE_PATH
                or calibration.get('effective_probability_includes_fallback') is not True):
            raise ValueError('Selector calibration exact contract mismatch')
        if data_kind == 'observed_calibration':
            sources = calibration.get('source_files', {})
            if not sources or calibration['raw_sha256'] not in sources.values():
                raise ValueError('Hash-bound observed calibration sources required')
            for name, expected in sources.items():
                path = Path(name)
                if (not path.is_absolute() or path.is_symlink()
                        or any(p in ('private', 'authoring') or p.startswith('.env') for p in path.parts)
                        or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                    raise ValueError('Observed calibration source changed or is not public')
        if _interval(variant['lower_probability'])[1] > 1:
            raise ValueError('Calibrated probability out of range')
        _interval(variant['classifier_cost_usd'])
        _d(variant['classify_flow_ms_upper'])
        tiers = routing.STAGES[needed['stage']]
        if set(variant['paths']) != set(tiers) or set(variant['classified_paths']) != set(tiers):
            raise ValueError('Both complete continuation paths required')
        for tier, config_id in zip(tiers, needed['candidate_ids'], strict=True):
            evidence = rows[config_id]['evidence']
            remaining = evidence.get('remaining_cost_by_stage', {})
            minimum = (remaining[needed['stage']] if needed['stage'] in remaining
                else evidence['complete_cost_usd'])
            if (_interval(variant['paths'][tier]['cost_usd'])[1] < _interval(minimum)[1]
                    or _interval(variant['classified_paths'][tier]['cost_usd'])[1]
                        < _interval(evidence['generation_cost_usd'])[1]):
                raise ValueError('Calibration cost path omits required generation/continuation')
            if _d(variant['paths'][tier]['flow_ms_upper']) > _d(plan['qcs_policy']['max_flow_ms']):
                raise ValueError('Calibrated direct continuation exceeds the execution window')
    return report


class EvidenceSelectors:
    """Shared by the qualified core and the explicitly provisional SQL adapter."""
    def _economic(self, stage, previous, pair):
        if len(pair) == 1:
            return {'stage': stage, 'action': 'direct', 'selected_tier': next(iter(pair)),
                'reason': 'only_one_available_candidate', 'economic_rejection': False}
        profiles = {tier: row['entry']['profile'] for tier, row in pair.items()}
        key = backup_classifier_contract_hash(profiles, self.decision['version'], stage,
            previous['tier'], previous['profile'])
        policy = self._backup['qcs_policy']
        variant = policy.get('stages', {}).get(stage, {}).get('variants', {}).get(key)
        if variant is None:
            raise CalibrationRequired({'missing_stages': [stage], 'contract_sha256': key,
                'reason': 'exact_classifier_calibration_missing'})
        # Supplied variants have already passed the exact binding/provenance
        # validator. Use both interval ends: lack of proven profit is not proof
        # of loss. A straddling interval requires more calibration.
        paths = variant['paths']
        direct = min(pair, key=lambda t: (_interval(paths[t]['cost_usd'])[1],
            _d(paths[t]['flow_ms_upper']), t))
        lower, upper = routing.STAGES[stage]
        with localcontext() as context:
            context.prec = 512
            p0, p1 = _interval(variant['lower_probability'])
            f0, f1 = _interval(variant['classifier_cost_usd'])
            l0, l1 = _interval(variant['classified_paths'][lower]['cost_usd'])
            u0, u1 = _interval(variant['classified_paths'][upper]['cost_usd'])
            expected0 = f0 + min(p*l0 + (1-p)*u0 for p in (p0, p1))
            expected1 = f1 + max(p*l1 + (1-p)*u1 for p in (p0, p1))
            d0, d1 = _interval(paths[direct]['cost_usd'])
            saving0, saving1 = d0-expected1, d1-expected0
            margin = max(_d(policy.get('minimum_saving_usd', '0')),
                d0*_d(policy.get('minimum_saving_fraction', '0')))
            result = {'stage': stage, 'contract_sha256': key,
                'net_saving_usd': {'lower': str(saving0), 'upper': str(saving1)},
                'required_saving_usd': str(margin)}
            if saving1 <= margin:
                return {**result, 'action': 'direct', 'selected_tier': direct,
                    'reason': 'classification_proven_not_economic', 'economic_rejection': True}
            if saving0 <= margin:
                raise CalibrationRequired({'missing_stages': [stage], **result,
                    'reason': 'classification_economics_uncertain'})
            if _d(variant['classify_flow_ms_upper']) > _d(policy['max_flow_ms']):
                return {**result, 'action': 'direct', 'selected_tier': direct,
                    'reason': 'classification_exceeds_speed_limit', 'economic_rejection': False}
            return {**result, 'action': 'classify', 'selected_tier': None,
                'reason': 'conservative_net_saving_positive', 'economic_rejection': False}


class SelectorResearchCore(EvidenceSelectors, SequentialResearchCore):
    _research_version = VERSION

    def __init__(self, client, backup_plan, decision_profile, **kwargs):
        policy = kwargs['research_policy']
        validate_selector_calibration(backup_plan, decision_profile,
            data_kind=policy.get('calibration_data_kind'))
        if policy['calibration_data_kind'] == 'offline_fixture':
            require_mock_transport(client)
        super().__init__(client, backup_plan, decision_profile, **kwargs)
        # Both direct construction and the convenience factory fail before a
        # first paid answer if any reachable selector cannot yet be assessed.
        self.budget_reservation_plan('cloudyfan')

    def _validate_research_policy(self, now):
        policy = self._research_policy
        if policy.get('calibration_data_kind') == 'offline_fixture':
            require_mock_transport(self.client)
        if (policy.get('schema_version') != VERSION or policy.get('envelope') != ENVELOPE
                or policy.get('recovery_prompt') != recovery_descriptor()):
            raise ValueError('Explicit new selector research policy required')
        flow, base = policy.get('whole_flow', {}), self._backup['qcs_policy']
        if (flow.get('contract_sha256') != selector_flow_contract_hash(self._backup)
                or flow.get('metric') != 'whole_task_flow_ms'
                or flow.get('statistic') != base['speed_statistic']
                or _d(flow['upper']) > _d(base['max_flow_ms'])):
            raise ValueError('New selector whole-flow contract mismatch')
        _provenance(flow.get('provenance'), base['workload_id'], now)
        if flow['provenance'].get('category_id') != self._backup['category_id']:
            raise ValueError('Research whole-flow evidence category mismatch')
        validate_selector_calibration(self._backup, self.decision,
            data_kind=policy.get('calibration_data_kind'), now=now)

    def configuration_descriptor(self):
        result = super().configuration_descriptor()
        result.update(routing_version=VERSION, selector_policy='evidence-required-not-fixed-direct')
        return result


def build_selector_research_core(client, backup_plan, decision_profile, **kwargs):
    """Preparation checks precede even the open-source first paid request."""
    return SelectorResearchCore(client, backup_plan, decision_profile, **kwargs)


def require_mock_transport(client):
    import httpx
    from ..client import Client
    http = getattr(client, 'http', None)
    transports = [getattr(http, '_transport', None)]
    transports.extend(v for v in getattr(http, '_mounts', {}).values() if v is not None)
    if (type(client) is not Client or type(http) is not httpx.Client
            or not all(isinstance(t, httpx.MockTransport) for t in transports)):
        raise ValueError('Offline calibration fixtures require MockTransport on every route')
