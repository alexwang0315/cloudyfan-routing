"""Category-local candidate preparation, separate from executable QCS plans.

Three entries mean three distinct models, not three endpoints of one model.
This module never approves a configuration, calibrates a changed profile, or
claims that different providers have independent physical infrastructure.
"""
from copy import deepcopy

from ..routing import _reject_private
from .catalog import _catalog_eligible, _entry, _positive, digest, timestamp, verify_snapshot
from .categories import CATEGORY_SPECS, resolve_category, verify_category_matrix
from .configuration import configuration_digest
from .qcs import _candidate, _d, _interval

VERSION = 'cloudyfan-category-backups-v1'
TIERS = ('open_source', 'low', 'mid', 'high')
_INVALID = (ValueError, KeyError, TypeError, AttributeError, OverflowError)


def _constraints(category, requirements, policy, target_size):
    if type(target_size) is not int or not 1 <= target_size <= 3:
        raise ValueError('Backup target must be an integer between one and three')
    if (not isinstance(requirements, dict)
            or set(requirements) - {'domain', 'capabilities', 'context_tokens', 'allowed_providers'}
            or requirements.get('domain') != category):
        raise ValueError('Explicit category-local public requirements required')
    required = requirements.get('capabilities')
    if not isinstance(required, list) or any(not isinstance(x, str) or not x for x in required):
        raise ValueError('Required capabilities must be a list of strings')
    _positive(requirements['context_tokens'])
    providers = requirements.get('allowed_providers')
    if providers is not None and (not isinstance(providers, list) or not providers
                                 or any(not isinstance(x, str) or not x for x in providers)):
        raise ValueError('Explicit nonempty provider restriction required')
    _reject_private(policy)
    if (not isinstance(policy, dict)
            or policy.get('schema_version') not in ('qcs-policy-v1', 'qcs-backup-policy-v1')
            or policy.get('category_id') != category
            or not isinstance(policy.get('workload_id'), str) or not policy['workload_id'].strip()
            or policy.get('speed_statistic') not in ('mean', 'p50', 'p95', 'p99')):
        raise ValueError('Explicit category-bound QCS admission policy required')
    _d(policy['max_flow_ms'])
    _d(policy.get('max_source_age_hours', '24'))


def _admit(entry, row, snapshot, category, requirements, policy, now):
    _entry(entry)
    if (entry['profile']['capability_card'].get('category_id') != category
            or not set(requirements['capabilities']) <= set(entry['capabilities'])
            or requirements['context_tokens'] > entry['context_tokens']
            or (requirements.get('allowed_providers') is not None
                and entry['profile']['provider'] not in requirements['allowed_providers'])):
        raise ValueError('capability_context_or_provider_mismatch')
    # Even an intact, rehashed snapshot cannot turn pending entries into approved
    # ones. None-time replay checks at preparation time, without expiring a run.
    instant = timestamp(now if now is not None else snapshot['created_at'])
    reason = _catalog_eligible(entry, snapshot['policy'], instant)
    if reason:
        raise ValueError(reason)
    _reject_private(row)
    _candidate(entry, row, policy, now)
    if any(cap.startswith('sql') for cap in requirements['capabilities']):
        reason = _catalog_eligible(entry, {**snapshot['policy'], 'score_role': 'family_effort_prior'}, instant)
        if reason:
            raise ValueError(reason)


def _identity(entry):
    profile = entry['profile']
    # Exact aliases only: no fuzzy name matching or invented variant equivalence.
    return {value for source in (entry, profile)
            for key in ('model', 'model_version', 'canonical_model_id', 'canonical_slug')
            if isinstance(value := source.get(key), str) and value}


def _family(entry):
    value = entry.get('model_family', entry['profile'].get('model_family'))
    return value if isinstance(value, str) and value else None


def _rank(record):
    return (_interval(record['evidence']['complete_cost_usd'])[1],
            _d(record['evidence']['speed']['upper']), record['entry']['config_id'])


