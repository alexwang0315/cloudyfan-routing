"""Offline SQL candidate reconciliation; an inventory never authorizes dispatch.

Only supplied metadata is inspected. No answers, credentials, files or network
are read here. Unknown configuration fields never share qualification evidence.
"""
from copy import deepcopy
from datetime import timedelta
import re

from .catalog import digest, timestamp
from .configuration import configuration_digest

CATEGORY = 'artificial-analysis:coding_index'
TIERS = ('open_source', 'low', 'mid', 'high')
POSITIONS = ('primary', 'backup_1', 'backup_2')
IDENTITY_FIELDS = ('model', 'model_version', 'accepted_models', 'effort', 'reasoning_parameter',
                   'provider', 'provider_name', 'endpoint', 'quantization', 'engine', 'weights_digest')
HISTORICAL_TRANSPORT_BLOCKER = 'omit_reasoning_transport_contract_not_integrated'
EVIDENCE_FIELDS = frozenset(('profile', 'configuration_sha256', 'report_id', 'artifact_sha256',
                            'domain', 'suite_sha256', 'grader_sha256', 'public_contract_sha256',
                            'valid_until', 'qualification_status'))


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value != 'unknown'


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _profile(raw, contract=None):
    profile = {k: deepcopy(raw.get(k)) for k in IDENTITY_FIELDS if k in raw}
    # Declared wire mode cannot turn provider-default effort into none or low.
    if contract and contract.get('reasoning_field_must_be_absent') is True:
        profile['reasoning_parameter'] = 'omit'
    return profile


def _identity_gaps(profile):
    gaps = [f'missing_{k}' for k in ('model', 'model_version', 'provider', 'provider_name',
                                   'endpoint', 'quantization') if not _text(profile.get(k))]
    effort = profile.get('effort')
    if not _text(effort) or effort == 'provider_default':
        gaps.append('unverified_effort')
    accepted = profile.get('accepted_models')
    if (not isinstance(accepted, list) or not accepted or
            not all(_text(v) for v in accepted)):
        gaps.append('missing_accepted_response_identities')
    if profile.get('reasoning_parameter', 'effort') not in ('effort', 'omit'):
        gaps.append('unknown_reasoning_parameter')
    elif profile.get('reasoning_parameter') == 'omit' and effort != 'none':
        gaps.append('omit_requires_explicit_none_effort')
    if profile.get('model', '').endswith(':free') and not (profile.get('model_version') or '').endswith(':free'):
        gaps.append('free_request_route_not_preserved')
    return gaps


def _freshness(at, now, *, expires=None):
    if not at:
        return {'status': 'unknown', 'observed_at': at, 'valid_until': expires}
    observed = timestamp(at)
    expiry = timestamp(expires) if expires else observed + timedelta(hours=48)
    if observed > now or expiry <= observed:
        status = 'invalid_interval'
    else:
        status = 'fresh' if now < expiry else 'expired'
    return {'status': status, 'observed_at': at, 'valid_until': expiry.isoformat(),
            'scope': 'inventory freshness only; not model or health qualification'}


def _evidence(config, evidence_index, now):
    """Compare exact metadata without treating caller-supplied claims as approval."""
    if not config['exact_configuration_complete']:
        return {'status': 'incomplete_identity_no_reuse', 'automatic_reuse': False}
    key = config['configuration_sha256']
    proof = evidence_index.get(key)
    if proof is None:
        return {'status': 'missing', 'automatic_reuse': False}
    if not isinstance(proof, dict) or set(proof) - EVIDENCE_FIELDS:
        raise ValueError('Evidence index must contain metadata only, with known fields')
    if (proof.get('configuration_sha256') != key or not isinstance(proof.get('profile'), dict) or
            configuration_digest({'profile': proof['profile']}) != key):
        return {'status': 'exact_identity_mismatch', 'automatic_reuse': False}
    required = ('artifact_sha256', 'suite_sha256', 'grader_sha256', 'public_contract_sha256')
    if (not all(_hash(proof.get(k)) for k in required) or not _text(proof.get('report_id')) or
            proof.get('domain') != 'bounded-sqlite-readonly' or not proof.get('valid_until')):
        return {'status': 'missing_contract_or_artifact_binding', 'automatic_reuse': False}
    expiry = timestamp(proof['valid_until'])
    return {'status': 'expired' if now >= expiry else 'matching_metadata_requires_contract_review',
            'report_id': proof['report_id'], 'artifact_sha256': proof['artifact_sha256'],
            'suite_sha256': proof['suite_sha256'], 'grader_sha256': proof['grader_sha256'],
            'public_contract_sha256': proof['public_contract_sha256'],
            'valid_until': proof['valid_until'],
            'qualification_status': proof.get('qualification_status'),
            'automatic_reuse': False,
            'scope': 'No artifact truth, current SQL contract equality or reviewer approval is established'}



