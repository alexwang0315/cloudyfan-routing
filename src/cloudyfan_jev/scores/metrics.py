"""Pure three-dimensional public source adapters; no fetching or qualification.

Rows retain source scope. In particular benchmark prices do not acquire an
endpoint identity, and provider throughput does not become task latency.
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib

from .adapters import _json
from .catalog import digest, number, timestamp
from ..routing import _reject_private

BENCHMARK_URL = 'https://openrouter.ai/api/v1/benchmarks'
UPTIME_FIELDS = ('uptime_last_5m', 'uptime_last_30m', 'uptime_last_1d')


def normalize_provider_health(endpoint):
    """Retain the three native percent windows; absent status remains unknown.

    The documented status contract only distinguishes integer zero from nonzero.
    No undocumented mapping of particular degraded codes is inferred.
    """
    if not isinstance(endpoint, dict):
        raise ValueError('Provider health requires an object')
    status = endpoint.get('status')
    if status is not None and type(status) is not int:
        raise ValueError('Provider status must be an integer or unknown')
    if endpoint.get('uptime_unit', 'percent') != 'percent':
        raise ValueError('Provider uptime unit must be percent')
    units = endpoint.get('units', {})
    if not isinstance(units, dict) or any(units.get(k, 'percent') != 'percent' for k in UPTIME_FIELDS):
        raise ValueError('Provider uptime units must be percent')
    values = {}
    for field in UPTIME_FIELDS:
        value = endpoint.get(field)
        if value is not None and not 0 <= number(value) <= 100:
            raise ValueError('Provider uptime must be within 0 to 100 percent')
        values[field] = value
    return {'status': status,
            'status_meaning': 'unknown' if status is None else 'operational' if status == 0 else 'degraded',
            **values, 'uptime_unit': 'percent', 'units': dict.fromkeys(UPTIME_FIELDS, 'percent'),
            'scope': 'provider_rolling_availability'}


def _amount(value):
    if not isinstance(value, str):
        raise ValueError('Prices require explicit decimal strings')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid price') from None
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid price')
    return value


def _pricing(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Pricing must be an object or unknown')
    for key, amount in value.items():
        if key == 'overrides':
            if not isinstance(amount, list):
                raise ValueError('Price overrides require a list')
            for override in amount:
                schedule_keys = {'min_prompt_tokens','utc_days','utc_start','utc_end'}
                if not isinstance(override,dict) or not schedule_keys.intersection(override):
                    raise ValueError('Invalid override threshold')
                if 'min_prompt_tokens' in override and (type(override['min_prompt_tokens']) is not int or override['min_prompt_tokens'] < 0):
                    raise ValueError('Invalid override threshold')
                if 'utc_days' in override and (not isinstance(override['utc_days'],list) or not override['utc_days']
                        or any(day not in ('monday','tuesday','wednesday','thursday','friday','saturday','sunday') for day in override['utc_days'])):
                    raise ValueError('Invalid price schedule days')
                for field in ('utc_start','utc_end'):
                    if field in override and (type(override[field]) is not int or not 0 <= override[field] < 2400 or override[field] % 100 >= 60):
                        raise ValueError('Invalid price schedule time')
                _pricing({k: v for k, v in override.items() if k not in schedule_keys})
        elif key == 'discount':
            number(amount)  # Relative markup can be negative; quoted rates already include it.
        else:
            _amount(amount)
    return deepcopy(value)



def normalize_provider_speed(endpoint):
    """Authenticated API publishes percentile objects; bare legacy units stay unknown."""
    values = {}
    object_seen = False
    scalar_seen = False
    statistics = {}
    for field in ('latency_last_30m', 'throughput_last_30m'):
        value = endpoint.get(field)
        if isinstance(value, dict):
            object_seen = True
            if any(k not in ('p50','p75','p90','p99') for k in value):
                raise ValueError('Unknown speed percentile')
            for n in value.values():
                if n is not None and number(n) < 0:
                    raise ValueError('Negative speed percentile')
        elif value is not None:
            scalar_seen = True
            if number(value) < 0:
                raise ValueError('Negative speed metric')
        values[field] = deepcopy(value)
        statistics[field] = 'percentiles' if isinstance(value,dict) else None
    return {**values, 'statistic':'mixed' if object_seen and scalar_seen else 'percentiles' if object_seen else None,
            'statistics':statistics,
            'scope':'provider_rolling_30m_all_workloads',
            'latency_unit':'milliseconds' if isinstance(values['latency_last_30m'],dict) else None,
            'throughput_unit':'tokens/second'}


def has_speed_observation(value):
    return any(v is not None for v in value.values()) if isinstance(value,dict) else value is not None


def _base(raw, retrieved_at, source, version, url):
    timestamp(retrieved_at)
    return {'schema_version': 'qcs-source-v1', 'source': source, 'source_version': version,
            'source_url': url, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'retrieved_at': retrieved_at}


def normalize_benchmarks(raw, *, retrieved_at, quarantine_ambiguous=False):
    body = _json(raw)
    _reject_private(body)
    meta = body['meta']
    if meta.get('version') != 'v1' or not isinstance(body['data'], list):
        raise ValueError('Unsupported benchmark schema')
    if meta.get('as_of') is not None:
        timestamp(meta['as_of'])
    def identity(item):
        return (item.get('source'), item.get('model_permaslug'),item.get('reasoning_effort'),
                item.get('arena'),item.get('category'),item.get('benchmark_type'),
                item.get('search_engine'),item.get('search_surface'),item.get('primary_metric'),
                item.get('display_name'),digest(item.get('run_config') or {}))
    counts = Counter(identity(item) for item in body['data'])
    rows = []
    for index,item in enumerate(body['data']):
        source = item['source']
        if source not in ('artificial-analysis', 'design-arena', 'openrouter') or meta.get('source') not in (None, source):
            raise ValueError('Unsupported or conflicting benchmark source')
        model = item['model_permaslug']
        if not isinstance(model, str) or not model.strip():
            raise ValueError('Explicit source model required')
        ambiguous = counts[identity(item)] > 1
        if ambiguous and not quarantine_ambiguous:
            raise ValueError('Ambiguous duplicate metric row')
        quality = {}
        names = (('intelligence_index', 'coding_index', 'agentic_index') if source == 'artificial-analysis'
                 else ('elo', 'win_rate') if source == 'design-arena' else ('accuracy',))
        for name in names:
            if item.get(name) is not None:
                quality[name] = number(item[name])
        speed = None
        if source == 'design-arena' and item.get('avg_generation_time_ms') is not None:
            value = number(item['avg_generation_time_ms'])
            if value < 0:
                raise ValueError('Negative generation time')
            speed = {'metric': 'generation_time_ms', 'value': value, 'statistic': 'mean'}
        extra = {}
        if source == 'openrouter':
            metric = item.get('primary_metric')
            if metric is not None:
                if metric not in ('accuracy','f1_by_item'):
                    raise ValueError('Unknown primary benchmark metric')
                if metric in quality and quality[metric] != number(item['primary_score']):
                    raise ValueError('Conflicting benchmark scores')
                quality[metric] = number(item['primary_score'])
            if any(not 0 <= v <= 1 for v in quality.values()):
                raise ValueError('Benchmark score outside native ratio')
            count = item.get('total_tasks')
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError('Invalid benchmark coverage')
            if item.get('last_run_timestamp') is not None:
                timestamp(item['last_run_timestamp'])
            for name in ('avg_cost_per_task','avg_latency_per_task_ms','accuracy_stddev'):
                if item.get(name) is not None and number(item[name]) < 0:
                    raise ValueError('Invalid benchmark measurement')
            run_config = deepcopy(item.get('run_config') or {})
            if not isinstance(run_config,dict) or any(k not in ('max_agent_turns','reasoning_effort','temperature') for k in run_config):
                raise ValueError('Unexpected benchmark configuration')
            effort = run_config.get('reasoning_effort')
            if effort is not None and (not isinstance(effort,str) or not effort.strip()):
                raise ValueError('Invalid benchmark effort')
            if (effort is not None and item.get('reasoning_effort') is not None
                    and effort != item['reasoning_effort']):
                raise ValueError('Conflicting benchmark effort')
            if run_config.get('max_agent_turns') is not None and (type(run_config['max_agent_turns']) is not int or run_config['max_agent_turns'] < 1):
                raise ValueError('Invalid benchmark agent turns')
            if run_config.get('temperature') is not None:
                number(run_config['temperature'])
            if not isinstance(item['benchmark_type'],str) or not item['benchmark_type'].strip():
                raise ValueError('Explicit benchmark type required')
            extra = {'benchmark_type':item['benchmark_type'],'sample_count':count,
                     'evaluated_at':item.get('last_run_timestamp'),'run_config':run_config,
                     'search_engine':item.get('search_engine'),'search_surface':item.get('search_surface'),
                     'accuracy_stddev':item.get('accuracy_stddev'),
                     'benchmark_cost':({'value':str(item['avg_cost_per_task']),'unit':'USD/task',
                                        'basis':'source_benchmark_mean_not_current_cost_or_upper_bound'}
                                       if item.get('avg_cost_per_task') is not None else None)}
            if item.get('avg_latency_per_task_ms') is not None:
                speed={'metric':'task_latency_ms','value':item['avg_latency_per_task_ms'],'statistic':'mean'}
        rows.append({**_base(raw, retrieved_at, source, 'openrouter-benchmarks-v1', BENCHMARK_URL),
            'as_of': meta.get('as_of'), 'upstream_url': meta.get('source_url'),
            'model_id': model, 'effort': item.get('reasoning_effort') or (item.get('run_config') or {}).get('reasoning_effort'), 'provider': None,
            'source_variant_label':item.get('display_name'),'source_row_index':index,
            'binding_status':'ambiguous_source_identity' if ambiguous else 'unreviewed_source_variant',
            'arena': item.get('arena'), 'category': item.get('category'), 'quality': quality,
            'pricing': _pricing(item.get('pricing')), 'price_unit': 'USD/token', 'speed': speed, **extra})
    return rows


def normalize_endpoints(raw, *, retrieved_at):
    body = _json(raw)
    guarded = deepcopy(body)
    for endpoint in guarded['data']['endpoints']:
        for field in ('supports_image_reference','supports_multiple_audio_references'):
            if field in endpoint:
                if type(endpoint.pop(field)) is not bool:
                    raise ValueError('Invalid public capability flag')
    _reject_private(guarded)
    data = body['data']
    model = data['id']
    if not isinstance(model, str) or len(model.split('/')) != 2:
        raise ValueError('Explicit catalog model ID required')
    rows, seen = [], set()
    for endpoint in data['endpoints']:
        if endpoint.get('model_id') not in (None, model):
            raise ValueError('Conflicting endpoint model identity')
        tag = endpoint['tag']
        if not isinstance(tag, str) or not tag or tag in seen:
            raise ValueError('Missing or ambiguous endpoint identity')
        seen.add(tag)
        speed = normalize_provider_speed(endpoint)
        rows.append({**_base(raw, retrieved_at, 'openrouter-endpoints', 'public-catalog',
                            f'https://openrouter.ai/api/v1/models/{model}/endpoints'),
            'model_id': model, 'endpoint_name': endpoint.get('name'), 'provider': tag,
            'provider_name': endpoint.get('provider_name'), 'service_tier': endpoint.get('service_tier'),
            'quantization': endpoint.get('quantization'), 'pricing': _pricing(endpoint.get('pricing')),
            'price_unit': 'USD/token; request fields USD/request', 'quality': None,
            'health': normalize_provider_health(endpoint),
            'speed': {**speed, 'units': {'latency_last_30m': speed['latency_unit'],
                                                        'throughput_last_30m': 'tokens/second'}},
            'live_identity_verified': False})
    return rows