def _choose(records, tier, target_size, excluded):
    unique, seen = [], set()
    for record in sorted(records, key=_rank):
        identities = _identity(record['entry'])
        duplicate = bool(seen & identities)
        seen.update(identities)
        if duplicate:
            excluded.append({'tier': tier, 'config_id': record['entry']['config_id'],
                             'reason': 'duplicate_model_identity'})
        else:
            unique.append(record)
    selected = []
    while unique and len(selected) < target_size:
        providers = {r['entry']['profile']['provider'] for r in selected}
        families = {_family(r['entry']) for r in selected} - {None}

        def order(record):
            entry = record['entry']
            provider = entry['profile']['provider'] not in providers
            family = _family(entry) is not None and _family(entry) not in families
            return (-(provider and family), -provider, -family, *_rank(record)) if selected else _rank(record)

        winner = min(unique, key=order)
        unique.remove(winner)
        selected.append(winner)
    excluded.extend({'tier': tier, 'config_id': record['entry']['config_id'],
                     'reason': 'qualified_beyond_target'} for record in unique)
    return selected


def prepare_backup_pools(matrix, category, requirements, evidence, qcs_policy, *, now, target_size=3):
    """Freeze up to three admitted models per tier; never create runtime plans.

    The first model minimizes complete cost, flow speed and stable ID. Backups
    prefer explicit provider/family diversity, then the same cost/speed order.
    Every concrete runtime profile combination still needs its own full QCS
    continuation calibration. A shortage is data, not permission to weaken gates.
    """
    category = resolve_category(category)
    verify_category_matrix(matrix)
    if now is not None:
        timestamp(now)
    _constraints(category, requirements, qcs_policy, target_size)
    if not isinstance(evidence, dict):
        raise ValueError('Explicit configuration-keyed evidence required')
    view = matrix['categories'][category]
    snapshot = view['snapshot']
    pools = {tier: [] for tier in TIERS}
    excluded = deepcopy(view['excluded'])
    if snapshot is not None:
        verify_snapshot(snapshot, now=now)
        for tier in TIERS:
            good = []
            ids = [e.get('config_id') for e in view['pools'][tier] if isinstance(e, dict)]
            for entry in view['pools'][tier]:
                config_id = entry.get('config_id') if isinstance(entry, dict) else None
                try:
                    if ids.count(config_id) != 1:
                        raise ValueError('duplicate_configuration_id')
                    row = evidence[config_id]
                    _admit(entry, row, snapshot, category, requirements, qcs_policy, now)
                    good.append({'entry': deepcopy(entry), 'evidence': deepcopy(row),
                                 'configuration_sha256': configuration_digest(entry)})
                except _INVALID as error:
                    excluded.append({'tier': tier, 'config_id': config_id,
                                     'reason': str(error) if isinstance(error, ValueError)
                                     else 'missing_or_invalid_dimensions'})
            pools[tier] = _choose(good, tier, target_size, excluded)
    counts = {tier: len(pools[tier]) for tier in TIERS}
    body = {'schema_version': VERSION, 'category_id': category, 'matrix_id': matrix['matrix_id'],
            'snapshot_id': snapshot['snapshot_id'] if snapshot else None, 'snapshot': deepcopy(snapshot),
            'prepared_at': now if now is not None else matrix['created_at'],
            'requirements': deepcopy(requirements), 'qcs_policy': deepcopy(qcs_policy),
            'pools': pools, 'excluded': excluded,
            'shortages': {tier: target_size - counts[tier] for tier in TIERS},
            'coverage': {'target_size': target_size, 'counts': counts,
                         'complete': all(count == target_size for count in counts.values())},
            'runtime_variant_calibration_required': True,
            'physical_failure_independence_proven': False}
    return {**body, 'backup_id': digest(body)}


