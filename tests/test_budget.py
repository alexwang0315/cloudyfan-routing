from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def test_admission_requires_live_authorization_and_reserves_shared_balance(tmp_path):
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    with pytest.raises(BudgetError, match='authorization'):
        store.reserve('r1', 'frozen', Decimal('0.60'))
    store.authorize(Authorization('approved', Decimal('1.00'), '2026-10-19T00:00:00+00:00'))
    store.reserve('r1', 'frozen', Decimal('0.60'))
    with pytest.raises(BudgetError, match='balance'):
        store.reserve('r2', 'other', Decimal('0.60'))
    assert store.report()['reserved_usd'] == '0.60'
    assert store.report()['available_usd'] == '0.40'
    restarted = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    restarted.reserve('r1', 'frozen', Decimal('0.60'))
    assert restarted.report()['reserved_usd'] == '0.60'


def test_unknown_holds_survive_restart_and_reconciliation_replaces_not_adds(tmp_path):
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal('1'), '2026-10-19T00:00:00+00:00'))
    store.reserve('r1', 'frozen', Decimal('.6'))
    store.begin_event('r1', 'model:one', 'model', Decimal('.3'), 'request-hash')
    restarted = BudgetStore(store.path, clock=lambda: NOW)
    with pytest.raises(BudgetError, match='unknown'):
        restarted.reserve('r2', 'other', Decimal('.1'))
    restarted.record_event('model:one', cost_usd='.1', verified=False, receipt_id='gen-one')
    restarted.record_event('model:one', cost_usd='.12', verified=True, receipt_id='gen-one')
    restarted.record_event('model:one', cost_usd='.12', verified=True, receipt_id='gen-one')
    restarted.finish('r1')
    assert restarted.report()['known_usd'] == '0.12'
    assert restarted.report()['verified_usd'] == '0.12'
    assert restarted.report()['available_usd'] == '0.88'
    restarted.reserve('r2', 'other', Decimal('.8'))
    assert restarted.report()['reserved_usd'] == '0.8'


def test_two_simultaneous_admissions_cannot_spend_same_balance(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal('1'), '2026-10-19T00:00:00+00:00'))
    def admit(run):
        try:
            BudgetStore(store.path, clock=lambda: NOW).reserve(run, run, Decimal('.6'))
            return True
        except BudgetError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, ['a', 'b']))
    assert sorted(results) == [False, True]


def test_budgeted_core_reserves_full_workflow_and_counts_repair_classifier(tmp_path):
    from cloudyfan_jev.budget import BudgetedCore, EnforcedBound
    from test_engine import API, core, TASK, ArithmeticEnvironment
    api = API(choices=('mid', 'high'))
    engine = core(tmp_path / 'client', api)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal('1'), '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'offline hard-cap fixture') for p in engine.profiles.values()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'offline hard-cap fixture')
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={},
                       verify_bound=lambda kind, identity, payload, bound: True)
    result = app.run('repair', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['status'] == 'passed'
    report = store.report()
    assert report['reservations'][0]['amount'] == '0.482'
    assert report['known_usd'] == '0.0202'
    assert report['reserved_usd'] == '0'
    assert result['budget']['run_total_usd'] == '0.0202'
    app.run('repair', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 4
    assert store.report()['known_usd'] == '0.0202'


def test_existing_client_spending_is_imported_before_new_admission(tmp_path):
    from cloudyfan_jev.budget import BudgetedCore, EnforcedBound
    from test_engine import API, core, TASK, ArithmeticEnvironment
    import httpx
    api = API(first='2')

    def transport(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': 'gen-test-1',
                'model': 'example/open_source', 'provider_name': 'Example', 'total_cost': '.01'}})
        return api(request)

    engine = core(tmp_path / 'client', transport)
    engine.run('prior', TASK, ArithmeticEnvironment, contract_version='v1')
    assert engine.client.reconcile('run-prior-a0-t0')['reconciliation']['verified'] is True
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal('.485'), '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'offline cap') for p in engine.profiles.values()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'offline cap')
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    with pytest.raises(BudgetError, match='balance'):
        app.run('new', TASK, ArithmeticEnvironment, contract_version='v1')
    assert store.report()['known_usd'] == '0.01'
    assert len(api.requests) == 1


def test_json_cost_precision_is_preserved_through_receipt_accounting():
    from cloudyfan_jev.client import usage
    record = {'raw': '{"id":"gen-precise","usage":{"cost":0.1234567890123456789}}'}
    assert usage(record)['cost_usd'] == '0.1234567890123456789'


