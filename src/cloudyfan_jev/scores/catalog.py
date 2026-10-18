"""Build source-native tier pools; benchmark evidence is never correctness proof."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from urllib.parse import urlsplit

SCHEMA = 'cloudyfan-score-snapshot-v1'
TIERS = ('low', 'mid', 'high')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must include timezone')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return dt.astimezone(timezone.utc)


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Expected a finite number')
    return value


def _positive(value):
    if type(value) is not int or value < 1:
        raise ValueError('Expected a positive integer')
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Expected a nonempty string')
    return value


def _url(value):
    parsed = urlsplit(_text(value))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Evidence URL must be HTTPS without credentials')
    return value


def _policy(policy):
    for key in ('id', 'domain', 'source', 'source_version', 'metric'):
        _text(policy[key])
    if number(policy['low_mid']) >= number(policy['mid_high']):
        raise ValueError('Tier boundaries must increase')
    for key in ('max_source_age_hours', 'max_catalog_age_hours', 'snapshot_ttl_hours'):
        _positive(policy[key])
    if type(policy['min_samples']) is not int or policy['min_samples'] < 0:
        raise ValueError('min_samples must be a nonnegative integer')
    if policy.get('max_evaluation_age_days') is not None:
        _positive(policy['max_evaluation_age_days'])
    # A native scale is mandatory. Percentiles and cross-source averages are not implicit.
    if policy.get('scale') != 'source_native':
        raise ValueError('Only explicit source_native thresholds are supported')


def _profile(profile):
    for key in ('model', 'model_version', 'effort', 'provider', 'provider_name'):
        _text(profile[key])
    accepted = profile['accepted_models']
    if not isinstance(accepted, list) or not accepted or any(not isinstance(x, str) or not x for x in accepted):
        raise ValueError('Explicit accepted models required')
    if profile['model_version'] not in accepted or not isinstance(profile['capability_card'], dict):
        raise ValueError('Pinned model version and public capability card required')
    # Reuse the routing boundary: no private evaluation fields can reach Jev.
    from ..routing import _reject_private
    _reject_private(profile['capability_card'])


def _entry(entry):
    _text(entry['config_id'])
    _profile(entry['profile'])
    if type(entry['approved']) is not bool:
        raise ValueError('approved must be explicit boolean')
    _positive(entry['context_tokens'])
    if not isinstance(entry['capabilities'], list) or any(not isinstance(x, str) or not x for x in entry['capabilities']):
        raise ValueError('Capabilities must be explicit strings')
    timestamp(entry['catalog_checked_at'])
    for key in ('estimated_request_usd', 'estimated_latency_ms'):
        if entry.get(key) is not None and number(entry[key]) < 0:
            raise ValueError('Estimates cannot be negative')
    if entry.get('estimated_request_usd') is not None:
        _text(entry['cost_basis'])


def _catalog_eligible(entry, policy, now):
    if not entry['approved']:
        return 'configuration_not_approved'
    checked = timestamp(entry['catalog_checked_at'])
    if checked > now:
        return 'catalog_timestamp_in_future'
    if now - checked >= timedelta(hours=policy['max_catalog_age_hours']):
        return 'catalog_stale'
    return None


def _observation_reason(obs, entry, policy, now):
    try:
        number(obs['score'])
        _url(obs['source_url'])
        raw_hash = obs['raw_sha256']
        if not isinstance(raw_hash, str) or len(raw_hash) != 64 or any(c not in '0123456789abcdef' for c in raw_hash):
            return 'invalid_provenance'
        retrieved = timestamp(obs['retrieved_at'])
        if retrieved > now:
            return 'source_timestamp_in_future'
        if now - retrieved >= timedelta(hours=policy['max_source_age_hours']):
            return 'source_stale'
        effort = obs.get('effort')
        if effort is None:
            effort = entry.get('verified_source_effort')
            if not effort:
                return 'effort_unknown'
            _url(entry['effort_evidence_url'])
        if effort != entry['profile']['effort']:
            return 'effort_mismatch'
        n = obs.get('sample_count')
        if n is not None and (type(n) is not int or n < 0):
            return 'invalid_sample_count'
        if policy['min_samples'] and (n is None or n < policy['min_samples']):
            return 'insufficient_coverage'
        evaluated = obs.get('evaluated_at')
        if evaluated:
            # Sources often expose only a date. Treat that date as UTC midnight.
            date = datetime.fromisoformat(evaluated.replace('Z', '+00:00'))
            date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
            if date > now:
                return 'evaluation_timestamp_in_future'
            if policy.get('max_evaluation_age_days') is not None and now - date >= timedelta(days=policy['max_evaluation_age_days']):
                return 'evaluation_stale'
        elif policy.get('max_evaluation_age_days') is not None:
            return 'evaluation_date_unknown'
    except (KeyError, TypeError, ValueError):
        return 'invalid_observation'
    return None


def compile_snapshot(observations, registry, policy, *, now):
    """Compile one domain/source/version/metric at explicit native thresholds.

    registry maps reviewed source identities to executable pinned configurations.
    No name guessing, threshold fitting, source fallback, or holdout access occurs.
    Missing evidence is excluded, not assigned to the low tier. Duplicate evidence
    is quarantined rather than picking the best score or counting it twice.
    """
    _policy(policy)
    from ..routing import _reject_private
    _reject_private(observations)
    instant = timestamp(now)
    entries = deepcopy(registry['candidates'])
    oss = deepcopy(registry['open_source'])
    _entry(oss)
    reason = _catalog_eligible(oss, policy, instant)
    if reason:
        raise ValueError('Open-source configuration: ' + reason)
    if oss.get('open_weight') is not True:
        raise ValueError('Open-source lane requires explicitly approved open weights')
    ids = {oss['config_id']}
    for entry in entries:
        _entry(entry)
        _text(entry['source_model_id'])
        _text(entry['source'])
        _text(entry['source_version'])
        if entry['config_id'] in ids:
            raise ValueError('Duplicate configuration id')
        ids.add(entry['config_id'])
    pools, excluded = {tier: [] for tier in TIERS}, []
    expiries = [instant + timedelta(hours=policy['snapshot_ttl_hours']),
                timestamp(oss['catalog_checked_at']) + timedelta(hours=policy['max_catalog_age_hours'])]
    for entry in entries:
        reason = _catalog_eligible(entry, policy, instant)
        if entry['source'] != policy['source'] or entry['source_version'] != policy['source_version']:
            reason = 'source_binding_mismatch'
        matches = [o for o in observations if all(o.get(k) == policy[k] for k in
                   ('source', 'source_version', 'metric')) and o.get('source_model_id') == entry['source_model_id']]
        # Match measured effort when several settings share a source id; unknown
        # effort may only be resolved using an explicit reviewed evidence binding.
        matches = [o for o in matches if o.get('effort') in (None, entry['profile']['effort'])]
        if reason is None:
            reason = 'score_missing' if not matches else ('ambiguous_evidence' if len(matches) != 1 else
                     _observation_reason(matches[0], entry, policy, instant))
        if reason:
            excluded.append({'config_id': entry['config_id'], 'reason': reason})
            continue
        obs = deepcopy(matches[0])
        score = obs['score']
        tier = 'low' if score < policy['low_mid'] else ('high' if score > policy['mid_high'] else 'mid')
        entry['evidence'] = obs
        entry['profile']['external_score'] = score
        entry['profile']['capability_card']['benchmark_evidence'] = {
            **obs, 'score_is_correctness_probability': False,
            'effort_mapping': 'measured' if obs.get('effort') else 'reviewed_binding',
            'effort_evidence_url': entry.get('effort_evidence_url'),
        }
        pools[tier].append(entry)
        expiries.extend([timestamp(obs['retrieved_at']) + timedelta(hours=policy['max_source_age_hours']),
                         timestamp(entry['catalog_checked_at']) + timedelta(hours=policy['max_catalog_age_hours'])])
        if policy.get('max_evaluation_age_days') is not None:
            date = datetime.fromisoformat(obs['evaluated_at'].replace('Z', '+00:00'))
            date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
            expiries.append(date + timedelta(days=policy['max_evaluation_age_days']))
    for pool in pools.values():
        pool.sort(key=lambda row: row['config_id'])
    body = {'schema_version': SCHEMA, 'created_at': instant.isoformat(), 'valid_until': min(expiries).isoformat(),
            'policy': deepcopy(policy), 'pools': pools, 'open_source': oss,
            'excluded': sorted(excluded, key=lambda row: row['config_id']),
            'input_sha256': digest({'observations': observations, 'registry': registry})}
    return {**body, 'snapshot_id': digest(body)}


def verify_snapshot(snapshot, *, now=None):
    if snapshot.get('schema_version') != SCHEMA:
        raise ValueError('Unsupported score snapshot schema')
    body = {k: v for k, v in snapshot.items() if k != 'snapshot_id'}
    if snapshot.get('snapshot_id') != digest(body):
        raise ValueError('Snapshot digest mismatch')
    _policy(snapshot['policy'])
    if timestamp(snapshot['created_at']) >= timestamp(snapshot['valid_until']):
        raise ValueError('Empty snapshot lifetime')
    if now is not None:
        instant = timestamp(now)
        if instant < timestamp(snapshot['created_at']) or instant >= timestamp(snapshot['valid_until']):
            raise ValueError('Snapshot is stale or from the future')


def select_profiles(snapshot, requirements, *, now):
    """Bind one eligible configuration per tier without paid classification.

    Cost is an operator-supplied estimate, not paid cost or inferred success rate.
    Unknown estimates sort last; ties use latency then stable configuration id.
    Required capabilities and context are explicit caller input, not an LLM guess.
    """
    verify_snapshot(snapshot, now=now)
    from ..routing import _reject_private
    allowed = {'domain', 'capabilities', 'context_tokens', 'allowed_providers'}
    if set(requirements) - allowed:
        raise ValueError('Unknown requirement fields; use only public capability constraints')
    if requirements['domain'] != snapshot['policy']['domain']:
        raise ValueError('Task domain does not match score policy')
    required = requirements['capabilities']
    if not isinstance(required, list) or any(not isinstance(x, str) or not x for x in required):
        raise ValueError('Required capabilities must be a list of strings')
    _positive(requirements['context_tokens'])
    providers = requirements.get('allowed_providers')
    if providers is not None and (not isinstance(providers, list) or not providers or
                                  any(not isinstance(x, str) or not x for x in providers)):
        raise ValueError('Provider restriction must be a nonempty list')

    def compatible(entry):
        return (set(required) <= set(entry['capabilities']) and
                requirements['context_tokens'] <= entry['context_tokens'] and
                (providers is None or entry['profile']['provider'] in providers))

    def rank(entry):
        return tuple(entry.get(k) if entry.get(k) is not None else math.inf
                     for k in ('estimated_request_usd', 'estimated_latency_ms')) + (entry['config_id'],)

    selected = {}
    for tier in (*TIERS, 'open_source'):
        pool = [snapshot['open_source']] if tier == 'open_source' else snapshot['pools'][tier]
        eligible = [entry for entry in pool if compatible(entry)]
        if not eligible:
            raise ValueError('No qualified configuration for tier: ' + tier)
        selected[tier] = min(eligible, key=rank)
    profiles = {}
    for tier, entry in selected.items():
        profile = deepcopy(entry['profile'])
        profile.setdefault('external_score', None)
        profile['capability_card']['score_selection'] = {
            'snapshot_id': snapshot['snapshot_id'], 'policy_id': snapshot['policy']['id'],
            'config_id': entry['config_id'], 'requirements': deepcopy(requirements),
            'estimated_request_usd': entry.get('estimated_request_usd'),
            'cost_basis': entry.get('cost_basis'), 'estimate_is_paid_cost': False,
        }
        profiles[tier] = profile
    _reject_private(profiles)
    return profiles
