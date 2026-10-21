"""23 category four-pool MockTransport demo; no credentials or real API."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx

from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.receipt_store import _immutable
from cloudyfan_jev.scores.categories import CATEGORY_SPECS, compile_category_matrix
from cloudyfan_jev.scores.category_routing import CategoryCore, prepare_category_selection
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.qcs import COMPLETE_PATH, HORIZON, flow_contract_hash


def demo(output):
    output = Path(output).resolve()
    root = Path(__file__).resolve().parents[1]
    if not output.is_relative_to(root / 'runtime'):
        raise ValueError('Private fresh runtime output required')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    now = datetime.now(timezone.utc)
    stamp, expiry = now.isoformat(), (now + timedelta(days=1)).isoformat()
    profiles, decision = example_config()
    registries, policies, rows = {}, {}, []
    for category, spec in CATEGORY_SPECS.items():
        # These explicit fixture thresholds are not real-source recommendations.
        cuts, scores = ((.5, .8), (.1, .2, .6, .95)) if spec['unit'] == 'ratio0..1' else (
            ((1200, 1400), (1000, 1100, 1300, 1500)) if spec['unit'] == 'Elo' else ((30, 60), (10, 20, 45, 80)))
        policies[category] = {'id': category + '-synthetic-v1', 'domain': category,
            'source': spec['source'], 'source_version': 'openrouter-benchmarks-v1', 'metric': spec['metric'],
            'scale': 'source_native', 'low_mid': cuts[0], 'mid_high': cuts[1],
            'max_source_age_hours': 48, 'max_catalog_age_hours': 48, 'snapshot_ttl_hours': 24, 'min_samples': 0}
        registry = {'candidates': [], 'open_source_candidates': []}
        for tier, score in zip(('open_source', 'low', 'mid', 'high'), scores, strict=True):
            row = {'schema_version': 'qcs-source-v1', 'source': spec['source'],
                'source_version': 'openrouter-benchmarks-v1', 'model_id': profiles[tier]['model'],
                'source_url': 'https://example.org/synthetic-source', 'raw_sha256': 'a'*64,
                'source_row_index': len(rows), 'quality': {spec['metric']: score}, 'effort': 'low',
                'retrieved_at': stamp, 'as_of': stamp,
                **{k:spec[k] for k in ('arena', 'category', 'benchmark_type') if k in spec}}
            entry = {'config_id': tier, 'profile': profiles[tier], 'approved': True,
                'context_tokens': 8192, 'capabilities': ['tools'], 'catalog_checked_at': stamp,
                'source_model_id': row['model_id'], 'source': spec['source'], 'source_version': row['source_version'],
                'observation_binding': {'raw_sha256': row['raw_sha256'], 'source_row_index': row['source_row_index'],
                    'reviewed': True, 'evidence_url': 'https://example.org/synthetic-review'}}
            if tier == 'open_source':
                entry['open_weight'] = True
                registry['open_source_candidates'].append(entry)
            else:
                registry['candidates'].append(entry)
            rows.append(row)
        registries[category] = registry
    matrix = compile_category_matrix(rows, registries, policies, now=stamp)
    _immutable(output / 'synthetic-matrix.json', matrix)
    summaries = []
    interval = lambda value: {'lower': value, 'upper': value}
    for index, category in enumerate(CATEGORY_SPECS):
        snapshot = matrix['categories'][category]['snapshot']
        entries = {t:matrix['categories'][category]['pools'][t][0] for t in profiles}
        provenance = {'basis': 'independent_calibration', 'category_id': category,
            'workload_id': 'synthetic-arithmetic-only', 'source_url': 'https://example.org/synthetic-calibration',
            'raw_sha256': 'b'*64, 'retrieved_at': stamp, 'valid_until': expiry}
        evidence = {t:{'configuration_sha256': configuration_digest(entry), 'provenance': dict(provenance),
            'quality_sufficient': True, 'generation_cost_usd': interval('.1'), 'complete_cost_usd': interval('.1'),
            'speed': {'metric': 'flow_ms', 'statistic': 'p95', 'upper': '100'}, 'source_metrics': []}
            for t, entry in entries.items()}
        stages = {key:{'direct_tier': tiers[1], 'classifier_cost_usd': interval('.01'), 'classifier_ms_upper': '10',
            'lower_probability': interval('.5'),
            'paths': {t:{'cost_usd': interval('.1'), 'flow_ms_upper': '100'} for t in tiers}}
            for key, tiers in [('low_mid', ('low','mid')), ('mid_high:low', ('mid','high')), ('mid_high:mid', ('mid','high'))]}
        requirements = {'domain': category, 'capabilities': ['tools'], 'context_tokens': 1000}
        policy = {'schema_version': 'qcs-policy-v1', 'category_id': category, 'workload_id': provenance['workload_id'],
            'speed_statistic': 'p95', 'max_flow_ms': '1000', 'minimum_saving_usd': '.001', 'minimum_saving_fraction': '.01',
            'calibration': {**provenance, 'configurations': {t:configuration_digest(e) for t,e in entries.items()},
                'includes': sorted(COMPLETE_PATH), 'horizon': HORIZON}, 'stages': stages,
            'execution_contract': Core(None, profiles, decision, limits=Limits(max_turns=1)).execution_contract_descriptor('category-mock-v1')}
        policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': 'p95', 'upper': '500',
            'provenance': dict(provenance), 'contract_sha256': flow_contract_hash(snapshot, requirements, evidence, policy)}
        prepared = prepare_category_selection(matrix, category, requirements, evidence, policy, now=stamp)
        calls = []
        def upstream(request):
            body = json.loads(request.content)
            calls.append(body)
            if request.url.path.endswith('/decisions'):
                raise AssertionError('Unprofitable classification must not dispatch')
            return httpx.Response(200, json={'id': f'gen-category-{index}-{len(calls)}', 'model': body['model'],
                'provider': 'Example', 'usage': {'cost': '.01'},
                'choices': [{'message': {'content': '3' if len(calls)==1 else '2'}}]})
        run_id = f'category-{index}'
        with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
            core = CategoryCore(Client(http, 'mock-only', output / 'client'), matrix, category,
                requirements, evidence, policy, decision, run_id=run_id, clock=lambda:stamp, limits=Limits(max_turns=1))
            task = {'prompt': 'Return only 1+1.', 'workload_id': provenance['workload_id']}
            result = core.run(run_id, task, ArithmeticEnvironment, contract_version='category-mock-v1')
            before = len(calls)
            core.run(run_id, task, ArithmeticEnvironment, contract_version='category-mock-v1')
        if result['status'] != 'passed' or len(calls) != 2 or len(calls)-before:
            raise AssertionError('Category mock path/replay diverged')
        summaries.append({'category_id': category, 'status': result['status'],
            'pools': {t:len(p) for t,p in matrix['categories'][category]['pools'].items()},
            'selected': {t:p['model'] for t,p in prepared['qcs_plan']['profiles'].items()},
            'mock_posts': len(calls), 'classifier_calls': result['selection']['classifier_calls'],
            'simulated_api_usd': result['costs']['api_usd'], 'replay_new_posts': len(calls)-before})
    summary = {'synthetic': True, 'categories': summaries, 'category_count': len(summaries),
        'real_api_cost_usd': '0', 'production_qualified': False,
        'limitation': 'Arithmetic fixtures exercise category plumbing only, not category-specific real model capability.'}
    _immutable(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(demo(args.output), indent=2))