def controlled(tmp_path, api, *, total='2', tools=None, verifier=lambda *a: True, limits=None):
    from cloudyfan_jev.budget import BudgetedCore, EnforcedBound
    from test_engine import core
    engine = core(tmp_path / 'client', api, limits=limits)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal(total), '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'offline hard cap') for p in engine.profiles.values()}
    bounds[engine.decision['version']] = EnforcedBound('.001', 'offline hard cap')
    return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds=tools or {}, verify_bound=verifier)


def test_external_tool_cost_counts_once_with_model_turns_and_stays_out_of_prompt(tmp_path):
    from cloudyfan_jev.budget import ToolBound, ToolReceipt, EnforcedBound
    from cloudyfan_jev.engine import Limits
    from test_engine import API, TASK
    messages = [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 't1', 'function': {'name': 'lookup', 'arguments': '{}'}}]},
        {'role': 'assistant', 'content': '2'}]
    api = API(messages=messages)
    class Environment:
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
        def call(self, name, arguments):
            return ToolReceipt({'value': '2'}, '.04', 'external-unique', True)
        def validate(self, final):
            return {'passed': final == '2'}
    app = controlled(tmp_path, api, tools={'lookup': ToolBound(EnforcedBound('.05', 'tool hardcap'), True)},
                     limits=Limits(max_turns=2, max_tool_calls=1))
    result = app.run('tool', TASK, Environment, contract_version='v1')
    assert result['budget']['run_total_usd'] == '0.06'
    assert result['budget']['shared']['verified_usd'] == '0.04'
    assert result['budget']['shared']['reservations'][0]['amount'] == '0.322'
    assert 'external-unique' not in str(api.requests)
    app.run('tool', TASK, Environment, contract_version='v1')
    assert app.store.report()['known_usd'] == '0.06'


@pytest.mark.parametrize('verifier', [None, lambda *a: False, lambda *a: '.01'])
def test_unconfirmed_caps_rejected_before_post(tmp_path, verifier):
    from test_engine import API, TASK, ArithmeticEnvironment
    api = API()
    app = controlled(tmp_path, api, verifier=verifier)
    with pytest.raises(BudgetError, match='upper bound'):
        app.run('no', TASK, ArithmeticEnvironment, contract_version='v1')
    assert api.requests == []


def test_interruption_preserves_unknown_and_never_resends_on_restart(tmp_path):
    import httpx
    from test_engine import TASK, ArithmeticEnvironment
    requests = []
    def interrupted(request):
        requests.append(request)
        raise httpx.ReadTimeout('fixture')
    app = controlled(tmp_path, interrupted)
    with pytest.raises(RuntimeError, match='incomplete billing'):
        app.run('crash', TASK, ArithmeticEnvironment, contract_version='v1')
    assert app.store.report()['unknown_events'] == 1
    assert app.store.report()['reserved_usd'] == '0.482'
    restarted = controlled(tmp_path, interrupted)
    with pytest.raises(BudgetError, match='unknown'):
        restarted.run('crash', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(requests) == 1


def test_expiry_between_first_answer_and_classification_stops_dispatch(tmp_path):
    from test_engine import API, TASK, ArithmeticEnvironment
    api = API()
    app = controlled(tmp_path, api)
    class Environment(ArithmeticEnvironment):
        def validate(self, final):
            app.store.clock = lambda: datetime(2026, 10, 20, tzinfo=timezone.utc)
            return super().validate(final)
    with pytest.raises(BudgetError, match='expired'):
        app.run('expired', TASK, Environment, contract_version='v1')
    assert len(api.requests) == 1
    assert app.store.report()['known_usd'] == '0.01'
    assert app.store.report()['reserved_usd'] == '0.482'


def test_external_tool_interruption_remains_unknown_and_cannot_be_replayed(tmp_path):
    from cloudyfan_jev.budget import ToolBound, EnforcedBound
    from cloudyfan_jev.engine import Limits
    from test_engine import API, TASK
    api = API(messages=[{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 't1', 'function': {'name': 'lookup', 'arguments': '{}'}}]}])
    effects = []
    class Environment:
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'lookup'}}]
        def call(self, name, arguments):
            effects.append(name)
            raise RuntimeError('provider interrupted after side effect')
        def validate(self, final):
            return {'passed': True}
    app = controlled(tmp_path, api, tools={'lookup': ToolBound(EnforcedBound('.05', 'tool cap'), True)},
                     limits=Limits(max_turns=2, max_tool_calls=1))
    with pytest.raises(RuntimeError):
        app.run('tool-crash', TASK, Environment, contract_version='v1')
    report = app.store.report()
    assert report['known_usd'] == '0.01'
    assert report['unknown_events'] == 1
    assert report['total_usd'] is None
    with pytest.raises(BudgetError, match='unknown'):
        app.run('tool-crash', TASK, Environment, contract_version='v1')
    assert effects == ['lookup']
    assert len(api.requests) == 1


