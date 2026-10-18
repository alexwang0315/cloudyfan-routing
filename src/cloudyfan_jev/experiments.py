"""Offline experiment preflight; a reviewed plan is never spending authorization."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from .diagnostics import DiagnosticContract
from .engine import Limits

from .routing import _reject_private, VERSION as ROUTING_VERSION
from .scores.catalog import digest, select_profiles, verify_snapshot, timestamp
from .scores.qualification import qualified_snapshot

SCHEMA = 'cloudyfan-experiment-plan-v1'
ARMS = ('fixed_high', 'low_first', 'open_source_first', 'cloudyfan', 'open_source_fixed_low')


def build_plan(spec, qualification_pack, review, *, now):
    spec, review = deepcopy(spec), deepcopy(review)
    if type(spec.get('synthetic')) is not bool:
        raise ValueError('synthetic must be an explicit boolean')
    instant = timestamp(now)
    blockers = []

    def approved(record):
        try:
            return (record['status'] == 'approved' and isinstance(record['reviewer'], str)
                    and bool(record['reviewer'].strip()) and bool(record['evidence'])
                    and isinstance(record['evidence'], list)
                    and all(isinstance(e, str) and e.strip() for e in record['evidence'])
                    and timestamp(record['reviewed_at']) <= instant)
        except (KeyError, TypeError, ValueError):
            return False

    binding = {'spec': spec, 'qualification_pack_id': qualification_pack['pack_id']}
    if review.get('input_sha256') != digest(binding):
        blockers.append('review:input_binding_mismatch')
    for area in ('plan', 'isolation', 'grading', 'decision_profile'):
        if not approved(review.get(area, {})):
            blockers.append(area + ':review_pending')
    all_tasks = spec['tasks'] + spec['calibration_tasks']
    if not spec['tasks'] or not spec['calibration_tasks']:
        blockers.append('tasks:missing_cohort')
    if len({row['task_id'] for row in all_tasks}) != len(all_tasks):
        blockers.append('tasks:duplicate_ids')
    holdout = {digest(row['task']) for row in spec['tasks']}
    calibration = {digest(row['task']) for row in spec['calibration_tasks']}
    if holdout & calibration:
        blockers.append('isolation:overlapping_inputs')
    owners = [spec.get(k) for k in ('design_owner', 'calibration_owner', 'grader_owner')]
    if any(not isinstance(v, str) or not v.strip() for v in owners) or owners[2] in owners[:2]:
        blockers.append('grading:not_independent')
    for row in all_tasks:
        _reject_private(row['task'])
        if row['input_sha256'] != digest(row['task']):
            blockers.append('task:' + row['task_id'] + ':input_hash_mismatch')
        if not isinstance(row.get('stratum'), str) or not row['stratum'].strip():
            blockers.append('task:' + row['task_id'] + ':stratum_missing')
        evidence = review.get('tasks', {}).get(row['task_id'], {})
        if (not approved(evidence) or evidence.get('input_sha256') != row['input_sha256']
                or evidence.get('unresolved_ambiguities') != []):
            blockers.append('task:' + row['task_id'] + ':ambiguity_review_pending')
    if (not isinstance(spec.get('arms'), list) or not spec['arms']
            or len(set(spec['arms'])) != len(spec['arms']) or set(spec['arms']) - set(ARMS)
            or 'fixed_high' not in spec['arms']):
        blockers.append('arms:unsupported_or_duplicate')
    try:
        dc = deepcopy(spec['diagnostic_contract'])
        dc['allowed_fields'] = tuple(dc['allowed_fields'])
        DiagnosticContract(**dc)
        Limits(**spec['limits'])
        decision = spec['decision_profile']
        if (spec['max_generation_attempts'] != 2
                or type(spec['max_generation_attempts']) is not int
                or spec['repair_policy'] != 'one-public-diagnostic-repair-v1'
                or not isinstance(spec['system'], str) or not spec['system'].strip()
                or not isinstance(spec['contract_version'], str) or not spec['contract_version'].strip()
                or decision.get('provider') != 'typesafe'
                or any(not isinstance(decision.get(k), str) or not decision[k].strip()
                       for k in ('model', 'version', 'provider_name'))):
            raise ValueError('Unsupported execution contract')
        _reject_private(decision)
        quality = Decimal(spec['quality_gate']['required_final_pass_rate'])
        if not quality.is_finite() or not 0 < quality <= 1:
            raise ValueError('Invalid quality gate')
    except (KeyError, TypeError, ValueError, InvalidOperation):
        blockers.append('execution:invalid_contract')
    try:
        budget = spec['budget']
        if not isinstance(budget['cap_usd'], str) or not isinstance(budget['next_block_upper_usd'], str):
            raise ValueError('Budget amounts must be decimal strings')
        cap, block = Decimal(budget['cap_usd']), Decimal(budget['next_block_upper_usd'])
        if (not cap.is_finite() or not block.is_finite() or not 0 < block <= cap
                or timestamp(budget['expires_at']) <= instant
                or any(not isinstance(budget.get(k), str) or not budget[k].strip()
                       for k in ('authorization_id', 'enforcement_evidence'))):
            raise ValueError('Budget not bounded')
    except (KeyError, TypeError, ValueError, InvalidOperation):
        blockers.append('budget:invalid_or_expired')
    snapshot, profiles, baseline = None, None, None
    try:
        snapshot = qualified_snapshot(qualification_pack, now=now, allow_synthetic=spec['synthetic'])
        profiles = select_profiles(snapshot, spec['requirements'], now=now)
        requirements = spec['requirements']
        eligible = [row for rows in snapshot['pools'].values() for row in rows
                    if set(requirements['capabilities']) <= set(row['capabilities'])
                    and row['context_tokens'] >= requirements['context_tokens']
                    and (requirements.get('allowed_providers') is None
                         or row['profile']['provider'] in requirements['allowed_providers'])]
        baseline = next((row for row in eligible if row['config_id'] == spec['baseline_config_id']), None)
        if not baseline or baseline['evidence']['score'] != max(row['evidence']['score'] for row in eligible):
            blockers.append('baseline:not_highest_qualified_score')
    except (ValueError, TypeError, KeyError):
        blockers.append('qualification:unavailable')
    body = {'schema_version': SCHEMA, 'created_at': now, 'synthetic': spec['synthetic'],
            'spec': spec, 'review': review, 'qualification_pack_id': qualification_pack['pack_id'],
            'routing_version': ROUTING_VERSION,
            'snapshot_id': snapshot['snapshot_id'] if snapshot else None, 'snapshot': snapshot, 'candidates': profiles,
            'baseline': baseline, 'blockers': blockers,
            'status': 'blocked' if blockers else ('synthetic_ready' if spec['synthetic'] else 'reviewed'),
            'planned_only_arms': [a for a in spec['arms'] if a == 'open_source_fixed_low'],
            'paid_execution_authorized': False}
    return {**body, 'plan_id': digest(body)}


def verify_plan_integrity(plan):
    """Archive check only; no freshness, approval, or execution permission implied."""
    if plan.get('schema_version') != SCHEMA or plan.get('plan_id') != digest(
            {k: v for k, v in plan.items() if k != 'plan_id'}):
        raise ValueError('Experiment plan integrity mismatch')


def verify_plan(plan, *, now, allow_synthetic=False):
    verify_plan_integrity(plan)
    if plan['synthetic'] and not allow_synthetic:
        raise ValueError('Synthetic plan cannot authorize a formal experiment')
    if plan['blockers'] or plan['status'] not in ('synthetic_ready', 'reviewed'):
        raise ValueError('Experiment plan is blocked')
    if plan['routing_version'] != ROUTING_VERSION:
        raise ValueError('Frozen routing implementation version differs')
    verify_snapshot(plan['snapshot'], now=now)
    if timestamp(plan['spec']['budget']['expires_at']) <= timestamp(now):
        raise ValueError('Experiment budget authorization expired')


def execution_binding(plan, arm, task_id, *, now, allow_synthetic=False):
    """Return reviewed inputs for an existing Core mode, without executing anything.

    The attribution arm remains plan-only: this seam refuses to fabricate an
    execution mode or relabel a high tier as low. Paid callers still need the
    independent authorization, reservation and transport controls.
    """
    verify_plan(plan, now=now, allow_synthetic=allow_synthetic)
    spec = plan['spec']
    if arm not in spec['arms']:
        raise ValueError('Arm is not in the reviewed plan')
    if arm == 'open_source_fixed_low':
        raise ValueError('Attribution arm is plan-only; execution adapter is not implemented')
    row = next((row for row in spec['tasks'] if row['task_id'] == task_id), None)
    if row is None:
        raise ValueError('Task is not in the holdout plan')
    profiles = deepcopy(plan['candidates'])
    if arm != 'cloudyfan':
        profiles['high'] = deepcopy(plan['baseline']['profile'])
    return {'plan_id': plan['plan_id'], 'arm': arm, 'task_id': task_id,
            'input_sha256': row['input_sha256'], 'task': deepcopy(row['task']), 'profiles': profiles,
            **{k: deepcopy(spec[k]) for k in ('decision_profile', 'contract_version',
               'diagnostic_contract', 'limits', 'max_generation_attempts', 'repair_policy', 'budget', 'system')}}


def verify_binding(plan, binding, *, now, allow_synthetic=False):
    expected = execution_binding(plan, binding['arm'], binding['task_id'], now=now,
                                 allow_synthetic=allow_synthetic)
    if digest(binding) != digest(expected):
        raise ValueError('Execution binding changed after plan review')


def write_plan(path, plan, *, now, allow_synthetic=False):
    """Persist once with private permissions; never replace a reviewed plan."""
    from pathlib import Path
    from .client import _immutable
    verify_plan(plan, now=now, allow_synthetic=allow_synthetic)
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Plan path cannot use symlinks')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        previous = read_plan(path, now=now, allow_synthetic=allow_synthetic)
        if previous['plan_id'] != plan['plan_id']:
            raise ValueError('Frozen plan cannot be replaced')
        return
    _immutable(path, plan)


def read_plan(path, *, now, allow_synthetic=False):
    import json
    from pathlib import Path
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Plan path cannot use symlinks')
    plan = json.loads(path.read_text())
    verify_plan(plan, now=now, allow_synthetic=allow_synthetic)
    return plan
