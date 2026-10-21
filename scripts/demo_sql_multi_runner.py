"""Five-setting qualification rehearsal using only MockTransport and real SQLite."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import httpx

from cloudyfan_jev.budget import Authorization, BudgetStore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.receipt_store import _immutable
from cloudyfan_jev.scores.configuration import configuration_digest
from cloudyfan_jev.scores.sql_exam import cases
from cloudyfan_jev.scores.sql_multi_runner import (
    MultiCandidateQualificationRunner, prepare_multi_candidate_table,
)
from cloudyfan_jev.sql import SqlValidationUnavailable


def synthetic_table(root):
    template = json.loads((root / 'examples/sql-model-versions-v1.json').read_text())['configurations'][0]['entry']
    entries = []
    providers = {}
    for index, (model, provider, name, effort) in enumerate([
        ('example/sql-one-v1', 'mock/a', 'Mock A', 'low'),
        ('example/sql-one-v1', 'mock/b', 'Mock B', 'low'),
        ('example/sql-three:free', 'mock/a', 'Mock A', 'none'),
        ('example/sql-four-v1', 'mock/c', 'Mock C', 'high'),
        ('example/sql-five-v1', 'mock/d', 'Mock D', 'medium'),
    ]):
        entry = deepcopy(template)
        entry['config_id'] = f'mock-sql-setting-{index}'
        entry['profile'].update(model=model, model_version=model, accepted_models=[model],
                                provider=provider, provider_name=name, effort=effort)
        if effort == 'none':
            entry['profile']['reasoning_parameter'] = 'omit'
        entries.append(entry)
        providers[provider] = name
    hashes = [configuration_digest(entry) for entry in entries]
    table = prepare_multi_candidate_table(entries + [deepcopy(entries[0])], slots={
        'open_source': [hashes[0], hashes[2]], 'low': [hashes[2]],
        'mid': [hashes[1], hashes[3]], 'high': [hashes[4]],
    })
    return table, providers


def demo(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not output.is_relative_to(root / 'runtime'):
        raise ValueError('Use a fresh private ignored runtime directory')
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    table, providers = synthetic_table(root)
    # Assessor-side fixture only. Hidden material is never sent to the runner.
    answers = {case.contract().task()['prompt']: case.answer() for case in cases()}
    posts, gets = [], []

    def upstream(request):
        if request.method == 'GET':
            generation_id = request.url.params['id']
            gets.append(generation_id)
            payload = posts[int(generation_id.rsplit('-', 1)[1]) - 1]
            return httpx.Response(200, json={'data': {'id': generation_id, 'total_cost': '0.004',
                'model': payload['model'], 'provider_name': providers[payload['provider']['only'][0]]}})
        if request.method != 'POST':
            raise AssertionError('No implicit supplier calls are allowed')
        payload = json.loads(request.content)
        posts.append(payload)
        answer = 'invalid-json' if len(posts) == 6 else answers.get(payload['messages'][1]['content'], 'OK')
        return httpx.Response(200, json={'id': f'gen-multi-demo-{len(posts)}',
            'model': payload['model'], 'provider': providers[payload['provider']['only'][0]],
            'choices': [{'message': {'content': answer}}], 'usage': {'cost': '0.001'}})

    interruption = []

    def environment(case):
        original = case.contract().environment()

        class Boundary:
            def validate(self, answer):
                if not interruption:
                    interruption.append(True)
                    raise SqlValidationUnavailable('SQL_VALIDATION_TIMEOUT')
                return original.validate(answer)
        return Boundary()

    store = BudgetStore(output / 'budget.sqlite')
    store.authorize(Authorization('synthetic-demo-only', Decimal('10'),
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()))
    bounds = {c['configuration_sha256']: EnforcedBound('0.01', 'artificial-mock-cap')
              for c in table['configurations']}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        client = Client(http, 'mock-' + 'credential', output / 'client')
        runner = MultiCandidateQualificationRunner(client, store, table, model_bounds=bounds,
            verify_bound=lambda *args: True, synthetic=True,
            environment_factory=environment, environment_version='mock-sql-multi-interruption-v1')
        first = runner.capture('multi-demo')
        _immutable(output / 'first-interruption.json', first)
        if first['status'] != 'stopped' or first['reason'] != 'SqlValidationUnavailable' or first['calls'] != 6:
            raise RuntimeError('Unexpected initial result; preserve and inspect evidence')
        completed = runner.capture('multi-demo')
        _immutable(output / 'capture-summary.json', completed)
        if completed['status'] != 'capture_complete':
            raise RuntimeError('Capture stopped; preserve and inspect before explicit resume')
        before = len(posts)
        replay = runner.capture('multi-demo', max_new_requests=0)
        if len(posts) != before or replay['status'] != 'capture_complete':
            raise RuntimeError('Completed replay unexpectedly dispatched or did not complete')
        # One explicit mock reconciliation; no automatic GET in capture/inspect.
        call_id = runner.records('multi-demo', runner.roles[1])[0]['attempts'][0]['request_id']
        client.reconcile(call_id)
        for candidate_id in runner.roles:
            _immutable(output / f'normalized-{candidate_id}.json', runner.records('multi-demo', candidate_id))
        graded = runner.grade('multi-demo', now=datetime.now(timezone.utc).isoformat())
        _immutable(output / 'graded.json', graded)
        if graded['capture']['known_api_usd'] != '0.259' or len(gets) != 1 or len(posts) != before:
            raise RuntimeError('Post-capture reconciliation or paid-request replay accounting changed')
    models = {candidate_id: {'first_pass': sum(c['first_pass'] is True for c in report['cases']),
                             'final_pass': sum(c['final_pass'] is True for c in report['cases']),
                             'synthetic_exam_pass': report['qualified'],
                             'known_synthetic_usd': report['known_api_usd']}
              for candidate_id, report in graded['models'].items()}
    result = {'kind': 'offline_mock_rehearsal', 'synthetic': True,
              'unique_settings': len(table['configurations']), 'slot_references': completed['slot_count'],
              'tasks_per_setting': 50, 'models': models, 'mock_posts': len(posts),
              'identity_calls': 5, 'repair_calls': len(posts) - 255, 'classifier_calls': 0,
              'interruption_resumed_without_resending': True, 'completed_replay_new_posts': len(posts) - before,
              'explicit_mock_reconciliation_gets': len(gets),
              'known_synthetic_usd': graded['capture']['known_api_usd'],
              'unknown_cost_count': completed['unknown_cost_count'], 'real_model_api_cost_usd': '0',
              'formal_qualification': 'pending', 'pool_activated': False, 'deployment': False, 'push': False}
    _immutable(output / 'summary.json', result)
    if not all(model['synthetic_exam_pass'] and model['final_pass'] == 50 for model in models.values()):
        raise RuntimeError('Synthetic qualification failed; preserve full reports')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(demo(Path(__file__).resolve().parents[1], args.output), indent=2))
