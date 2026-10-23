"""New SQL research adapter; no credentials, transport creation or HTTP here.

Old direct-only artifacts are read-only inputs. Independent exact selector
calibration is mandatory before even the open-source first model dispatch.
Unknown model SQL quality/speed remains provisional, never formal qualification.
"""

from copy import deepcopy
from types import SimpleNamespace

import httpx
from cloudyfan_jev.client import Client
from cloudyfan_jev.recovery_prompts import descriptor as recovery_descriptor
from cloudyfan_jev.scores.catalog import digest, timestamp
from cloudyfan_jev.scores.qcs import _d
from cloudyfan_jev.scores.selector_research import (
    VERSION as SELECTOR_VERSION,
)
from cloudyfan_jev.scores.selector_research import (
    CalibrationRequired,
    EvidenceSelectors,
    calibration_readiness,
    required_contracts,
    validate_selector_calibration,
)
from cloudyfan_jev.scores.workflow_research import ENVELOPE

from ._sql_research_support import (
    _ProvisionalSQLSupport as ResearchProvisionalCore, pool_contract, ZERO_SCENARIO,
)
VERSION = 'sql-evidence-selectors-research-v1'
PURPOSES = {'offline_rehearsal', 'live_preparation'}


def _transport_guard(client, *, data_kind, purpose):
    if purpose not in PURPOSES:
        raise ValueError('Explicit offline rehearsal or live preparation purpose required')
    if purpose == 'live_preparation' and data_kind != 'observed_calibration':
        raise ValueError('Live preparation cannot use offline calibration fixtures')
    if purpose == 'offline_rehearsal' or data_kind == 'offline_fixture':
        # Inspect the actual injected sync transport, including URL mounts.
        # A custom Client.call override is not verified by a mock http field.
        http = getattr(client, 'http', None)
        if (
            type(client) is not Client
            or type(http) is not httpx.Client
            or not isinstance(http._transport, httpx.MockTransport)
            or any(t is not None and not isinstance(t, httpx.MockTransport)
                   for t in http._mounts.values())
        ):
            raise ValueError('Offline SQL selector rehearsal requires exclusive MockTransport')


def research_policy(plan, *, data_kind):
    """A new declaration, not an invented calibration, price or authority."""
    return {
        'schema_version': VERSION,
        'selector_version': SELECTOR_VERSION,
        'provisional_only': True,
        'backup_contract_sha256': pool_contract(plan),
        'selector_policy_sha256': digest(plan['qcs_policy']),
        'conditional_calibration': 'required_exact_independent_variants',
        'data_kind': data_kind,
        'operational_deadline_not_latency_prediction': True,
        'envelope': deepcopy(ENVELOPE),
        'zero_first_model_fee_scenario': deepcopy(ZERO_SCENARIO),
        'recovery_prompt': recovery_descriptor(),
        'formal_qualification': 'pending',
    }


def preflight(plan, decision, *, data_kind, now=None):
    """Pure preparation: preserve an explicit missing-data stop, never dispatch."""
    if data_kind not in ('observed_calibration', 'offline_fixture'):
        raise ValueError('Explicit observed or synthetic calibration kind required')
    report = calibration_readiness(plan, decision)
    economic = {}
    if report['ready']:
        validate_selector_calibration(plan, decision, data_kind=data_kind, now=now)
        if data_kind == 'observed_calibration':
            _execution_binding(plan)
        rows = {r['entry']['config_id']: r for pool in plan['pools'].values() for r in pool}
        probe = SimpleNamespace(_backup=plan, decision=decision)
        for key, needed in required_contracts(plan, decision).items():
            tiers = ('low', 'mid') if needed['stage'] == 'low_mid' else ('mid', 'high')
            pair = dict(zip(tiers, (rows[cid] for cid in needed['candidate_ids']), strict=True))
            previous = {'tier': needed['previous_tier'],
                        'profile': rows[needed['previous_config_id']]['entry']['profile']}
            try:
                economic[key] = EvidenceSelectors._economic(probe, needed['stage'], previous, pair)
            except CalibrationRequired as error:
                report = {**report, **error.report, 'ready': False}
                break
    return {
        **report,
        'stop': None if report['ready'] else 'calibration_required',
        'data_kind': data_kind,
        'formal_qualification': 'pending',
        'unknown_model_quality': True,
        'unknown_model_speed': True,
        'dispatch_authorized': False,
        'model_requests': 0,
        'economic_by_contract': economic,
    }


def _execution_binding(plan):
    value = plan['qcs_policy'].get('execution_contract')
    if (
        not isinstance(value, dict)
        or not isinstance(value.get('core'), dict)
        or not isinstance(value.get('validation_contract'), str)
        or not value['validation_contract']
    ):
        raise ValueError('Live SQL selector preparation requires complete execution binding')


