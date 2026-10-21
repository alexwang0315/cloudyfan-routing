#!/usr/bin/env python3
"""Synthetic 23-category, 4-pool, 3-model demo. No network or real qualification."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import uuid

import httpx

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.scores import (BackupCategoryCore, HealthStore, backup_flow_contract_hash,
                                 compile_category_matrix, prepare_backup_pools)
from cloudyfan_jev.scores.backup_pools import TIERS
from cloudyfan_jev.scores.catalog import digest
from cloudyfan_jev.scores.categories import CATEGORY_SPECS
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.metrics import normalize_provider_health, normalize_provider_speed

NOW = '2026-10-21T00:00:00+00:00'
UNTIL = '2026-10-22T00:00:00+00:00'
HEALTH = {'ttl_seconds': 300, 'failure_threshold': 2, 'cooldown_seconds': 60,
          'min_uptime_percent': 99, 'latency_percentile': 'p99', 'max_latency_ms': 10000,
          'throughput_percentile': 'p50', 'min_throughput': 10}


def fixture(category):
    """Invented example.org identities/provenance are solely simulation inputs."""
    spec = CATEGORY_SPECS[category]
    profiles, decision = example_config()
    thresholds, scores = (([1200, 1400], [900, 1000, 1300, 1500]) if spec['source'] == 'design-arena'
        else ([.5, .8], [.1, .2, .6, .95]) if spec['source'] == 'openrouter'
        else ([30, 60], [10, 20, 45, 80]))
    score_policy = {'id': 'synthetic-' + category, 'domain': category, 'source': spec['source'],
        'source_version': 'openrouter-benchmarks-v1', 'metric': spec['metric'], 'scale': 'source_native',
        'low_mid': thresholds[0], 'mid_high': thresholds[1], 'max_source_age_hours': 48,
        'max_catalog_age_hours': 48, 'snapshot_ttl_hours': 24, 'min_samples': 0}
    rows, registry, evidence = [], {'candidates': [], 'open_source_candidates': []}, {}
    provenance = {'basis': 'independent_calibration', 'workload_id': 'synthetic-backup-demo',
        'category_id': category, 'source_url': 'https://example.org/offline-only',
        'raw_sha256': 'a' * 64, 'retrieved_at': NOW, 'valid_until': UNTIL}
    for tier, score in zip(TIERS, scores):
        for n in range(3):
            model = f'example/{tier}-{n}'
            profile = deepcopy(profiles[tier])
            profile.update(model=model, model_version=model, accepted_models=[model],
                provider=f'provider-{n}', provider_name=f'Provider {n}')
            index = len(rows)
            row = {'schema_version': 'qcs-source-v1', 'source': spec['source'],
                'source_version': 'openrouter-benchmarks-v1', 'source_url': 'https://example.org/scores',
                'model_id': model, 'quality': {spec['metric']: score}, 'effort': 'low',
                'retrieved_at': NOW, 'as_of': NOW, 'raw_sha256': 'a' * 64, 'source_row_index': index,
                'binding_status': 'unreviewed_source_variant', 'arena': spec.get('arena'),
                'category': spec.get('category'), 'benchmark_type': spec.get('benchmark_type')}
            entry = {'config_id': f'{tier}-{n}', 'profile': profile, 'approved': True,
                'context_tokens': 8192, 'capabilities': ['tools'], 'catalog_checked_at': NOW,
                'source_model_id': model, 'source': spec['source'], 'source_version': row['source_version'],
                'model_family': f'family-{n}', 'observation_binding': {'raw_sha256': 'a' * 64,
                    'source_row_index': index, 'reviewed': True, 'evidence_url': 'https://example.org/review'}}
            if tier == 'open_source': entry['open_weight'] = True
            registry['open_source_candidates' if tier == 'open_source' else 'candidates'].append(entry)
            rows.append(row)
            evidence[entry['config_id']] = {'configuration_sha256': configuration_digest(entry),
                'provenance': deepcopy(provenance), 'quality_sufficient': True,
                'generation_cost_usd': {'lower': '.01', 'upper': '.01'},
                'complete_cost_usd': {'lower': '.10', 'upper': '.10'},
                'speed': {'metric': 'flow_ms', 'statistic': 'p95', 'upper': '100'}, 'source_metrics': []}
    matrix = compile_category_matrix(rows, {category: registry}, {category: score_policy}, now=NOW)
    policy = {'schema_version': 'qcs-backup-policy-v1', 'workload_id': 'synthetic-backup-demo',
        'category_id': category, 'speed_statistic': 'p95', 'max_flow_ms': '100000', 'stages': {}}
    plan = prepare_backup_pools(matrix, category,
        {'domain': category, 'capabilities': ['tools'], 'context_tokens': 1000}, evidence, policy, now=NOW)
    return plan, decision, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output or Path('runtime') / ('backup-demo-' + uuid.uuid4().hex)
    report = {'offline': True, 'synthetic_only': True, 'real_api_cost_usd': '0', 'categories': []}
    instant = datetime.fromisoformat(NOW)
    for index, category in enumerate(CATEGORY_SPECS):
        root = output / str(index)
        plan, decision, provenance = fixture(category)
        health = HealthStore(root / 'health', policy=HEALTH, clock=lambda: instant)
        endpoint = {'status': 0, 'uptime_last_5m': 100, 'uptime_last_30m': 100, 'uptime_last_1d': 100,
                    'latency_last_30m': {'p99': 100}, 'throughput_last_30m': {'p50': 100}}
        health.publish([{'configuration_sha256': r['configuration_sha256'],
            'provider': r['entry']['profile']['provider'], 'observed_at': NOW,
            'health': normalize_provider_health(endpoint), 'speed': normalize_provider_speed(endpoint)}
            for pool in plan['pools'].values() for r in pool])
        policy = plan['qcs_policy']
        policy['health_policy'] = deepcopy(health.policy)
        profiles = {tier: pool[0]['entry']['profile'] for tier, pool in plan['pools'].items()}
        policy['execution_contract'] = Core(None, profiles, decision,
            limits=Limits(max_turns=1)).execution_contract_descriptor('integer-sum-v1')
        policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': 'p95',
            'upper': '10000', 'provenance': provenance, 'contract_sha256': backup_flow_contract_hash(plan)}
        plan['backup_id'] = digest({k: v for k, v in plan.items() if k != 'backup_id'})
        calls = []
        def fake(request):
            assert request.method == 'POST'
            payload = json.loads(request.content)
            calls.append(payload)
            body = {'id': f'gen-synthetic-{index}-{len(calls)}', 'model': payload['model'],
                'provider': 'Provider ' + payload['provider']['only'][0].rsplit('-', 1)[1],
                'usage': {'cost': '.01'}}
            if len(calls) == 1:
                body['error'] = {'code': 503, 'metadata': {'provider_name': body['provider']}}
                return httpx.Response(503, json=body)
            body['choices'] = [{'message': {'role': 'assistant', 'content': '2'}}]
            return httpx.Response(200, json=body)
        with httpx.Client(transport=httpx.MockTransport(fake)) as http:
            client = Client(http, 'offline-mock-only', root / 'receipts')
            core = BackupCategoryCore(client, plan, decision, run_id='demo', health_store=health,
                clock=lambda: instant, limits=Limits(max_turns=1))
            task = {'prompt': 'Return 1+1.', 'category': category, 'workload_id': 'synthetic-backup-demo'}
            result = core.run('demo', task, ArithmeticEnvironment, contract_version='integer-sum-v1')
            before = len(calls)
            # Expired health on replay must not trigger a new paid request.
            instant += timedelta(minutes=10)
            replay = core.run('demo', task, ArithmeticEnvironment, contract_version='integer-sum-v1')
            instant -= timedelta(minutes=10)
            assert replay['status'] == result['status'] == 'passed' and len(calls) == before == 2
        report['categories'].append({'category_id': category, 'pool_counts': plan['coverage']['counts'],
            'status': result['status'], 'configs': [a['config_id'] for a in result['attempts']],
            'mock_posts': len(calls), 'replay_added_posts': len(calls)-before,
            'simulated_api_usd': result['costs']['api_usd']})
    output.mkdir(parents=True, exist_ok=True)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'offline': True, 'real_api_cost_usd': '0', 'categories': len(report['categories']),
        'models_per_pool': 3, 'mock_posts': sum(r['mock_posts'] for r in report['categories']),
        'replay_added_posts': 0, 'passed': all(r['status'] == 'passed' for r in report['categories']),
        'report': str(output / 'report.json')}, indent=2))


if __name__ == '__main__':
    main()
