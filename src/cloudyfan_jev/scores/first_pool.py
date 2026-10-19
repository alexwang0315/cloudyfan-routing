"""Offline first-release inputs; no source fetching, approval or model dispatch."""
from copy import deepcopy

from .catalog import digest, select_profiles, timestamp
from .configuration import configuration_digest
from .qualification import build_qualification_pack

PROFILE_SCHEMA = 'cloudyfan-first-pool-profile-v1'
REPORT_SCHEMA = 'cloudyfan-first-pool-report-v1'


def _evidence(value):
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.strip() for item in value)


def prepare_pool(profile, *, now):
    """Return a reproducible qualification pack or actionable blocking report.

    Evidence records are trusted operator inputs, never authenticated guarantees.
    This seam adds exact configuration/operation binding to the existing review
    package. It cannot create missing evidence or approve a pending statement.
    """
    timestamp(now)
    required = {'schema_version', 'synthetic', 'source_choice', 'observations', 'registry', 'policy',
                'requirements', 'decision', 'review', 'configuration_evidence'}
    if set(profile) != required or profile['schema_version'] != PROFILE_SCHEMA:
        raise ValueError('Unsupported first-pool profile')
    if type(profile['synthetic']) is not bool:
        raise ValueError('Synthetic status must be explicit boolean')
    from ..routing import _reject_private
    _reject_private(profile)
    registry = deepcopy(profile['registry'])
    admission_context = digest({k: profile[k] for k in (
        'configuration_evidence', 'source_choice', 'requirements', 'decision', 'synthetic')})
    assessments, blockers = [], []
    for entry in [registry['open_source'], *registry['candidates']]:
        config_id = entry['config_id']
        proof = profile['configuration_evidence'].get(config_id, {})
        reasons = []
        bound = proof.get('configuration_sha256') == configuration_digest(entry)
        if not bound:
            reasons.append('configuration_evidence_unbound')
        mapping = proof.get('mapping', {})
        if (not bound or mapping.get('status') != 'approved' or not _evidence(mapping.get('evidence'))
                or (entry is not registry['open_source'] and any(
                    mapping.get(k) != entry[k] for k in ('source', 'source_version', 'source_model_id')))):
            reasons.append('mapping_unproven')
        capabilities = proof.get('capabilities', {})
        if (not bound or capabilities.get('basis') not in ('independent_calibration', 'public_capability_record')
                or not _evidence(capabilities.get('evidence'))
                or not isinstance(capabilities.get('operations'), list)
                or not set(entry['capabilities']) <= set(capabilities.get('operations', []))
                or type(capabilities.get('context_tokens')) is not int
                or capabilities.get('context_tokens', 0) < entry['context_tokens']):
            reasons.append('capabilities_unproven')
        if reasons:
            entry['approved'] = False
        # Existing input-bound review must cover these NEW qualification inputs
        # too. Store only digests in the public card; do not project arbitrary
        # operator evidence fields into classification requests.
        entry['profile']['capability_card']['first_pool_qualification'] = {
            'configuration_evidence_sha256': digest(proof), 'admission_context_sha256': admission_context}
        scores = [deepcopy(row) for row in profile['observations'] if all(
            row.get(k) == entry.get(k) for k in ('source', 'source_version', 'source_model_id'))
            and row.get('metric') == profile['policy']['metric']
            and row.get('effort') in (None, entry['profile']['effort'])]
        assessments.append({'config_id': config_id, 'configuration_sha256': configuration_digest(entry),
                            'score_evidence': scores, 'blockers': reasons})
        blockers.extend('model:' + config_id + ':' + reason for reason in reasons)
    pack = build_qualification_pack(profile['observations'], registry, profile['policy'],
                                   review=profile['review'], now=now,
                                   synthetic=profile['synthetic'])
    blockers.extend(pack['blockers'])
    if not blockers:
        try:
            select_profiles(pack['preview'], profile['requirements'], now=now)
        except ValueError:
            blockers.append('task:no_compatible_candidates')
    body = {'schema_version': REPORT_SCHEMA, 'created_at': now,
            'profile': deepcopy(profile), 'profile_sha256': digest(profile),
            'source_choice': deepcopy(profile['source_choice']), 'candidates': assessments,
            'qualification_pack': pack, 'blockers': sorted(set(blockers)),
            'status': 'blocked' if blockers else pack['status'],
            'production_ready': not blockers and pack['production_ready']}
    return {**body, 'report_id': digest(body)}


def qualified_pool_core(client, report, *, run_id, budget_store, model_bounds,
                        verify_bound=None, allow_synthetic=False, clock=None, **core_options):
    """Controlled first-release entry: qualification AND durable budget admission.

    Original packages may resume saved runs after expiry; ScoreCore refuses a
    new run using an expired snapshot. Synthetic admission is an explicit test
    option, not a network guard or spending authority. Use MockTransport in tests.
    """
    from ..budget import BudgetedCore
    from .integration import ScoreCore, utc_now
    from .qualification import qualified_snapshot
    if (report.get('schema_version') != REPORT_SCHEMA
            or report != prepare_pool(report['profile'], now=report['created_at'])):
        raise ValueError('First-pool report integrity mismatch')
    if report['blockers']:
        raise ValueError('First-pool qualification blocked')
    snapshot = qualified_snapshot(report['qualification_pack'], now=report['created_at'],
                                  allow_synthetic=allow_synthetic)
    engine = ScoreCore(client, snapshot, report['profile']['requirements'],
                       report['profile']['decision'], run_id=run_id, clock=clock or utc_now,
                       **core_options)
    return BudgetedCore(engine, budget_store, model_bounds=model_bounds, tool_bounds={},
                        verify_bound=verify_bound)
