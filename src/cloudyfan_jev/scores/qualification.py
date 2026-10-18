"""Offline qualification review, distinct from score ingestion and production approval."""
from copy import deepcopy

from .catalog import compile_snapshot, digest, timestamp, verify_snapshot

SCHEMA = 'cloudyfan-qualification-pack-v1'
REVIEW_AREAS = ('domain', 'source', 'thresholds', 'coverage', 'expiry', 'access')


def build_qualification_pack(observations, registry, policy, *, review, now, synthetic=False):
    """Preserve review inputs and evidence gaps without inventing missing approval.

    Trusted operators supply review attestations; a hash proves binding/integrity,
    not a signature, source permission, or real model qualification.
    """
    inputs = deepcopy({'observations': observations, 'registry': registry, 'policy': policy})
    if type(synthetic) is not bool:
        raise ValueError('synthetic must be explicit boolean')
    instant = timestamp(now)

    def approved(record):
        try:
            return (record.get('status') == 'approved' and
                    isinstance(record.get('reviewer'), str) and bool(record['reviewer'].strip()) and
                    isinstance(record.get('evidence'), list) and bool(record['evidence']) and
                    all(isinstance(x, str) and x.strip() for x in record['evidence']) and
                    timestamp(record['reviewed_at']) <= instant)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    blockers = []
    if review.get('input_sha256') != digest(inputs):
        blockers.append('review:input_binding_mismatch')
    for area in REVIEW_AREAS:
        if not approved(review.get(area, {})):
            blockers.append(area + ':review_pending')
    for entry in [registry['open_source'], *registry['candidates']]:
        if not approved(review.get('models', {}).get(entry['config_id'], {})):
            blockers.append('model:' + entry['config_id'] + ':review_pending')
    try:
        preview = compile_snapshot(observations, registry, policy, now=now)
        for tier, rows in preview['pools'].items():
            if not rows:
                blockers.append(tier + ':no_qualified_candidates')
    except (ValueError, KeyError, TypeError):
        preview = None
        blockers.append('inputs:invalid_or_unqualified')
    body = {'schema_version': SCHEMA, 'created_at': now, 'synthetic': synthetic,
            'inputs': inputs, 'input_sha256': digest(inputs), 'review': deepcopy(review),
            'preview': preview, 'blockers': blockers,
            'production_ready': not blockers and not synthetic,
            'status': 'blocked' if blockers else ('synthetic_only' if synthetic else 'qualified')}
    return {**body, 'pack_id': digest(body)}


def qualified_snapshot(pack, *, now, allow_synthetic=False):
    """Explicit promotion seam; never implicitly publishes a preview."""
    if pack.get('schema_version') != SCHEMA or pack.get('pack_id') != digest(
            {k: v for k, v in pack.items() if k != 'pack_id'}):
        raise ValueError('Qualification pack integrity mismatch')
    if pack['status'] not in ('qualified', 'synthetic_only') or pack['blockers']:
        raise ValueError('Review pack is not qualified')
    if pack['synthetic'] and not allow_synthetic:
        raise ValueError('Synthetic qualification cannot become a production pool')
    verify_snapshot(pack['preview'], now=now)
    snapshot = deepcopy(pack['preview'])
    snapshot['qualification'] = {k: pack[k] for k in ('pack_id', 'status', 'production_ready')}
    snapshot['snapshot_id'] = digest({k: v for k, v in snapshot.items() if k != 'snapshot_id'})
    return snapshot
