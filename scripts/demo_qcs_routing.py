"""Synthetic public metrics -> QCS admission -> budgeted MockTransport routing.

No environment credentials, network transport or real model identifiers.
"""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import httpx

from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.receipt_store import _immutable
from cloudyfan_jev.scores.adapters import import_codexradar
from cloudyfan_jev.scores.catalog import compile_snapshot
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.demo import example_inputs
from cloudyfan_jev.scores.integration import ScoreCore
from cloudyfan_jev.scores.metrics import normalize_benchmarks, normalize_endpoints
from cloudyfan_jev.scores.qcs import prepare_qcs, classifier_contract_hash, flow_contract_hash, HORIZON


def interval(value):
    return {'lower': value, 'upper': value}


def demo(output, *, scenario='direct'):
    root = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(root / 'runtime') or scenario not in ('direct', 'classify'):
        raise ValueError('Fresh private runtime and explicit scenario required')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    now = datetime.now(timezone.utc)
    stamp, expiry = now.isoformat(), (now + timedelta(days=1)).isoformat()
    imported, registry, score_policy, requirements, decision = example_inputs(stamp)
    snapshot = compile_snapshot(import_codexradar(json.dumps(imported).encode(), retrieved_at=stamp),
                                registry, score_policy, now=stamp)
    entries = {t: snapshot['pools'][t][0] for t in ('low', 'mid', 'high')}
    entries['open_source'] = snapshot['open_source']
    provenance = {'basis': 'independent_calibration', 'workload_id': 'arithmetic-small',
        'source_url': 'https://example.org/synthetic-calibration', 'raw_sha256': 'a' * 64,
        'retrieved_at': stamp, 'valid_until': expiry}
    evidence = {}
    for tier, entry in entries.items():
        model, provider = entry['profile']['model_version'], entry['profile']['provider']
        benchmark = normalize_benchmarks(json.dumps({'meta': {'version': 'v1', 'source': 'artificial-analysis',
            'as_of': stamp}, 'data': [{'source': 'artificial-analysis', 'model_permaslug': model,
            'intelligence_index': 60, 'pricing': {'prompt': '0.000001', 'completion': '0.000002'}}]}).encode(),
            retrieved_at=stamp)
        endpoints = normalize_endpoints(json.dumps({'data': {'id': model, 'endpoints': [{
            'tag': provider, 'name': 'Synthetic | ' + model, 'provider_name': 'Example',
            'pricing': {'prompt': '0.000001', 'completion': '0.000002'},
            'latency_last_30m': .2, 'throughput_last_30m': 100}]}}).encode(), retrieved_at=stamp)
        cost = '0.01' if scenario == 'classify' and tier == 'low' else '0.1'
        evidence[entry['config_id']] = {'configuration_sha256': configuration_digest(entry),
            'provenance': dict(provenance), 'quality_sufficient': True,
            'generation_cost_usd': interval(cost),
            'complete_cost_usd': interval('0.2' if scenario == 'classify' and tier == 'low' else cost),
            'speed': {'metric': 'flow_ms', 'statistic': 'p95', 'upper': '100'},
            'source_metrics': benchmark + endpoints}
    stages = {}
    for stage, tiers in [('low_mid', ('low', 'mid')), ('mid_high:low', ('mid', 'high')),
                         ('mid_high:mid', ('mid', 'high'))]:
        stages[stage] = {'direct_tier': tiers[1], 'classifier_cost_usd': interval('0.001'),
            'classifier_ms_upper': '10', 'classify_flow_ms_upper': '110', 'lower_probability': interval('.9'),
            'paths': {t: {'cost_usd': evidence[t]['complete_cost_usd'], 'flow_ms_upper': '100'} for t in tiers}}
    policy = {'schema_version': 'qcs-policy-v1', 'workload_id': 'arithmetic-small',
        'speed_statistic': 'p95', 'max_flow_ms': '1000', 'minimum_saving_usd': '0.001',
        'minimum_saving_fraction': '0.01', 'calibration': {**provenance,
            'horizon': HORIZON,
            'configurations': {t: configuration_digest(e) for t, e in entries.items()},
            'includes': ['generation', 'failure', 'retry', 'repair', 'escalation', 'later_classification', 'cache', 'reasoning']},
        'stages': stages}
    policy['execution_contract'] = Core(None, {t: e['profile'] for t, e in entries.items()}, decision,
        limits=Limits(max_turns=1)).execution_contract_descriptor('qcs-mock-v1')

    def bind_synthetic_flow():
        policy['complete_policy_flow'] = {'metric': 'whole_task_flow_ms', 'statistic': 'p95', 'upper': '500',
            'provenance': dict(provenance),
            'contract_sha256': flow_contract_hash(snapshot, requirements, evidence, policy)}

    bind_synthetic_flow()
    if scenario == 'classify':
        initial = prepare_qcs(snapshot, requirements, evidence, policy, now=stamp)
        policy['classifier_version'] = decision['version']
        stages['low_mid']['classified_calibration'] = {**policy['calibration'],
            'stage': 'low_mid', 'previous_tier': 'open_source', 'context': 'public_failure_or_unknown',
            'classifier_version': decision['version'], 'effective_probability_includes_fallback': True,
            'contract_sha256': classifier_contract_hash(initial['profiles'], decision['version'], 'low_mid', 'open_source')}
        stages['low_mid']['classified_paths'] = {'low': {'cost_usd': interval('0.01')},
                                               'mid': {'cost_usd': interval('0.1')}}
        bind_synthetic_flow()
    plan = prepare_qcs(snapshot, requirements, evidence, policy, now=stamp)
    _immutable(output / 'plan.json', plan)
    calls = []

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body)
        if request.url.path.endswith('/decisions'):
            return httpx.Response(200, json={'id': f'gen-qcs-demo-{len(calls)}', 'model': body['model'],
                'provider': 'TypeSafe', 'usage': {'cost': .001, 'input_tokens': 1, 'output_tokens': 1},
                'answers': {'low_mid': {'type': 'choice', 'choice': 'low', 'probabilities': {'low': .9, 'mid': .1}}}})
        return httpx.Response(200, json={'id': f'gen-qcs-demo-{len(calls)}', 'model': body['model'],
            'provider': 'Example', 'usage': {'cost': '0.01'}, 'choices': [{'message': {
                'content': '3' if body['model'] == 'example/open_source' else '2'}}]})

    store = BudgetStore(output / 'budget.sqlite')
    store.authorize(Authorization('synthetic-only', Decimal('1'), expiry))
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        core = ScoreCore(Client(http, 'mock-only', output / 'client'), snapshot, requirements,
            decision, run_id='demo', clock=lambda: stamp, qcs_plan=plan, limits=Limits(max_turns=1))
        bounds = {p['model_version']: EnforcedBound('0.1', 'synthetic-bound') for p in core.profiles.values()}
        bounds[decision['version']] = EnforcedBound('0.1', 'synthetic-bound')
        engine = BudgetedCore(core, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
        task = {'prompt': 'Return only the integer sum of 1 and 1.', 'workload_id': 'arithmetic-small'}
        result = engine.run('demo', task, ArithmeticEnvironment, contract_version='qcs-mock-v1')
        before = len(calls)
        engine.run('demo', task, ArithmeticEnvironment, contract_version='qcs-mock-v1')
    summary = {'synthetic': True, 'scenario': scenario, 'status': result['status'],
        'tiers': [a['tier'] for a in result['attempts']], 'mock_posts': len(calls),
        'classifier_calls': result['selection']['classifier_calls'], 'replay_new_posts': len(calls)-before,
        'simulated_api_usd': result['costs']['api_usd'], 'simulated_reserved_usd': result['budget']['shared']['reservations'][0]['amount'],
        'real_api_cost_usd': '0', 'production_qualified': False}
    expected = ['open_source', 'low' if scenario == 'classify' else 'mid']
    if summary['tiers'] != expected or summary['replay_new_posts'] != 0 or result['status'] != 'passed':
        raise RuntimeError('Synthetic path diverged; retain original evidence')
    if scenario == 'classify' and result['selection']['decisions'][0].get('fallback') is not False:
        raise RuntimeError('Synthetic classification did not parse; retain original evidence')
    _immutable(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scenario', choices=('direct', 'classify'), default='direct')
    args = parser.parse_args()
    print(json.dumps(demo(args.output, scenario=args.scenario), indent=2))
