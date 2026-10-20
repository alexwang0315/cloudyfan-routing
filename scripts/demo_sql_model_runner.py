"""Complete mock-only SQL qualification rehearsal. No real credential or network."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import httpx

from cloudyfan_jev.budget import Authorization, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.receipt_store import _immutable
from cloudyfan_jev.scores.sql_exam import cases
from cloudyfan_jev.scores.sql_runner import QualificationRunner, ROLES
from cloudyfan_jev.sql import SqlValidationUnavailable


def demo(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not output.is_relative_to(root / 'runtime'):
        raise ValueError('Use a fresh private ignored runtime directory')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    table = json.loads((root / 'examples/sql-model-versions-v1.json').read_text())
    # Assessor-side fixture only. The capture runner never obtains this mapping.
    answers = {case.contract().task()['prompt']: case.answer() for case in cases()}
    posts = []
    receipt_gets = []

    def mock_upstream(request):
        if request.method == 'GET':
            receipt_gets.append(request.url.params['id'])
            payload = posts[int(request.url.params['id'].rsplit('-', 1)[1]) - 1]
            provider = 'BaseTen' if payload['provider']['only'] == ['baseten/fp4'] else 'OpenAI'
            return httpx.Response(200, json={'data': {'id': request.url.params['id'],
                'model': payload['model'], 'provider_name': provider, 'total_cost': '0.004'}})
        if request.method != 'POST':
            raise AssertionError('No automatic receipt GET or retry in this rehearsal')
        payload = json.loads(request.content)
        posts.append(payload)
        answer = 'invalid-json' if len(posts) == 5 else answers.get(payload['messages'][1]['content'], 'OK')
        provider = 'BaseTen' if payload['provider']['only'] == ['baseten/fp4'] else 'OpenAI'
        return httpx.Response(200, json={'id': f'gen-mock-exam-{len(posts)}', 'model': payload['model'],
            'provider': provider, 'choices': [{'message': {'content': answer}}], 'usage': {'cost': '0.001'}})

    interrupted = []

    def environment(case):
        original = case.contract().environment()

        class Boundary:
            def validate(self, answer):
                if not interrupted:
                    interrupted.append(True)
                    raise SqlValidationUnavailable('SQL_VALIDATION_TIMEOUT')
                return original.validate(answer)
        return Boundary()

    store = BudgetStore(output / 'budget.sqlite')
    store.authorize(Authorization('synthetic-demo-only', Decimal('5'),
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    bounds = {c['entry']['profile']['model_version']: EnforcedBound('0.01', 'artificial-mock-cap')
              for c in table['configurations']}
    with httpx.Client(transport=httpx.MockTransport(mock_upstream)) as http:
        client = Client(http, 'mock-' + 'credential', output / 'client')
        runner = QualificationRunner(client, store, table, model_bounds=bounds,
            verify_bound=lambda *args: True, synthetic=True,
            environment_factory=environment, environment_version='mock-interruption-v1')
        first = runner.capture('mock-exam')
        _immutable(output / 'first-interruption.json', first)
        if first['status'] != 'stopped' or first['reason'] != 'SqlValidationUnavailable' or first['calls'] != 5:
            raise RuntimeError('Unexpected initial interruption; inspect preserved receipt')
        completed = runner.capture('mock-exam')
        _immutable(output / 'capture-summary.json', completed)
        if completed['status'] != 'capture_complete':
            raise RuntimeError('Capture stopped; inspect saved evidence before explicit resume')
        before = len(posts)
        replay = runner.capture('mock-exam', max_new_requests=0)
        if len(posts) != before or replay['status'] != 'capture_complete':
            raise RuntimeError('Completed replay failed or unexpectedly dispatched')
        # Explicit mock GET after completion: provisional fee changes, answers do not.
        call_id = runner.records('mock-exam', 'low')[0]['attempts'][0]['request_id']
        client.reconcile(call_id)
        for role in ROLES:
            _immutable(output / f'normalized-{role}.json', runner.records('mock-exam', role))
        # Only now, after the complete immutable capture seal, consult sealed cases.
        graded = runner.grade('mock-exam', now=datetime.now(timezone.utc).isoformat())
        _immutable(output / 'graded.json', graded)
        if graded['capture']['known_api_usd'] != '0.208' or len(receipt_gets) != 1 or len(posts) != before:
            raise RuntimeError('Post-capture reconciliation did not reach grading and total ledger')
    models = {role: {'final_pass': sum(c['final_pass'] is True for c in report['cases']),
                     'first_pass': sum(c['first_pass'] is True for c in report['cases']),
                     'synthetic_exam_pass': report['qualified'], 'known_synthetic_usd': report['known_api_usd']}
              for role, report in graded['models'].items()}
    result = {'kind': 'offline_mock_rehearsal', 'synthetic': True, 'tasks_per_model': 50,
              'models': models, 'mock_posts': len(posts), 'classifier_calls': 0,
              'identity_calls': 4, 'repair_calls': len(posts) - 204,
              'known_synthetic_usd': graded['capture']['known_api_usd'], 'real_api_cost_usd': '0',
              'explicit_mock_receipt_gets': len(receipt_gets),
              'unknown_cost_count': completed['unknown_cost_count'], 'completed_replay_new_posts': len(posts) - before,
              'interruption_resumed_without_resending': True, 'formal_qualification': 'pending',
              'pool_activated': False, 'deployment': False, 'push': False}
    _immutable(output / 'summary.json', result)
    if not all(r['synthetic_exam_pass'] for r in models.values()):
        raise RuntimeError('Synthetic qualification failed; retain full report')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(demo(Path(__file__).resolve().parents[1], args.output), indent=2))