def _archived_profile(coverage, archive, row, tier):
    """Recover explicit archived fields only after summary and hash agreement."""
    if archive is None or row.get('status') != 'research_admitted_only':
        return row['profile'], None
    if archive.get('pack_id') != digest({k: v for k, v in archive.items() if k != 'pack_id'}):
        raise ValueError('Archived pack integrity mismatch')
    if archive['matrix']['matrix_id'] != coverage['existing_research_admission']['matrix_id']:
        raise ValueError('Archived matrix does not match inventory reference')
    entries = archive['matrix']['categories'][CATEGORY]['pools'].get(tier, [])
    matches = [e for e in entries if e.get('config_id') == row.get('config_id')]
    if len(matches) != 1:
        raise ValueError('Expected one exact archived research configuration')
    entry = matches[0]
    profile = entry['profile']
    for field in IDENTITY_FIELDS:
        value = row['profile'].get(field)
        if value is not None and value != profile.get(field):
            raise ValueError(f'Archived profile conflicts with inventory {field}')
    config_hash = configuration_digest(entry)
    if config_hash != row.get('configuration_sha256'):
        raise ValueError('Archived configuration hash does not match inventory')
    qualification = entry['sql_qualification']
    admission = archive['matrix']['research_measured_admissions'][entry['config_id']]
    if (qualification.get('configuration_sha256') != config_hash or
            admission.get('configuration_sha256') != config_hash or
            qualification.get('parent_measured_report_id') != archive['qualification_report_ids'][tier] or
            admission.get('parent_report_id') != archive['qualification_report_ids'][tier]):
        raise ValueError('Archived SQL evidence identity mismatch')
    metadata = {'pack_id': archive['pack_id'], 'matrix_id': archive['matrix']['matrix_id'],
                'source_config_id': entry['config_id'], 'configuration_sha256': config_hash,
                'parent_report_id': qualification['parent_measured_report_id'],
                'research_admission_id': qualification.get('report_id'),
                'suite_id': admission.get('suite_id'),
                'identity_contract_id': admission.get('identity_contract_id'),
                'grader_sha256': None, 'public_contract_sha256': None,
                'valid_until': qualification.get('valid_until'),
                'retrieved_at': archive.get('retrieved_at'),
                'automatic_reuse': False,
                'scope': 'Exact archived research reference; current suite/grader/public contract and eligibility still require comparison'}
    return profile, metadata

