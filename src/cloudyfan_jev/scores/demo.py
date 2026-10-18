"""Offline score ingestion -> pools -> real orchestration with mocked HTTP."""
import argparse
import json
from pathlib import Path
import uuid

import httpx

from ..client import Client
from ..demo import ArithmeticEnvironment, example_config
from .adapters import import_codexradar
from .catalog import compile_snapshot
from .integration import ScoreCore, utc_now
from .snapshots import SnapshotStore


def example_inputs(now):
    """Synthetic, non-routable example IDs; never represents observed benchmark performance."""
    profiles, decision = example_config()
    policy = {'id': 'demo-coding-v1', 'domain': 'coding', 'source': 'codexradar',
              'source_version': 'synthetic-demo-v1', 'metric': 'overall', 'scale': 'source_native',
              'low_mid': 80, 'mid_high': 100, 'max_source_age_hours': 48, 'max_catalog_age_hours': 48,
              'snapshot_ttl_hours': 24, 'min_samples': 64, 'max_evaluation_age_days': 30}
    registry, rows = {'candidates': []}, []
    for tier in ('open_source', 'low', 'mid', 'high'):
        entry = {'config_id': tier, 'profile': profiles[tier], 'approved': True,
                 'context_tokens': 8192, 'capabilities': ['tools'], 'catalog_checked_at': now,
                 'estimated_request_usd': None, 'estimated_latency_ms': None,
                 'source_model_id': 'example/' + tier, 'source': 'codexradar',
                 'source_version': 'synthetic-demo-v1'}
        if tier == 'open_source':
            registry['open_source'] = {**entry, 'open_weight': True}
        else:
            registry['candidates'].append(entry)
            rows.append({'model_id': entry['source_model_id'], 'effort': 'low',
                         'score': profiles[tier]['external_score'], 'sample_count': 64,
                         'evaluated_at': now[:10]})
    imported = {'kind': 'model_benchmark', 'source_url': 'https://deng.codexradar.com/',
                'source_version': 'synthetic-demo-v1', 'rows': rows, 'notice': 'SYNTHETIC OFFLINE FIXTURE'}
    requirements = {'domain': 'coding', 'capabilities': ['tools'], 'context_tokens': 1000}
    return imported, registry, policy, requirements, decision


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synthetic score-to-routing smoke; no real API or API keys')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--route', choices=['pass', 'low', 'mid', 'high'], default='high')
    args = parser.parse_args(argv)
    root = args.output or Path('runtime') / ('scores-demo-' + uuid.uuid4().hex[:10])
    root.mkdir(parents=True, exist_ok=False)
    now = utc_now()
    imported, registry, policy, requirements, decision = example_inputs(now)
    observations = import_codexradar(json.dumps(imported).encode(), retrieved_at=now)
    for name, value in [('codexradar-import', imported), ('registry', registry), ('policy', policy),
                        ('requirements', requirements), ('observations', observations)]:
        (root / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    snapshot = compile_snapshot(observations, registry, policy, now=now)
    store = SnapshotStore(root / 'snapshots')
    store.publish(snapshot, now=now)
    requests = []

    def fake_api(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(body['questions']))
            choice = ('low' if args.route == 'low' else 'mid') if stage == 'low_mid' else args.route
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            return httpx.Response(200, json={'id': f'gen-score-demo-{len(requests)}', 'model': body['model'],
                'provider': 'TypeSafe', 'usage': {'cost': .0001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {stage: {'type': 'choice', 'choice': choice,
                                   'probabilities': {k: .8 if k == choice else .2 for k in pair}}}})
        answer = '2' if args.route == 'pass' or body['model'] != 'example/open_source' else '3'
        return httpx.Response(200, json={'id': f'gen-score-demo-{len(requests)}', 'model': body['model'],
            'provider': 'Example', 'usage': {'cost': .001, 'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]})

    with httpx.Client(transport=httpx.MockTransport(fake_api)) as http:
        core = ScoreCore(Client(http, 'offline-mock-only', root / 'run'), store.load(now=now), requirements,
                         decision, run_id='demo')
        result = core.run('demo', {'prompt': 'Return only the integer sum of 1 and 1.'},
                          ArithmeticEnvironment, contract_version='arithmetic-demo-v1')
    report = {'synthetic': True, 'status': result['status'], 'snapshot_id': snapshot['snapshot_id'],
              'route': args.route, 'mock_requests': len(requests), 'real_api_calls': 0, 'real_api_cost_usd': 0,
              'simulated_costs': result['costs'], 'output': str(root)}
    (root / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0