def verify_backup_pools(plan, *, now=None):
    """Check frozen content and candidate admission; no external authentication."""
    if (not isinstance(plan, dict) or plan.get('schema_version') != VERSION
            or plan.get('backup_id') != digest({k: v for k, v in plan.items() if k != 'backup_id'})):
        raise ValueError('Backup pool integrity mismatch')
    category = resolve_category(plan['category_id'])
    timestamp(plan['prepared_at'])
    target = plan['coverage']['target_size']
    _constraints(category, plan['requirements'], plan['qcs_policy'], target)
    snapshot = plan['snapshot']
    if snapshot is not None:
        verify_snapshot(snapshot, now=now)
        if (snapshot['snapshot_id'] != plan['snapshot_id'] or snapshot.get('category_id') != category
                or snapshot['policy']['domain'] != category):
            raise ValueError('Backup snapshot scope mismatch')
    elif plan['snapshot_id'] is not None:
        raise ValueError('Backup snapshot missing')
    if set(plan['pools']) != set(TIERS):
        raise ValueError('Backup pools must preserve four tiers')
    for tier, pool in plan['pools'].items():
        if not isinstance(pool, list) or len(pool) > target:
            raise ValueError('Invalid backup pool size')
        seen = set()
        for record in pool:
            entry = record['entry']
            source = (snapshot.get('open_source_pool', []) if tier == 'open_source'
                      else snapshot['pools'][tier]) if snapshot else []
            if entry not in source or record['configuration_sha256'] != configuration_digest(entry):
                raise ValueError('Backup candidate not bound to snapshot')
            if seen & _identity(entry):
                raise ValueError('Duplicate model identity in backup pool')
            seen.update(_identity(entry))
            _admit(entry, record['evidence'], snapshot, category, plan['requirements'],
                   plan['qcs_policy'], now if now is not None else plan['prepared_at'])
        count = len(pool)
        if (plan['coverage']['counts'][tier] != count or plan['shortages'][tier] != target - count):
            raise ValueError('Backup coverage mismatch')
    if (plan['coverage']['complete'] != all(len(pool) == target for pool in plan['pools'].values())
            or plan.get('runtime_variant_calibration_required') is not True
            or plan.get('physical_failure_independence_proven') is not False):
        raise ValueError('Backup preparation boundary mismatch')


def backup_coverage_report(matrix, requirements, evidence, qcs_policies, *, now, target_size=3):
    """Report all 23 classes, retaining absent data as zero qualified candidates.

    Inputs are category-keyed; a global coding workload cannot supply another
    category. Invalid or absent category inputs cannot hide the other reports.
    """
    verify_category_matrix(matrix)
    timestamp(now)
    if type(target_size) is not int or not 1 <= target_size <= 3:
        raise ValueError('Backup target must be an integer between one and three')
    if any(not isinstance(value, dict) for value in (requirements, evidence, qcs_policies)):
        raise ValueError('Category-keyed report inputs required')
    reports = {}
    for category in CATEGORY_SPECS:
        try:
            plan = prepare_backup_pools(matrix, category, requirements[category], evidence[category],
                                        qcs_policies[category], now=now, target_size=target_size)
            reports[category] = {'status': 'complete' if plan['coverage']['complete'] else 'shortage',
                                 'plan': plan, 'coverage': plan['coverage'], 'shortages': plan['shortages']}
        except _INVALID as error:
            reports[category] = {'status': 'unavailable', 'plan': None,
                'reason': str(error) if isinstance(error, ValueError) else 'missing_category_inputs',
                'coverage': {'target_size': target_size, 'counts': dict.fromkeys(TIERS, 0), 'complete': False},
                'shortages': dict.fromkeys(TIERS, target_size)}
    body = {'schema_version': 'cloudyfan-category-backup-coverage-v1', 'matrix_id': matrix['matrix_id'],
            'prepared_at': now, 'categories': reports, 'candidate_proposals_are_qualified': False,
            'runtime_enabled': False}
    return {**body, 'report_id': digest(body)}