def prepare_sql_backup_candidates(coverage, sql_low_proposal, *, now, evidence_index=None, archived_pack=None):
    """Reconcile 12 SQL positions, preferring the explicit SQL low proposal.

    Complete identities are deduplicated by the shared configuration contract.
    Incomplete profiles remain separate proposals, even when names match. The
    returned profiles are preparation input, never an executable qualification.
    """
    current = timestamp(now)
    evidence_index = evidence_index or {}
    if not isinstance(evidence_index, dict):
        raise ValueError('Expected an evidence index mapping')
    if (sql_low_proposal.get('category_id') != CATEGORY or
            sql_low_proposal.get('target_pool') != 'low'):
        raise ValueError('SQL low proposal must target the coding category low pool')
    pools = [p for p in coverage['named_pool_positions']['pools'] if p['category_id'] == CATEGORY]
    if len(pools) != 4 or {p['tier'] for p in pools} != set(TIERS):
        raise ValueError('Expected exactly four coding pools')
    pools = {p['tier']: p for p in pools}
    for pool in pools.values():
        if {r.get('position') for r in pool['positions']} != set(POSITIONS) or len(pool['positions']) != 3:
            raise ValueError('Expected exactly three distinct positions per pool')
    low = sql_low_proposal['candidates']
    if len(low) != 3 or {r.get('position') for r in low} != set(POSITIONS):
        raise ValueError('Expected three SQL low proposal positions')
    low = {r['position']: r for r in low}
    selected = {tier: {r['position']: r for r in pools[tier]['positions']} for tier in TIERS}
    selected['low'] = low
    configs, slots = {}, []
    for tier in TIERS:
        models = []
        versions = []
        for position in POSITIONS:
            row = selected[tier][position]
            raw = row['entry']['profile'] if tier == 'low' else row['profile']
            archive_metadata = None
            if tier != 'low':
                raw, archive_metadata = _archived_profile(coverage, archived_pack, row, tier)
                if archive_metadata is not None:
                    archive_metadata['freshness'] = _freshness(archive_metadata.get('retrieved_at'), current, expires=archive_metadata.get('valid_until'))
            if not isinstance(raw, dict) or not _text(raw.get('model')):
                raise ValueError('A named candidate profile is required')
            models.append(raw['model'])
            if _text(raw.get('model_version')):
                versions.append(raw['model_version'])
            contract = row.get('request_contract') if tier == 'low' else None
            profile = _profile(raw, contract)
            gaps = _identity_gaps(profile)
            if gaps:
                key = digest({'proposal_identity': profile, 'tier': tier, 'position': position})
                config_hash = None
            else:
                config_hash = configuration_digest({'profile': profile})
                key = config_hash
            cid = 'sql-backup-' + key
            source = 'sql_specific_low_v1' if tier == 'low' else 'broad_category_inventory'
            if cid not in configs:
                legacy = row.get('status') == 'research_admitted_only'
                legacy_hash = row.get('configuration_sha256')
                historical_blockers = row.get('qualification', {}).get('blockers', [])
                source_at = (row.get('source', {}).get('retrieved_at') if tier == 'low' else
                             coverage.get('source_capture_times', {}).get('catalog'))
                configs[cid] = {
                    'config_id': cid, 'profile': profile,
                    'exact_configuration_complete': not gaps,
                    'configuration_sha256': config_hash, 'proposal_identity_sha256': key,
                    'identity_gaps': gaps, 'slots': [],
                    'exam_compatibility': {
                        'status': ('incomplete_identity' if gaps else
                                   'requires_new_explicit_exact_response_contract' if profile['accepted_models'] != [profile['model_version']] else
                                   'exact_response_contract_compatible_only'),
                        'existing_aliases_preserved': True,
                        'new_contract_requires_new_configuration_hash': True,
                        'is_exam_dispatch_authority': False},
                    'source': source,
                    'source_freshness': _freshness(source_at, current),
                    'historical_source_configuration_sha256': legacy_hash,
                    'archived_profile_evidence': archive_metadata,
                    'historical_sql_evidence': {
                        'status': 'historical_research_reference_only' if legacy else 'missing',
                        'source_config_id': row.get('config_id'), 'source_scope': row.get('scope'),
                        'freshness': _freshness(coverage.get('existing_research_admission', {}).get('verification_time'), current, expires=coverage.get('source_capture_times', {}).get('existing_research_valid_until')) if legacy else {'status': 'unknown'},
                        'automatic_reuse': False,
                        'reason': 'Reconcile actual archived report and exact SQL contract before reuse; no answers are read'},
                    'historical_blockers': deepcopy(historical_blockers),
                    'current_blockers': [v for v in historical_blockers if v != HISTORICAL_TRANSPORT_BLOCKER],
                    'transport_implementation': {
                        'none_omit_support_implemented': True,
                        'historical_blocker_removed_from_current': HISTORICAL_TRANSPORT_BLOCKER in historical_blockers,
                        'provider_default_not_reinterpreted': True},
                    'new_backup_evidence': {
                        'whole_flow_cost_speed': 'missing', 'classifier_pair_variants': 'missing',
                        'current_health': 'requires_current_exact_endpoint_observation',
                        'enforceable_fee_guarantee': 'not_established',
                        'old_singleton_calibration_reusable': False,
                        'reason': 'New pool/flow/classifier hashes require independent applicable evidence'},
                    'free_route_must_be_preserved': raw['model'].endswith(':free'),
                    'data_policy': {k: deepcopy((contract or {}).get(k)) for k in
                                    ('allowed_data_classes', 'cloud_free_trial_opt_in_required')},
                    'quoted_scenarios': deepcopy(row.get('quoted_scenarios', [])),
                    'runtime_dispatch_enabled': False, 'production_qualified': False}
            slot = {'tier': tier, 'position': position, 'config_id': cid,
                    'source': source, 'runtime_dispatch_enabled': False}
            slots.append(slot)
            configs[cid]['slots'].append({'tier': tier, 'position': position})
        if len(set(models)) != 3 or len(set(versions)) != len(versions):
            raise ValueError('Each pool requires three different model routes')
    for config in configs.values():
        config['sql_evidence_comparison'] = _evidence(config, evidence_index, current)
    report = {
        'schema_version': 'cloudyfan-sql-backup-candidate-review-v1',
        'category_id': CATEGORY, 'created_at': now,
        'purpose': 'Offline candidate and evidence reconciliation; not dispatch or qualification',
        'input_digests': {'coverage': digest(coverage), 'sql_low_proposal': digest(sql_low_proposal),
                          'evidence_index': digest(evidence_index),
                          'archived_pack': digest(archived_pack) if archived_pack is not None else None},
        'slots': slots, 'configurations': list(configs.values()),
        'low_proposal_comparison': {
            'selected_proposal': 'sql_specific_low_v1',
            'selection_basis': 'Explicit SQL configuration and request contracts take priority over generic quoted-price inventory; not measured answer performance',
            'generic_original_order': [_profile(r['profile']) for r in pools['low']['positions']],
            'sql_specific_original_order': [_profile(r['entry']['profile'], r.get('request_contract')) for r in sql_low_proposal['candidates']],
            'unselected_generic_proposal_reason': 'Preserved alternative; missing exact runtime version/effort and independent SQL evidence; quoted zero does not establish full cost',
            'minimum_price_gap_fraction': sql_low_proposal.get('minimum_price_gap_fraction'),
            'price_gap_basis': sql_low_proposal.get('price_gap_basis'),
            'quote_gap_does_not_establish_classifier_saving': True,
            'proposal_freshness': _freshness(sql_low_proposal.get('created_at'), current,
                                              expires=sql_low_proposal.get('review_before'))},
        'counts': {'positions': len(slots), 'unique_preparation_records': len(configs),
                   'complete_exact_configurations': sum(not c['identity_gaps'] for c in configs.values()),
                   'historical_research_references': sum(c['historical_sql_evidence']['status'] == 'historical_research_reference_only' for c in configs.values()),
                   'exact_response_contract_compatible': sum(c['exam_compatibility']['status'] == 'exact_response_contract_compatible_only' for c in configs.values()),
                   'runtime_dispatch_enabled': 0, 'production_qualified': 0},
        'rg01_rules_changed': False, 'historical_packs_changed': False,
        'formal_qualification': 'pending', 'runtime_dispatch_enabled': False,
        'production_qualified': False, 'model_api_calls': 0, 'source_gets': 0,
        'answers_read': False, 'automatic_evidence_reuse': False}
    report['review_id'] = digest(report)
    return report
