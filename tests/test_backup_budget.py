"""The opt-in global backup envelope must reserve expensive alternatives too."""
from copy import deepcopy
from decimal import Decimal

import pytest

from cloudyfan_jev.budget import BudgetError, EnforcedBound, ToolBound
from test_budget import controlled
from test_engine import API, TASK, ArithmeticEnvironment


def backup_app(tmp_path, *, total='2', classifiers=0, tool_calls=0):
    api = API(first='2')
    app = controlled(tmp_path, api, total=total)
    profiles = list(deepcopy(app.core.profiles).values())
    other = deepcopy(profiles[0])
    other['model_version'] = 'example/expensive-backup'
    profiles.append(other)
    app.model_bounds[other['model_version']] = EnforcedBound('.2', 'offline mock hard cap')
    plan = {'generation_profiles': profiles, 'max_generation_calls': 3,
            'classifier_calls': classifiers, 'classifier_profile': app.core.decision,
            'max_tool_calls': tool_calls}
    app.core.budget_reservation_plan = lambda mode: deepcopy(plan)
    return app, api, plan


def test_expensive_backup_reserved_even_when_primary_passes_and_no_classifier_needed(tmp_path):
    app, api, plan = backup_app(tmp_path)
    # No classifier is reachable. Its bound must not be required or reserved.
    del app.model_bounds[app.core.decision['version']]
    result = app.run('passed', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.6'
    assert result['budget']['run_total_usd'] == '0.01'
    assert len(api.requests) == 1
    app.run('passed', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 1
    assert app.store.report()['known_usd'] == '0.01'


def test_balance_insufficient_for_backup_stops_before_any_post(tmp_path):
    app, api, _ = backup_app(tmp_path, total='.599')
    with pytest.raises(BudgetError, match='balance'):
        app.run('no', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def test_each_backup_needs_enforced_cap_even_if_not_called(tmp_path):
    app, api, _ = backup_app(tmp_path)
    del app.model_bounds['example/expensive-backup']
    with pytest.raises(BudgetError, match='upper bound'):
        app.run('no', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def test_global_classifier_and_tool_envelopes_do_not_multiply_per_pool(tmp_path):
    app, _, _ = backup_app(tmp_path, classifiers=2, tool_calls=6)
    app.tool_bounds = {'external': ToolBound(EnforcedBound('.05', 'mock'), True)}
    assert app._admit('cloudyfan') == Decimal('.902')


@pytest.mark.parametrize('key,value', [
    ('max_generation_calls', 9), ('max_generation_calls', True),
    ('classifier_calls', 3), ('classifier_calls', True),
    ('max_tool_calls', 49), ('max_tool_calls', -1),
    ('generation_profiles', []), ('classifier_profile', {'version': 'other'}),
])
def test_invalid_backup_envelope_fails_closed(tmp_path, key, value):
    app, api, plan = backup_app(tmp_path, classifiers=1)
    plan[key] = value
    with pytest.raises(BudgetError):
        app.run('no', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def test_baselines_cannot_claim_three_generation_or_classifier_calls(tmp_path):
    app, _, plan = backup_app(tmp_path)
    with pytest.raises(BudgetError):
        app._admit('low_first')
    plan['max_generation_calls'] = 2
    plan['classifier_calls'] = 1
    with pytest.raises(BudgetError):
        app._admit('open_source_first')


def test_unknown_prior_request_blocks_backup_admission(tmp_path):
    app, api, _ = backup_app(tmp_path)
    app.store.import_history('unresolved', cost_usd=None)
    with pytest.raises(BudgetError, match='unknown'):
        app.run('no', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def test_real_backup_runtime_budget_and_replay_include_availability_failure_fee(tmp_path):
    import json
    from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetStore
    from test_backup_runtime import build, response, run
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return response(payload, len(calls), status=503 if len(calls) == 1 else 200,
                        answer='bad' if len(calls) == 2 else '2')
    engine, _, time = build(tmp_path, handler)
    store = BudgetStore(tmp_path / 'controlled.sqlite', clock=lambda: time[0])
    store.authorize(Authorization('offline mock only', Decimal('2'), '2026-10-22T00:00:00+00:00'))
    bounds = {r['entry']['profile']['model_version']: EnforcedBound('.03', 'mock enforced cap')
              for pool in engine._backup['pools'].values() for r in pool}
    bounds['example/open_source-1'] = EnforcedBound('.20', 'mock backup cap')
    verified = []
    def verify(kind, identity, payload, bound):
        verified.append(identity)
        return True
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=verify)
    result = run(app)
    assert result['status'] == 'passed'
    assert result['generation_calls'] == 3 and len(calls) == 3
    assert result['budget']['run_total_usd'] == result['costs']['api_usd'] == '0.03'
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.60'
    assert set(bounds) <= set(verified)
    again = run(app)
    assert again['budget']['run_total_usd'] == '0.03'
    assert store.report()['known_usd'] == '0.03' and len(calls) == 3
