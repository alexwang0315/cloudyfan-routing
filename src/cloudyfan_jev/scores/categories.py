"""Independent native benchmark categories; no guessed identity or paid classifier.

Compilation is preparation, not service qualification. Explicit reviewed bindings
select a source observation, and the existing score compiler retains its SQL
qualification rule for SQL family priors. Category priors require a separate
category-bound independent qualification. QCS admission still needs task evidence.
"""
from copy import deepcopy
from datetime import timedelta
from urllib.parse import urlsplit

from .catalog import compile_snapshot, digest, number, timestamp, verify_snapshot

SCHEMA = 'cloudyfan-category-pools-v1'
SOURCE_VERSION = 'openrouter-benchmarks-v1'
CATEGORY_SPECS = {
    **{f'artificial-analysis:{metric}': {
        'source': 'artificial-analysis', 'metric': metric, 'unit': 'AA_native_index',
    } for metric in ('agentic_index', 'coding_index', 'intelligence_index')},
    **{f'design-arena:models:{category}': {
        'source': 'design-arena', 'metric': 'elo', 'arena': 'models',
        'category': category, 'unit': 'Elo',
    } for category in ('3d', 'asciiart', 'audiorealism', 'codecategories', 'dataviz',
                       'gamedev', 'graphicdesign', 'image', 'imageediting', 'logo',
                       'svg', 'text-to-speech', 'uicomponent', 'website')},
    **{f'openrouter:{benchmark}': {
        'source': 'openrouter', 'metric': 'f1_by_item' if benchmark == 'search_widesearch' else 'accuracy',
        'benchmark_type': benchmark, 'unit': 'ratio0..1',
    } for benchmark in ('gpqa_diamond', 'search_browsecomp', 'search_dsqa',
                        'search_hle', 'search_widesearch', 'tau_bench_verified_airline')},
}
_ALIASES = {'coding': 'artificial-analysis:coding_index',
            'intelligence': 'artificial-analysis:intelligence_index',
            'agentic': 'artificial-analysis:agentic_index',
            **{spec['category']: key for key, spec in CATEGORY_SPECS.items() if 'category' in spec}}


def resolve_category(value):
    """Resolve a canonical ID or explicit unique alias, never classify prose."""
    if isinstance(value, str):
        if value in CATEGORY_SPECS:
            return value
        if value in _ALIASES:
            return _ALIASES[value]
    raise ValueError('Unknown or ambiguous task category; provide an explicit canonical category')


def category_matches(row, category):
    """Match only the exact source-native scope and required quality metric."""
    category = resolve_category(category)
    spec = CATEGORY_SPECS[category]
    return (isinstance(row, dict) and row.get('schema_version') == 'qcs-source-v1'
            and row.get('source_version') == SOURCE_VERSION
            and row.get('source') == spec['source']
            and all(row.get(key) == spec.get(key) for key in ('arena', 'category', 'benchmark_type'))
            and isinstance(row.get('quality'), dict) and spec['metric'] in row['quality'])