def test_unknown_history_resolves_to_actual_fee_without_adding_an_event(tmp_path):
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal('1'), '2026-10-19T00:00:00+00:00'))
    store.import_history('old-404', cost_usd=None)
    with pytest.raises(BudgetError, match='unknown'):
        store.reserve('r1', 'frozen', Decimal('.5'))
    store.import_history('old-404', cost_usd='.2', receipt_id='gen-old', verified=True)
    store.reserve('r1', 'frozen', Decimal('.5'))
    assert store.report()['available_usd'] == '0.3'
    assert len(store.report()['events']) == 1


def test_unknown_tool_policy_stops_before_first_model_request(tmp_path):
    from test_engine import API, TASK, ArithmeticEnvironment
    class Environment(ArithmeticEnvironment):
        def tools_schema(self):
            return [{'type': 'function', 'function': {'name': 'unbounded'}}]
    api = API()
    app = controlled(tmp_path, api)
    with pytest.raises(BudgetError, match='Tool lacks'):
        app.run('unknown-tool', TASK, Environment, contract_version='v1')
    assert api.requests == []


def test_budgeted_core_reconciliation_uses_verified_total_once(tmp_path):
    import httpx
    import json
    from test_engine import TASK, ArithmeticEnvironment
    calls = []
    def api(request):
        calls.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, content='{"data":{"id":"gen-reconcile","model":"example/open_source",'
                '"provider_name":"Example","total_cost":0.01234567890123456789}}')
        model = json.loads(request.content)['model']
        return httpx.Response(200, json={'id': 'gen-reconcile', 'model': model, 'provider': 'Example',
            'usage': {}, 'choices': [{'message': {'content': '2'}, 'finish_reason': 'stop'}]})
    app = controlled(tmp_path, api)
    result = app.run('reconcile', TASK, ArithmeticEnvironment, contract_version='v1')
    assert result['budget']['run_total_usd'] == '0.01234567890123456789'
    assert result['budget']['shared']['verified_usd'] == '0.01234567890123456789'
    assert calls == ['POST', 'GET']


def test_local_zero_fee_receipt_combines_without_inventing_cloud_generation(tmp_path):
    from cloudyfan_jev.budget import BudgetedCore, EnforcedBound
    from test_local_backend import local_engine, response, TextContract
    requests = []
    def local(request):
        requests.append(request)
        return response()
    engine = local_engine(tmp_path / 'clients', local)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: NOW)
    store.authorize(Authorization('approved', Decimal('1'), '2026-10-19T00:00:00+00:00'))
    bounds = {p['model_version']: EnforcedBound('.03', 'mock provider cap') for p in engine.profiles.values()}
    bounds[engine.profiles['open_source']['model_version']] = EnforcedBound('0', 'mock operator no-fee')
    bounds[engine.decision['version']] = EnforcedBound('.001', 'mock classifier cap')
    app = BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    result = app.run('local', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
    assert result['budget']['run_total_usd'] == '0'
    assert result['budget']['shared']['events'][0]['receipt'].startswith('local:')
    assert result['attempts'][0]['calls'][0]['generation_id'] is None
    assert result['costs']['host_and_human_usd'] is None
    app.run('local', {'prompt': 'Return 1+1'}, TextContract, contract_version='v1')
    assert len(requests) == 1


def test_changed_enforcement_evidence_cannot_reuse_frozen_completed_run(tmp_path):
    from cloudyfan_jev.budget import EnforcedBound
    from test_engine import API, TASK, ArithmeticEnvironment
    api = API(first='2')
    app = controlled(tmp_path, api)
    app.run('frozen', TASK, ArithmeticEnvironment, contract_version='v1')
    model = app.core.profiles['high']['model_version']
    app.model_bounds[model] = EnforcedBound('.03', 'different cap evidence at same price')
    with pytest.raises(BudgetError, match='Frozen reservation changed'):
        app.run('frozen', TASK, ArithmeticEnvironment, contract_version='v1')
    assert len(api.requests) == 1
