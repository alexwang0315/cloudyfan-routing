"""One-command synthetic pool lifecycle; fixed MockTransport, real SQLite only."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import json
import uuid

import httpx

from ..budget import Authorization, BudgetError, BudgetStore, EnforcedBound
from ..client import Client
from ..engine import Limits
from ..sql import SQL_DIAGNOSTICS, SqlContract, run_sql
from .adapters import import_codexradar
from .catalog import canonical
from .demo import example_inputs
from .configuration import configuration_digest
from .first_pool import prepare_pool, qualified_pool_core
from .integration import utc_now
from .snapshots import SnapshotStore
from .updates import ScoreUpdater


def fixture_profile(now):
    """Non-routable IDs and explicit artificial evidence, never supplier guarantees."""
    imported, registry, policy, requirements, decision = example_inputs(now)
    policy['domain'] = requirements['domain'] = 'bounded-sqlite-readonly'
    requirements['capabilities'] = ['sql_constant']
    evidence = {}
    for entry in [registry['open_source'], *registry['candidates']]:
        entry['capabilities'] = ['sql_constant']
        entry['profile']['capability_card'] = {'sql_constant': 'Synthetic SELECT-constant exercise only'}
        evidence[entry['config_id']] = {
            'configuration_sha256': configuration_digest(entry),
            'mapping': {'status': 'approved', 'evidence': ['fixture://identity'],
                        **{k: entry[k] for k in ('source', 'source_version', 'source_model_id')}},
            'capabilities': {'basis': 'public_capability_record', 'operations': ['sql_constant'],
                             'context_tokens': 8192, 'evidence': ['fixture://constant']}}
    return {'schema_version': 'cloudyfan-first-pool-profile-v1', 'synthetic': True,
            'source_choice': {'reason': 'Offline fixture, not source data', 'archive_sha256': '0' * 64},
            'observations': import_codexradar(canonical(imported), retrieved_at=now),
            'registry': registry, 'policy': policy, 'requirements': requirements,
            'decision': decision, 'review': {}, 'configuration_evidence': evidence}


def fixture_review(profile, now):
    """Only the isolated synthetic demo uses these explicit AI fixture statements."""
    proof = {'status': 'approved', 'reviewer': 'AI-offline-fixture-not-human-approval',
             'reviewed_at': now, 'evidence': ['fixture://synthetic-review']}
    profile['review'] = {
        'input_sha256': prepare_pool(profile, now=now)['qualification_pack']['input_sha256'],
        **{k: deepcopy(proof) for k in ('domain', 'source', 'thresholds', 'coverage', 'expiry', 'access')},
        'models': {e['config_id']: deepcopy(proof) for e in [profile['registry']['open_source'],
                                                          *profile['registry']['candidates']]}}


def demonstrate_pool(output, *, now):
    """Exercise qualification/update/budget/routing without an injectable real URL."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    profile = fixture_profile(now)
    fixture_review(profile, now)
    report = prepare_pool(profile, now=now)
    before = report['qualification_pack']
    store = SnapshotStore(output / 'test-only-snapshots')
    updater = ScoreUpdater(store)
    initial = updater.apply('initial', None, before, now=now, allow_synthetic=True)
    if initial['status'] != 'published':
        raise RuntimeError('Synthetic initial update failed')
    requests, routes = [], []
    active = {'route': 'pass'}

    def api(request):
        body = json.loads(request.content)
        requests.append(body)
        response = {'id': 'gen-first-pool-' + str(len(requests)), 'model': body['model']}
        if request.url.path.endswith('/decisions'):
            stage = next(iter(body['questions']))
            pair = ('low', 'mid') if stage == 'low_mid' else ('mid', 'high')
            choice = ('low' if active['route'] == 'low' else 'mid') if stage == 'low_mid' else active['route']
            response.update(provider='TypeSafe', usage={'cost': .0001, 'input_tokens': 1, 'output_tokens': 1},
                            answers={stage: {'type': 'choice', 'choice': choice,
                                            'probabilities': {k: .9 if k == choice else .1 for k in pair}}})
        else:
            value = (0 if (active['route'] != 'pass' and body['model'] == 'example/open_source')
                     or (active['route'] == 'high' and body['model'] == 'example/mid') else 1)
            response.update(provider='Example', usage={'cost': .001, 'prompt_tokens': 1, 'completion_tokens': 1},
                            choices=[{'message': {'role': 'assistant', 'content': json.dumps({'sql': f'SELECT {value} AS value'})},
                                      'finish_reason': 'stop'}])
        return httpx.Response(200, json=response)

    budget = BudgetStore(output / 'test-only-budget.sqlite', clock=lambda: datetime.fromisoformat(now))
    budget.authorize(Authorization('synthetic-fixture-only', '1', '2099-01-01T00:00:00+00:00'))
    bounds = {e['profile']['model_version']: EnforcedBound('.01', 'fixture://not-a-supplier-cap')
              for e in [profile['registry']['open_source'], *profile['registry']['candidates']]}
    bounds[profile['decision']['version']] = EnforcedBound('.001', 'fixture://not-a-supplier-cap')
    contract = SqlContract(version='first-pool-offline-v1', prompt='Return the constant integer 1 as value.',
                           tables={'items': [('id', 'INTEGER')]}, expected_columns=['value'],
                           samples=[{'id': 'public-constant', 'tables': {'items': []}, 'expected': [[1]]}])
    with httpx.Client(transport=httpx.MockTransport(api), trust_env=False) as http:
        client = Client(http, 'mock-only-never-a-real-key', output / 'test-only-client')

        def engine(prepared, name, *, clock=lambda: now, verifier=lambda *args: True):
            return qualified_pool_core(client, prepared, run_id=name, budget_store=budget,
                                       model_bounds=bounds, verify_bound=verifier, allow_synthetic=True,
                                       clock=clock, diagnostic_contract=SQL_DIAGNOSTICS,
                                       limits=Limits(max_turns=1))

        for route in ('pass', 'low', 'mid', 'high'):
            active['route'] = route
            count = len(requests)
            result = run_sql(engine(report, route), route, contract)
            routes.append({'selected_tier': result['attempts'][-1]['tier'],
                           'status': result['status'], 'model_requests': len(requests) - count})
        next_profile = deepcopy(profile)
        next_profile['observations'][0]['score'] = 61  # Explicit artificial second version.
        fixture_review(next_profile, now)
        after_report = prepare_pool(next_profile, now=now)
        update = updater.apply('second', before, after_report['qualification_pack'], now=now, allow_synthetic=True)
        current = store.load(now=now)
        if current['snapshot_id'] != update['snapshot_id']:
            raise RuntimeError('New current does not match update')
        active['route'] = 'pass'
        fresh = engine(after_report, 'new-task')
        run_sql(fresh, 'new-task', contract)
        selected_new = fresh.core.profiles['low']['capability_card']['score_selection']['snapshot_id']
        expired = (datetime.fromisoformat(now) + timedelta(days=3)).isoformat()
        count = len(requests)
        resumed = engine(report, 'high', clock=lambda: expired)
        run_sql(resumed, 'high', contract)
        resume_posts = len(requests) - count
        expired_blocked = False
        try:
            engine(after_report, 'expired-new', clock=lambda: expired)
        except ValueError as error:
            if 'stale' not in str(error):
                raise
            expired_blocked = True
        fee_blocked = False
        try:
            run_sql(engine(after_report, 'no-fee-proof', verifier=None), 'no-fee-proof', contract)
        except BudgetError:
            fee_blocked = True
        broken = deepcopy(next_profile)
        broken['review'] = {}
        blocked = updater.apply('unqualified', after_report['qualification_pack'],
                                prepare_pool(broken, now=now)['qualification_pack'], now=now, allow_synthetic=True)
        kept = blocked['status'] == 'blocked' and store.load(now=now) == current
    result = {'synthetic': True, 'production_ready': False, 'real_api_calls': 0,
              'routes': routes, 'simulated_api_usd': budget.report()['known_usd'],
              'update_status': update['status'], 'original_snapshot_id': initial['snapshot_id'],
              'new_snapshot_id': current['snapshot_id'], 'new_task_snapshot_id': selected_new,
              'resume_snapshot_id': resumed.core.profiles['low']['capability_card']['score_selection']['snapshot_id'],
              'resume_posts': resume_posts, 'expired_new_run_blocked': expired_blocked,
              'unconfirmed_fee_bound_blocked': fee_blocked, 'blocked_update_kept_current': kept}
    for name, value in [('before-report', report), ('after-report', after_report), ('report', result)]:
        (output / (name + '.json')).write_bytes(canonical(value) + b'\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Synthetic first-pool lifecycle; MockTransport only')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(demonstrate_pool(args.output or Path('runtime') / ('first-pool-demo-' + uuid.uuid4().hex),
                                     now=utc_now()), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
