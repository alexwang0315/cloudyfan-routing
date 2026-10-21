"""Read-only broad discovery. Proposals are deliberately NOT executable pools.

Native benchmark priors, advertised prices and scoped provider metrics remain
separate from SQL qualification, full-path economics and enforced budget bounds.
"""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import re
from urllib.parse import quote

import httpx

from .adapters import _json, MAX_BYTES
from .catalog import canonical, digest, number, timestamp
from .metrics import normalize_provider_speed, normalize_provider_health, has_speed_observation
from ..routing import _reject_private

CATALOG_URL = 'https://openrouter.ai/api/v1/models'
_ID = re.compile(r'^~?[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+$')


def _decimal(value):
    if not isinstance(value, str):
        raise ValueError('Price requires decimal string')
    try:
        n = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid price') from None
    if not n.is_finite() or n < 0:
        raise ValueError('Invalid price')
    return n


def _price(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid pricing')
    _reject_private(value)
    dynamic = value.get('prompt') == '-1' and value.get('completion') == '-1'
    for key, amount in value.items():
        if key == 'overrides':
            if not isinstance(amount, list) or any(not isinstance(v, dict) for v in amount):
                raise ValueError('Invalid price overrides')
            # Preserve schedules verbatim; unresolved overrides disable price ranking.
            canonical(amount)
            for override in amount:
                for field, rate in override.items():
                    if field not in ('min_prompt_tokens', 'utc_days', 'utc_start', 'utc_end'):
                        _decimal(rate)
        elif key == 'discount':
            number(amount)  # Relative advertised discount can be negative (markup); never applied again.
        elif not (dynamic and key in ('prompt', 'completion')):
            _decimal(amount)
    return deepcopy(value)


def _scenario(price, policy):
    if (not price or price.get('overrides') or any(k not in price for k in ('prompt', 'completion'))
            or any(price[k] == '-1' for k in ('prompt', 'completion'))):
        return None
    return str(_decimal(price['prompt']) * policy['input_tokens']
               + _decimal(price['completion']) * policy['output_tokens'])


def catalog_inventory(raw, *, retrieved_at):
    """Project public fields; unknown source fields never enter classifier prompts."""
    timestamp(retrieved_at)
    rows, seen = [], set()
    for item in _json(raw)['data']:
        model = item['id']
        if not isinstance(model, str) or not _ID.fullmatch(model) or model in seen:
            raise ValueError('Invalid or duplicate model identity')
        seen.add(model)
        metrics = deepcopy(item.get('benchmarks') or {})
        _reject_private(metrics)
        aa = metrics.get('artificial_analysis') or {}
        for name in ('coding_index', 'intelligence_index', 'agentic_index'):
            if aa.get(name) is not None:
                number(aa[name])
        for row in metrics.get('design_arena') or []:
            for name in ('elo', 'win_rate', 'rank'):
                if row.get(name) is not None:
                    number(row[name])
        reasoning = deepcopy(item.get('reasoning'))
        if reasoning is not None:
            _reject_private(reasoning)
        hf = item.get('hugging_face_id') or None
        if hf is not None and (not isinstance(hf, str) or not _ID.fullmatch(hf)):
            raise ValueError('Invalid weights association')
        rows.append({'model_id': model, 'canonical_slug': item.get('canonical_slug'),
            'context_length': item.get('context_length'),
            'architecture': deepcopy(item.get('architecture') or {}),
            'supported_parameters': deepcopy(item.get('supported_parameters') or []),
            'reasoning': reasoning, 'benchmarks': metrics, 'catalog_price': _price(item['pricing']),
            'hugging_face_id': hf, 'expiration_date': item.get('expiration_date')})
    return {'schema_version': 'openrouter-inventory-v1', 'source_url': CATALOG_URL,
            'retrieved_at': retrieved_at, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'models': sorted(rows, key=lambda r: r['model_id'])}


def _policy(policy):
    if not isinstance(policy.get('id'), str) or not policy['id']:
        raise ValueError('Versioned discovery policy required')
    if policy['metric'] not in ('coding_index', 'intelligence_index', 'agentic_index'):
        raise ValueError('Explicit AA native metric required')
    if number(policy['low_mid']) >= number(policy['mid_high']):
        raise ValueError('Increasing native thresholds required')
    for name in ('input_tokens', 'output_tokens', 'ttl_hours'):
        if type(policy[name]) is not int or policy[name] <= 0:
            raise ValueError('Positive scenario and TTL required')
    if type(policy.get('max_endpoint_requests',600)) is not int or policy.get('max_endpoint_requests',600) <= 0:
        raise ValueError('Positive request bound required')
    gap = _decimal(policy['minimum_price_gap_fraction'])
    if gap >= 1:
        raise ValueError('Invalid gap')


def _exclusion(row, now):
    if all(row['catalog_price'].get(k) == '-1' for k in ('prompt', 'completion')):
        return 'dynamic_priced_router_not_fixed_model'
    if row['model_id'].startswith('~'):
        return 'virtual_unpinned_route'
    if row['model_id'].endswith(':batch'):
        return 'batch_not_synchronous'
    arch = row['architecture']
    if 'text' not in arch.get('input_modalities', []) or arch.get('output_modalities') != ['text']:
        return 'not_pure_text_interface'
    if row['expiration_date']:
        expiry = row['expiration_date']
        # Source date-only expiry is conservatively the start of that UTC day.
        if isinstance(expiry, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', expiry):
            expiry += 'T00:00:00Z'
        if timestamp(expiry) <= timestamp(now):
            return 'expired'
    return None


def _provider_rows(raw, model, policy):
    data = _json(raw)['data']
    if data['id'] != model:
        raise ValueError('Conflicting endpoint identity')
    endpoints = data['endpoints']
    tags = Counter(e.get('tag') for e in endpoints if isinstance(e, dict) and isinstance(e.get('tag'), str))
    rows, rejected = [], []
    for index, endpoint in enumerate(endpoints):
        try:
            tag = endpoint['tag']
            if (not isinstance(tag, str) or not tag or tags[tag] > 1
                    or endpoint.get('model_id') not in (None, model)):
                raise ValueError('Ambiguous endpoint identity')
            # Explicit public projection; supports_image_reference is not a hidden answer.
            speed = normalize_provider_speed(endpoint)
            price = _price(endpoint['pricing'])
            rows.append({'provider': tag, 'provider_name': endpoint.get('provider_name'),
                'endpoint_name': endpoint.get('name'), 'quantization': endpoint.get('quantization'),
                'service_tier': endpoint.get('service_tier'), 'pricing': price,
                'context_length': endpoint.get('context_length'),
                'supported_parameters': deepcopy(endpoint.get('supported_parameters') or []),
                'scenario_usd': _scenario(price, policy),
                'health': normalize_provider_health(endpoint),
                'speed': speed,
                'raw_sha256': hashlib.sha256(raw).hexdigest(), 'live_identity_verified': False})
        except (ValueError, KeyError, TypeError):
            rejected.append({'row_index': index, 'reason': 'invalid_or_ambiguous_provider_row'})
    return sorted(rows, key=lambda r: r['provider']), rejected


def build_proposal(inventory, endpoint_sources, policy):
    """Multi-candidate views, with honest unknowns and non-executable economics."""
    _policy(policy)
    now = inventory['retrieved_at']
    pools = {name: [] for name in ('oss', 'low', 'mid', 'high')}
    excluded, unassigned, all_candidates = [], [], []
    coverage = {key: 0 for key in ('intelligence_index', 'coding_index', 'agentic_index')}
    for row in inventory['models']:
        for key in coverage:
            coverage[key] += (row['benchmarks'].get('artificial_analysis') or {}).get(key) is not None
        reason = _exclusion(row, now)
        if reason:
            excluded.append({'model_id': row['model_id'], 'reason': reason})
            continue
        candidate = deepcopy(row)
        primary = (row['benchmarks'].get('artificial_analysis') or {}).get(policy['metric'])
        candidate['quality'] = {'source': 'openrouter_catalog:artificial_analysis',
            'metric': policy['metric'], 'value': primary, 'scale': 'source_native',
            'role': 'family_prior', 'effort': None, 'evaluated_at': None, 'sample_count': None}
        candidate['catalog_scenario_usd'] = _scenario(row['catalog_price'], policy)
        candidate['zero_token_price'] = all(row['catalog_price'].get(k) == '0' for k in ('prompt','completion'))
        candidate['zero_total_fee_guarantee'] = False
        candidate['open_weight_license_verified'] = False
        candidate['mac128_qualified'] = False
        candidate['sql_qualified'] = False
        candidate['economic_qualification'] = 'pending_full_path_and_classifier_evidence'
        candidate['production_eligible'] = False
        source = endpoint_sources.get(row['model_id'], {})
        candidate['endpoint_status'] = source.get('status')
        candidate['endpoint_error'] = source.get('error')
        candidate['endpoint_parse_error'] = None
        candidate['providers'] = []
        candidate['provider_rejections'] = []
        if source.get('status') == 200 and not source.get('error'):
            try:
                candidate['providers'], candidate['provider_rejections'] = _provider_rows(source['raw'], row['model_id'], policy)
            except (ValueError, KeyError, TypeError):
                candidate['endpoint_parse_error'] = 'invalid_endpoint_source'
        # Catalog quote is retained separately; provider analysis never borrows catalog speed/prices.
        candidate['economics'] = {'status': 'unknown_price_or_next_pool'}
        if primary is None:
            unassigned.append({'model_id': row['model_id'], 'reason': 'primary_quality_missing'})
            candidate['quality_tier'] = None
        else:
            tier = 'low' if primary < policy['low_mid'] else 'mid' if primary <= policy['mid_high'] else 'high'
            candidate['quality_tier'] = tier
            pools[tier].append(candidate)
        if row['hugging_face_id']:
            pools['oss'].append(candidate)
        all_candidates.append(candidate)
    # This is only a cheap preliminary price-gap screen, never a complete-path forecast.
    for lower, upper in (('low','mid'), ('mid','high')):
        upper_prices = [Decimal(c['catalog_scenario_usd']) for c in pools[upper]
                        if c['catalog_scenario_usd'] is not None]
        for candidate in pools[lower]:
            price = candidate['catalog_scenario_usd']
            if not upper_prices or price is None:
                continue
            next_price = min(upper_prices)
            gap = Decimal(policy['minimum_price_gap_fraction'])
            sufficient = next_price > 0 and Decimal(price) <= next_price * (1-gap)
            candidate['economics'] = {'status': 'price_gap_only' if sufficient else 'gap_not_demonstrated',
                'next_pool': upper, 'next_cheapest_catalog_scenario_usd': str(next_price),
                'minimum_price_gap_fraction': str(gap),
                'scope': 'unqualified_unrestricted_catalog_not_task_routing',
                'basis': 'advertised_input_output_scenario_only_not_complete_or_provider_bound'}
    return {'schema_version': 'openrouter-discovery-proposal-v1', 'created_at': now,
        'valid_until': (timestamp(now)+timedelta(hours=policy['ttl_hours'])).isoformat(),
        'policy': deepcopy(policy), 'policy_sha256': digest(policy),
        'catalog_sha256': inventory['raw_sha256'], 'catalog_url': inventory['source_url'],
        'production_activated': False,
        'pool_semantics': {'oss':'weights_repository_association_only_license_and_local_resources_pending',
                           'quality_tiers':'single_native_metric_prior_not_fixed_effort_SQL_qualification',
                           'overlap':'oss_can_overlap_quality_tiers_not_four_independent_arms'},
        'pools': pools,
        'unassigned': unassigned, 'excluded': excluded,
        'summary': {'catalog_models': len(inventory['models']), 'quality_coverage': coverage,
            'candidate_models': len(all_candidates), 'pools': {k:len(v) for k,v in pools.items()},
            'models_with_provider_data': sum(bool(c['providers']) for c in all_candidates),
            'provider_rows': sum(len(c['providers']) for c in all_candidates),
            'provider_rows_with_latency': sum(has_speed_observation(p['speed']['latency_last_30m'])
                                            for c in all_candidates for p in c['providers']),
            'provider_rows_with_throughput': sum(has_speed_observation(p['speed']['throughput_last_30m'])
                                               for c in all_candidates for p in c['providers']),
            'provider_rows_rejected': sum(len(c['provider_rejections']) for c in all_candidates),
            'endpoint_parse_failures': sum(c['endpoint_parse_error'] is not None for c in all_candidates)},
        'qualification_requirements': ['review_weights_license_and_local_resources_for_oss',
            'pin_configuration_effort_provider_and_version', 'independent_frozen_sql_exam',
            'exact_endpoint_workload_latency', 'complete_path_failure_classifier_and_repair_cost',
            'enforceable_component_budget_and_known_billing', 'qualified_snapshot_review_and_activation'],
        'candidate_models': all_candidates}


def _get(client, url, api_key=None):
    if url != CATALOG_URL and not url.startswith(CATALOG_URL+'/'):
        raise ValueError('Unexpected public source URL')
    # Construct Request directly: no inherited client credentials/cookies/auth or redirects.
    headers = {'Accept':'application/json', 'User-Agent':'CloudyFan-discovery/1'}
    if api_key:
        headers['Authorization'] = 'Bearer '+api_key
    request = httpx.Request('GET', url, headers=headers)
    chunks, size = [], 0
    try:
        response = client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    return {'status':response.status_code, 'error':'source_too_large', 'raw':b''.join(chunks),
                            'bytes_observed':size, 'truncated':True}
                chunks.append(chunk)
            return {'status':response.status_code, 'raw':b''.join(chunks)}
        finally:
            response.close()
    except httpx.HTTPError:
        return {'status':None, 'error':'transport_error', 'raw':b''.join(chunks),
                'bytes_observed':size,'truncated':bool(size)}


def _changes(proposal, previous):
    old = {r['model_id']:r for r in (previous or {}).get('candidate_models',[])}
    current = {r['model_id']:r for r in proposal['candidate_models']}
    def facts(row):
        result = {k:row.get(k) for k in ('canonical_slug','catalog_price','benchmarks','reasoning','expiration_date',
                                   'context_length','architecture','supported_parameters','hugging_face_id')}
        result['providers'] = [{k:v for k,v in p.items() if k != 'raw_sha256'} for p in row['providers']]
        result['coverage'] = {k:row.get(k) for k in ('endpoint_status','endpoint_error',
                                                   'endpoint_parse_error','provider_rejections')}
        return result
    proposal['changes'] = {'compared_to_sha256':digest(previous) if previous is not None else None,
        'added':sorted(current.keys()-old.keys()),'removed':sorted(old.keys()-current.keys()),
        'changed':sorted(k for k in current.keys() & old.keys() if facts(current[k]) != facts(old[k]))}


def refresh(client, output, policy, *, now, previous=None, api_key=None):
    """One immutable GET-only capture, no retries, no automatic activation.

    All catalogue text candidates get endpoint discovery, even missing primary
    scores, so quality gaps never hide potentially useful new providers/models.
    """
    _policy(policy)
    timestamp(now)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    def save(name, data):
        with (output/name).open('xb') as f:
            (output/name).chmod(0o600)
            f.write(data)
    receipts = []
    def fetch(url, filename):
        result = _get(client, url, api_key=api_key)
        save(filename, result['raw'])
        receipts.append({'method':'GET','url':url,'status':result['status'],
            'retrieved_at':now,'error':result.get('error'), 'file':filename,
            'bytes_observed':result.get('bytes_observed',len(result['raw'])),
            'truncated':result.get('truncated',False),'authenticated':bool(api_key),
            'sha256':hashlib.sha256(result['raw']).hexdigest()})
        save('requests.json' if len(receipts)==1 else f'request-{len(receipts):04d}.json', canonical(receipts[-1]))
        return result
    catalog = fetch(CATALOG_URL, 'models.raw')
    if catalog['status'] != 200 or catalog.get('error'):
        raise ValueError('Catalogue unavailable; previous snapshots untouched')
    inv = catalog_inventory(catalog['raw'], retrieved_at=now)
    endpoints = {}
    for index,row in enumerate(inv['models']):
        if _exclusion(row, now) is None:
            url = CATALOG_URL+'/'+quote(row['model_id'], safe='/:')+'/endpoints'
            if len(endpoints) >= policy.get('max_endpoint_requests',600):
                break
            endpoints[row['model_id']] = fetch(url, f'endpoint-{index:04d}.raw')
            if endpoints[row['model_id']]['status'] in (401,403,429):
                break  # Preserve partial coverage; do not hammer a rejecting endpoint.
    proposal = build_proposal(inv, endpoints, policy)
    _changes(proposal, previous)
    proposal['requests'] = {'get':len(receipts),'post':0,'automatic_retries':0,
                            'status_counts':{str(code):sum(r['status']==code for r in receipts)
                                             for code in set(r['status'] for r in receipts)}}
    save('inventory.json',canonical(inv))
    save('manifest.json',canonical({'retrieved_at':now,'requests':receipts,'policy':policy,
                                    'proposal_sha256':digest(proposal),'paid_model_calls':0}))
    save('proposal.json',canonical(proposal))
    return proposal


def replay_capture(capture, output, policy, *, previous=None):
    """Re-normalize hash-verified GET evidence without sending any new request."""
    import json
    capture, output = Path(capture), Path(output)
    record = _json((capture/'manifest.json').read_bytes())
    timestamp(record['retrieved_at'])
    sources = {}
    for request in record['requests']:
        filename = request['file']
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError('Unsafe source filename')
        raw = (capture/filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != request['sha256']:
            raise ValueError('Source hash mismatch')
        if request['method'] != 'GET':
            raise ValueError('Not a read-only capture')
        sources[request['url']] = {'raw':raw, 'status':request['status'], 'error':request.get('error')}
    source = sources[CATALOG_URL]
    if source['status'] != 200:
        raise ValueError('Catalogue source unavailable')
    inv = catalog_inventory(source['raw'], retrieved_at=record['retrieved_at'])
    endpoints = {row['model_id']:sources[url] for row in inv['models']
                 if (url := CATALOG_URL+'/'+quote(row['model_id'],safe='/:')+'/endpoints') in sources}
    proposal = build_proposal(inv, endpoints, policy)
    _changes(proposal, previous)
    proposal['requests'] = {'get':0, 'post':0, 'automatic_retries':0,
        'reused_capture_get':len(record['requests']), 'capture_manifest_sha256':digest(record)}
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name,value in [('inventory.json',inv),('proposal.json',proposal),
                        ('replay.json',{'capture_manifest_sha256':digest(record),
                                        'policy_sha256':digest(policy),'new_api_calls':0})]:
        with (output/name).open('x') as f:
            (output/name).chmod(0o600)
            json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.write('\n')
    return proposal
