"""Offline HTTP simulation of the real core. Never reads API credentials."""
import argparse
import json
from pathlib import Path
import uuid

import httpx

from .client import Client
from .engine import Core


def example_config():
    profiles = {tier: {'model': 'example/' + tier, 'model_version': 'example/' + tier,
                      'accepted_models': ['example/' + tier], 'provider': 'example',
                      'provider_name': 'Example', 'effort': 'low', 'capability_card': {},
                      'external_score': score}
                for tier, score in [('open_source', None), ('low', 60), ('mid', 90), ('high', 110)]}
    decision = {'model': 'typesafe/jev-1.13', 'version': 'typesafe/jev-1.13',
                'provider': 'typesafe', 'provider_name': 'TypeSafe'}
    return profiles, decision


class ArithmeticEnvironment:
    def tools_schema(self):
        return []

    def call(self, name, arguments):
        raise ValueError('This public contract has no tools')

    def validate(self, final_text):
        passed = final_text.strip() == '2'
        return {'passed': passed, 'checks': [] if passed else ['integer_sum_contract_failed']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline routing demo, zero real API requests')
    parser.add_argument('--route', choices=['pass', 'low', 'mid', 'high'], default='low')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    output = args.output or Path('runtime') / ('offline-demo-' + uuid.uuid4().hex[:10])
    count = 0

    def fake_api(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if request.url.path.endswith('/decisions'):
            stage = next(iter(payload['questions']))
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            choice = ('low' if args.route == 'low' else 'mid') if stage == 'low_mid' else args.route
            return httpx.Response(200, json={
                'id': f'gen-demo-{count}', 'model': payload['model'], 'provider': 'TypeSafe',
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'cost': .0001},
                'answers': {stage: {'type': 'choice', 'choice': choice,
                            'probabilities': {tier: .8 if tier == choice else .2 for tier in pair}}}})
        answer = '2' if args.route == 'pass' or payload['model'] != 'example/open_source' else '3'
        return httpx.Response(200, json={
            'id': f'gen-demo-{count}', 'model': payload['model'], 'provider': 'Example',
            'usage': {'cost': .001, 'prompt_tokens': 1, 'completion_tokens': 1},
            'choices': [{'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]})

    profiles, decision = example_config()
    with httpx.Client(transport=httpx.MockTransport(fake_api)) as http:
        core = Core(Client(http, 'offline-mock-only', output), profiles, decision)
        result = core.run('demo', {'prompt': 'Return only the integer sum of 1 and 1.'},
                          ArithmeticEnvironment, contract_version='integer-sum-v1')
    print(json.dumps({'offline': True, 'real_api_cost_usd': '0', 'mock_requests': count,
                      'status': result['status'], 'tiers': [a['tier'] for a in result['attempts']],
                      'classifier_calls': (result.get('selection') or {}).get('classifier_calls', 0),
                      'simulated_costs': result['costs'], 'evidence_directory': str(output)},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