def prepare_template(old_template, *, data_kind, qcs_policy=None, now=None):
    """Return a separate versioned template. Never overwrite old input files.

    qcs_policy must be supplied from independently prepared observations. When
    absent, old empty variants remain empty and preflight reports not ready.
    """
    plan = deepcopy(old_template['backup_plan'])
    if qcs_policy is not None:
        plan['qcs_policy'] = deepcopy(qcs_policy)
    plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
    decision = deepcopy(old_template['decision_profile'])
    return {
        'schema_version': VERSION,
        'source_template_sha256': digest(old_template),
        'backup_plan': plan,
        'decision_profile': decision,
        'research_policy': research_policy(plan, data_kind=data_kind),
        'preflight': preflight(plan, decision, data_kind=data_kind, now=now),
        'required_factory': 'build_calibrated_sql_research_core',
        'dispatch_authorized': False,
    }


class CalibratedSQLResearchCore(EvidenceSelectors, ResearchProvisionalCore):
    """Actual sequential SQL Core, retaining three answers/two selectors."""

    _research_version = VERSION

    def __init__(self, client, backup_plan, decision_profile, *, data_kind,
                 purpose='live_preparation', **kwargs):
        _transport_guard(client, data_kind=data_kind, purpose=purpose)
        validate_selector_calibration(backup_plan, decision_profile, data_kind=data_kind)
        if purpose == 'live_preparation':
            _execution_binding(backup_plan)
        self._selector_data_kind, self._selector_purpose = data_kind, purpose
        super().__init__(client, backup_plan, decision_profile, **kwargs)
        self._validate_policy(self._now().isoformat())
        # Economic uncertainty in ANY reachable exact variant is a preparation
        # stop before the shared-budget layer or initial model can dispatch.
        self.budget_reservation_plan('cloudyfan')

    def _validate_policy(self, now):
        policy = self._backup['qcs_policy']
        if (
            policy.get('schema_version') != 'qcs-backup-policy-v1'
            or not policy.get('workload_id')
            or policy.get('health_policy') != self.health_store.policy
            or policy.get('classifier_version') != self.decision['version']
            or _d(policy['max_flow_ms']) <= 0
            or _d(policy.get('minimum_saving_fraction', '0')) > 1
        ):
            raise ValueError('Explicit calibrated SQL research limits and version required')
        _d(policy.get('minimum_saving_usd', '0'))
        required = required_contracts(self._backup, self.decision)
        actual = {
            k for row in policy.get('stages', {}).values()
            for k in row.get('variants', {})
        }
        if actual != set(required):
            # Missing evidence has its own control-plane exception.
            report = calibration_readiness(self._backup, self.decision)
            if not report['ready']:
                raise CalibrationRequired(report)
            raise ValueError('Unreachable selector calibration variant supplied')
        validate_selector_calibration(
            self._backup, self.decision, data_kind=self._selector_data_kind, now=now
        )
        for stage in policy['stages'].values():
            for variant in stage['variants'].values():
                if any(_d(path['flow_ms_upper']) > _d(policy['max_flow_ms'])
                       for path in variant['paths'].values()):
                    raise ValueError('Direct calibrated path exceeds operational limit')
        if now is not None and timestamp(now) >= timestamp(
            self._backup['provisional_policy']['valid_until']
        ):
            raise ValueError('Provisional SQL research evidence window expired')

    def _validate_research_policy(self, now):
        _transport_guard(
            self.client, data_kind=self._selector_data_kind, purpose=self._selector_purpose
        )
        if self._research_policy != research_policy(
            self._backup, data_kind=self._selector_data_kind
        ):
            raise ValueError('New exact SQL selector research policy required')
        self._validate_policy(now)

    def _require_selection_fresh(self):
        # The old adapter never classified, so inherited strict health logic
        # rejected its deliberately unknown-health research probes. Keep known
        # bad/stale endpoints blocked while not fabricating a positive result.
        self._require_policy_fresh()
        for record in self._active_selection['pair'].values():
            self._require_candidate_fresh(record)
            profile = record['entry']['profile']
            health = self.health_store.assess(record['configuration_sha256'], profile['provider'])
            if self._health_reasons(health):
                raise RuntimeError('Classifier candidate has known bad or stale research health')

    def configuration_descriptor(self):
        result = super().configuration_descriptor()
        result.pop('direct_selection', None)
        result.update(
            routing_version=VERSION,
            selector_policy='evidence-required-not-fixed-direct',
            selector_version=SELECTOR_VERSION,
            calibration_data_kind=self._selector_data_kind,
            evidence_mode='provisional_research_not_qualified',
            exact_SQL_quality_guaranteed=False,
            speed_SLA_guaranteed=False,
        )
        return result

    def _with_costs(self, result):
        result = super()._with_costs(result)
        result['research_policy_version'] = VERSION
        result['selector_policy'] = 'evidence-required-not-fixed-direct'
        result['calibration_data_kind'] = self._selector_data_kind
        result['formal_qualification'] = 'pending'
        return result


def build_calibrated_sql_research_core(client, backup_plan, decision_profile, **kwargs):
    """Dependency injection only; no key loading, HTTP or paid authority here."""
    return CalibratedSQLResearchCore(client, backup_plan, decision_profile, **kwargs)