def _https(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (TypeError, ValueError):
        return False


def _bound_observation(entry, rows, category):
    binding = entry.get('observation_binding')
    if (not isinstance(binding, dict) or binding.get('reviewed') is not True
            or not _https(binding.get('evidence_url'))
            or type(binding.get('source_row_index')) is not int
            or binding['source_row_index'] < 0):
        return None, 'observation_binding_missing_or_mismatch'
    matches = [row for row in rows if category_matches(row, category)
               and row.get('model_id') == entry.get('source_model_id')
               and row.get('raw_sha256') == binding.get('raw_sha256')
               and row.get('source_row_index') == binding['source_row_index']]
    if len(matches) != 1:
        return None, 'observation_binding_missing_or_mismatch'
    row = matches[0]
    try:
        score = number(row['quality'][CATEGORY_SPECS[category]['metric']])
    except (KeyError, TypeError, ValueError):
        return None, 'invalid_observation'
    if CATEGORY_SPECS[category]['unit'] == 'ratio0..1' and not 0 <= score <= 1:
        return None, 'score_outside_native_range'
    if row.get('binding_status') == 'ambiguous_source_identity':
        return None, 'ambiguous_source_identity'
    for field in ('run_config', 'search_engine', 'search_surface'):
        value = row.get(field)
        if ((field in binding and binding[field] != value)
                or (value not in (None, {}) and field not in binding)):
            return None, 'source_configuration_binding_mismatch'
    try:
        retrieved = timestamp(row['retrieved_at'])
        as_of = timestamp(row['as_of']) if row.get('as_of') is not None else None
        # A fresh download cannot make an old upstream snapshot fresh again.
        effective = min(retrieved, as_of) if as_of is not None else retrieved
    except (KeyError, TypeError, ValueError):
        return None, 'invalid_observation'
    observation = {
        'source': row['source'], 'source_version': row['source_version'],
        'source_model_id': row['model_id'], 'metric': CATEGORY_SPECS[category]['metric'],
        'score': row['quality'][CATEGORY_SPECS[category]['metric']],
        'effort': row.get('effort'), 'sample_count': row.get('sample_count'),
        'evaluated_at': row.get('evaluated_at'), 'retrieved_at': effective.isoformat(),
        'source_url': row.get('source_url'), 'raw_sha256': row.get('raw_sha256'),
        'source_row_index': row['source_row_index'], 'as_of': row.get('as_of'),
        'source_variant_label': row.get('source_variant_label'),
        'source_retrieved_at': row['retrieved_at'], 'category_id': category,
        'unit': CATEGORY_SPECS[category]['unit'],
        'source_scope': {key: row.get(key) for key in ('arena', 'category', 'benchmark_type',
                         'run_config', 'search_engine', 'search_surface')},
        'review_evidence_url': binding['evidence_url'],
    }
    return observation, None


def _entry_reason(entry, observation, policy, now):
    # Validation follows the pre-existing compiler rules, including independent
    # SQL qualification for family priors, rather than weakening those gates.
    from .catalog import _catalog_eligible, _entry, _observation_reason
    from ..routing import _reject_private
    try:
        _entry(entry)
        _reject_private(observation)
        if (entry.get('source') != policy['source']
                or entry.get('source_version') != policy['source_version']):
            return 'source_binding_mismatch'
        reason = _catalog_eligible(entry, policy, now)
        if reason:
            return reason
        if timestamp(observation['source_retrieved_at']) > now:
            return 'source_timestamp_in_future'
        if observation['as_of'] is not None and timestamp(observation['as_of']) > now:
            return 'source_timestamp_in_future'
        return _observation_reason(observation, entry, policy, now)
    except (KeyError, TypeError, ValueError):
        return 'invalid_configuration_or_observation'


def _decorate(entry, observation, category):
    entry = deepcopy(entry)
    entry['profile']['capability_card']['category_id'] = category
    entry['profile']['capability_card']['category_source'] = {
        'source': observation['source'], 'source_version': observation['source_version'],
        'metric': observation['metric'], 'unit': observation['unit'],
        'source_scope': deepcopy(observation['source_scope']),
        'raw_sha256': observation['raw_sha256'],
        'source_row_index': observation['source_row_index'],
        'review_evidence_url': observation['review_evidence_url'],
    }
    return entry



def _independent_oss(entry, policy, category, now):
    """An independently qualified OSS lane need not appear on a public board."""
    from .catalog import _catalog_eligible, _entry
    try:
        _entry(entry)
        # Always require a category-specific report for this scoreless path,
        # including exact-role policies. Preserve any legacy SQL rule too.
        category_policy = {**policy, 'score_role': 'category_family_prior'}
        reason = _catalog_eligible(entry, category_policy, now)
        if reason is None and policy.get('score_role') == 'family_effort_prior':
            reason = _catalog_eligible(entry, policy, now)
        if reason:
            return None, reason
        evidence = {key: deepcopy(entry['category_qualification'][key]) for key in
                    ('category_id', 'configuration_sha256', 'report_id', 'valid_until')}
        result = deepcopy(entry)
        card = result['profile']['capability_card']
        card.pop('benchmark_evidence', None)
        card['category_id'] = category
        card['category_source'] = {'basis': 'independent_category_qualification', **evidence}
        result['profile']['external_score'] = None
        result['evidence'] = {'evidence_role': 'independent_category_qualification', **evidence,
                              'score_is_correctness_probability': False}
        return result, None
    except (KeyError, TypeError, ValueError):
        return None, 'invalid_configuration_or_observation'

def _policy_reason(policy, category):
    from .catalog import _policy
    spec = CATEGORY_SPECS[category]
    if (not isinstance(policy, dict) or policy.get('domain') != category
            or policy.get('source') != spec['source']
            or policy.get('source_version') != SOURCE_VERSION
            or policy.get('metric') != spec['metric']):
        return 'category_policy_mismatch'
    try:
        _policy(policy)
    except (KeyError, TypeError, ValueError):
        return 'invalid_category_policy'
    return None


def _compile_category(rows, registry, policy, category, instant):
    result = {'status': 'blocked', 'snapshot': None,
              'pools': {tier: [] for tier in ('open_source', 'low', 'mid', 'high')},
              'excluded': [], 'policy': deepcopy(policy)}
    reason = 'category_policy_missing' if policy is None else _policy_reason(policy, category)
    if reason:
        result['excluded'].append({'config_id': None, 'reason': reason})
        return result
    if not isinstance(registry, dict):
        result['excluded'].append({'config_id': None, 'reason': 'category_registry_missing'})
        return result
    oss_entries = registry.get('open_source_candidates')
    if oss_entries is None:
        oss_entries = [registry['open_source']] if registry.get('open_source') is not None else []
    entries = registry.get('candidates', [])
    if not isinstance(oss_entries, list) or not isinstance(entries, list):
        result['excluded'].append({'config_id': None, 'reason': 'invalid_category_registry'})
        return result
    seen, duplicate_ids = set(), set()
    for entry in [*oss_entries, *entries]:
        config_id = entry.get('config_id') if isinstance(entry, dict) else None
        if isinstance(config_id, str):
            if config_id in seen:
                duplicate_ids.add(config_id)
            seen.add(config_id)
    valid_oss, valid_scored = [], []
    for lane, candidates in (('open_source', oss_entries), ('scored', entries)):
        for entry in candidates:
            config_id = entry.get('config_id') if isinstance(entry, dict) else None
            if not isinstance(entry, dict):
                result['excluded'].append({'config_id': None, 'reason': 'invalid_configuration_or_observation'})
                continue
            unscored = lane == 'open_source' and 'observation_binding' not in entry
            if unscored:
                prepared, reason = _independent_oss(entry, policy, category, instant)
                obs = None
            else:
                obs, reason = _bound_observation(entry, rows, category)
            if isinstance(config_id, str) and config_id in duplicate_ids:
                reason = 'duplicate_configuration_id'
            if reason is None and not unscored:
                reason = _entry_reason(entry, obs, policy, instant)
            if reason is None and lane == 'open_source' and entry.get('open_weight') is not True:
                reason = 'open_weights_not_approved'
            if reason:
                result['excluded'].append({'config_id': config_id, 'reason': reason})
            else:
                (valid_oss if lane == 'open_source' else valid_scored).append(
                    (prepared if unscored else _decorate(entry, obs, category), obs))
    if not valid_oss:
        result['excluded'].append({'config_id': None, 'reason': 'no_qualified_open_source_configuration'})
        return result
    valid_oss.sort(key=lambda pair: pair[0]['config_id'])
    representative = valid_oss[0][0]
    # Compile one reviewed source binding at a time: distinct measured variants
    # of one source model are never averaged or selected by their best score.
    base = compile_snapshot([], {'candidates': [], 'open_source': representative}, policy,
                            now=instant.isoformat())
    expiries = [timestamp(base['valid_until'])]
    for entry, obs in valid_scored:
        partial = compile_snapshot([obs], {'candidates': [entry], 'open_source': representative}, policy,
                                   now=instant.isoformat())
        for tier in ('low', 'mid', 'high'):
            result['pools'][tier].extend(partial['pools'][tier])
        result['excluded'].extend(partial['excluded'])
        expiries.append(timestamp(partial['valid_until']))
    for entry, obs in valid_oss:
        independent = compile_snapshot([], {'candidates': [], 'open_source': entry}, policy,
                                       now=instant.isoformat())
        expiries.append(timestamp(independent['valid_until']))
        if obs is None:
            result['pools']['open_source'].append(entry)
            expiries.append(timestamp(entry['category_qualification']['valid_until']))
            continue
        entry['evidence'] = deepcopy(obs)
        entry['profile']['external_score'] = obs['score']
        entry['profile']['capability_card']['benchmark_evidence'] = {
            **deepcopy(obs), 'score_is_correctness_probability': False,
            'effort_mapping': 'measured' if obs.get('effort') else 'reviewed_binding',
            'effort_evidence_url': entry.get('effort_evidence_url'),
        }
        if policy.get('score_role') in ('family_effort_prior', 'category_family_prior'):
            entry['profile']['capability_card']['benchmark_evidence'].update(
                effort_mapping='source_family_prior', evidence_role=policy['score_role'],
                api_snapshot_equivalence_proven=False)
        result['pools']['open_source'].append(entry)
        expiries.extend([timestamp(entry['catalog_checked_at']) + timedelta(hours=policy['max_catalog_age_hours']),
                         timestamp(obs['retrieved_at']) + timedelta(hours=policy['max_source_age_hours'])])
        if policy.get('score_role') in ('family_effort_prior', 'category_family_prior'):
            key = 'sql_qualification' if policy['score_role'] == 'family_effort_prior' else 'category_qualification'
            expiries.append(timestamp(entry[key]['valid_until']))
        if policy.get('max_evaluation_age_days') is not None:
            value = obs['evaluated_at']
            if len(value) == 10:
                value += 'T00:00:00Z'
            expiries.append(timestamp(value) + timedelta(days=policy['max_evaluation_age_days']))
    for pool in result['pools'].values():
        pool.sort(key=lambda entry: entry['config_id'])
    snapshot = {**base, 'pools': {tier: deepcopy(result['pools'][tier]) for tier in ('low', 'mid', 'high')},
                'open_source': deepcopy(result['pools']['open_source'][0]),
                'open_source_pool': deepcopy(result['pools']['open_source']),
                'category_id': category, 'valid_until': min(expiries).isoformat(),
                'excluded': deepcopy(result['excluded']),
                'input_sha256': digest({'observations': rows, 'registry': registry, 'policy': policy})}
    snapshot.pop('snapshot_id')
    snapshot['snapshot_id'] = digest(snapshot)
    result['snapshot'] = snapshot
    result['status'] = 'ready' if any(result['pools'][tier] for tier in ('low', 'mid', 'high')) else 'blocked'
    return result


def compile_category_matrix(observations, registries, policies, *, now):
    """Prepare 23 isolated, hash-bound four-pool views from reviewed bindings.

    Unknown classes and absent policies are blocked, never borrowed from coding.
    Caller-owned registries and previous snapshots are not changed. `ready` means
    a preparable snapshot only; task-specific QCS qualification is still required.
    """
    instant = timestamp(now)
    if not isinstance(observations, list) or not isinstance(registries, dict) or not isinstance(policies, dict):
        raise ValueError('Explicit observations list and category-keyed registries/policies required')
    if set(registries) - set(CATEGORY_SPECS) or set(policies) - set(CATEGORY_SPECS):
        raise ValueError('Unknown category in registry or policy')
    body = {'schema_version': SCHEMA, 'created_at': instant.isoformat(),
            'categories': {category: _compile_category(observations, registries.get(category),
                                                       policies.get(category), category, instant)
                           for category in CATEGORY_SPECS},
            'input_sha256': digest({'observations': observations, 'registries': registries, 'policies': policies})}
    return {**body, 'matrix_id': digest(body)}


def verify_category_matrix(matrix):
    """Verify all nested integrity without making unrelated freshness a gate."""
    if (not isinstance(matrix, dict) or matrix.get('schema_version') != SCHEMA
            or set(matrix.get('categories', {})) != set(CATEGORY_SPECS)):
        raise ValueError('Unsupported or incomplete category matrix')
    if matrix.get('matrix_id') != digest({k: v for k, v in matrix.items() if k != 'matrix_id'}):
        raise ValueError('Category matrix digest mismatch')
    timestamp(matrix['created_at'])
    for category, view in matrix['categories'].items():
        if not isinstance(view, dict) or view.get('status') not in ('ready', 'blocked'):
            raise ValueError('Invalid category status')
        if (not isinstance(view.get('pools'), dict)
                or set(view['pools']) != {'open_source', 'low', 'mid', 'high'}
                or any(not isinstance(pool, list) for pool in view['pools'].values())):
            raise ValueError('Category must preserve all four pools')
        snapshot = view.get('snapshot')
        if snapshot is None:
            if view['status'] != 'blocked' or any(view['pools'].values()):
                raise ValueError('Missing category snapshot')
            continue
        if not isinstance(snapshot, dict):
            raise ValueError('Invalid category snapshot')
        verify_snapshot(snapshot)
        if (snapshot.get('category_id') != category or snapshot['policy'] != view['policy']
                or _policy_reason(snapshot['policy'], category)):
            raise ValueError('Category snapshot scope mismatch')
        for tier in ('low', 'mid', 'high'):
            if view['pools'][tier] != snapshot['pools'][tier]:
                raise ValueError('Category pool differs from snapshot')
        if view['pools']['open_source'] != snapshot.get('open_source_pool'):
            raise ValueError('Category OSS pool differs from snapshot')
        if not view['pools']['open_source'] or snapshot['open_source'] != view['pools']['open_source'][0]:
            raise ValueError('Category OSS representative mismatch')
        if view['excluded'] != snapshot['excluded']:
            raise ValueError('Category exclusions differ from snapshot')
        spec = CATEGORY_SPECS[category]
        for pool in view['pools'].values():
            for entry in pool:
                card = entry['profile']['capability_card']
                evidence = entry.get('evidence', {})
                if evidence.get('evidence_role') == 'independent_category_qualification':
                    from .configuration import configuration_digest
                    if (entry not in view['pools']['open_source']
                            or card.get('category_id') != category
                            or evidence.get('category_id') != category
                            or card.get('category_source') != {
                                'basis': 'independent_category_qualification',
                                **{key: entry.get('category_qualification', {}).get(key) for key in
                                   ('category_id', 'configuration_sha256', 'report_id', 'valid_until')}}
                            or evidence != {'evidence_role': 'independent_category_qualification',
                                            'score_is_correctness_probability': False,
                                            **{key: entry.get('category_qualification', {}).get(key) for key in
                                               ('category_id', 'configuration_sha256', 'report_id', 'valid_until')}}
                            or evidence.get('configuration_sha256') != configuration_digest(entry)
                            or entry['profile'].get('external_score') is not None
                            or 'benchmark_evidence' in card):
                        raise ValueError('Independent OSS category mismatch')
                    continue
                if (card.get('category_id') != category
                        or evidence.get('category_id') != category
                        or evidence.get('source_model_id') != entry.get('source_model_id')
                        or evidence.get('source') != spec['source']
                        or evidence.get('source_version') != SOURCE_VERSION
                        or evidence.get('metric') != spec['metric']
                        or evidence.get('unit') != spec['unit']
                        or any(evidence.get('source_scope', {}).get(key) != spec.get(key)
                               for key in ('arena', 'category', 'benchmark_type'))):
                    raise ValueError('Candidate category mismatch')
        expected = 'ready' if any(view['pools'][tier] for tier in ('low', 'mid', 'high')) else 'blocked'
        if view['status'] != expected:
            raise ValueError('Category readiness mismatch')
