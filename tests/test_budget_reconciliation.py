"""Account-wide reconciliation safety, with independent callers and durable state."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetError, BudgetStore, BudgetedCore, EnforcedBound
from cloudyfan_jev.client import Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def store_at(path, total='1'):
    store = BudgetStore(path, clock=lambda: NOW)
    store.authorize(Authorization('fixture authorization', Decimal(total), '2099-01-01T00:00:00+00:00'))
    return store


def provisional(store, *, bound='.3', cost='.1'):
    store.reserve('A', 'frozen-A', '.6')
    store.begin_event('A', 'call-A', 'model', bound, 'request-A')
    store.record_event('call-A', cost_usd=cost, receipt_id='gen-A')
    store.finish('A')


def test_late_bound_breach_blocks_a_different_client_and_survives_restart(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite')
    posts = []
    def build(name):
        def api(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'data': {'id': 'gen-' + name, 'model': 'example/high',
                    'provider_name': 'Example', 'total_cost': '.3'}})
            posts.append(name)
            return httpx.Response(200, json={'id': 'gen-' + name,
                'model': json.loads(request.content)['model'], 'provider': 'Example', 'usage': {'cost': '.1'},
                'choices': [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]})
        profiles, decision = example_config()
        client = Client(httpx.Client(transport=httpx.MockTransport(api)), 'fixture', tmp_path / name)
        engine = Core(client, profiles, decision, limits=Limits(max_turns=1))
        bounds = {p['model_version']: EnforcedBound('.2', 'fixture cap') for p in profiles.values()}
        return BudgetedCore(engine, store, model_bounds=bounds, tool_bounds={}, verify_bound=lambda *a: True)
    first, second = build('first'), build('second')
    task = {'prompt': 'Return the sum of 1 and 1.'}
    first.run('one', task, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    first.client.reconcile('run-one-a0-t0')
    with pytest.raises(BudgetError, match='cap violated'):
        first.sync_client(first.client)
    with pytest.raises(BudgetError):
        second.run('two', task, ArithmeticEnvironment, contract_version='v1', mode='fixed_high')
    assert posts == ['first']
    restarted = BudgetStore(store.path, clock=lambda: NOW)
    report = restarted.report()
    assert report['dispatch_blocked'] is True and report['total_usd'] is None
    violation = report['violations'][0]
    assert Decimal(violation['observed_cost']) == Decimal('.3')
    assert Decimal(violation['prior_cost']) == Decimal('.1')
    assert violation['receipt'] == 'gen-first'
    with pytest.raises(BudgetError):
        restarted.reserve('three', 'new', '.01')
    # A later stale response cannot erase the durable violation.
    with pytest.raises(BudgetError):
        restarted.record_event(violation['event_id'], cost_usd='.1', receipt_id='gen-first')
    assert restarted.report()['violations'] == report['violations']


def test_breach_blocks_an_already_reserved_other_workflow(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite', '2')
    provisional(store, bound='.2')
    store.reserve('B', 'frozen-B', '.5')
    with pytest.raises(BudgetError):
        store.record_event('call-A', cost_usd='.3', verified=True, receipt_id='gen-A')
    with pytest.raises(BudgetError):
        store.begin_event('B', 'call-B', 'model', '.5', 'request-B')


def test_completed_provisional_call_retains_its_upper_bound_exposure(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite')
    provisional(store)
    with pytest.raises(BudgetError, match='balance'):
        store.reserve('B', 'frozen-B', '.8')
    report = store.report()
    assert Decimal(report['provisional_hold_usd']) == Decimal('.2')
    assert Decimal(report['available_usd']) == Decimal('.7')
    restarted = BudgetStore(store.path, clock=lambda: NOW)
    restarted.record_event('call-A', cost_usd='.3', verified=True, receipt_id='gen-A')
    assert Decimal(restarted.report()['available_usd']) == Decimal('.7')
    assert Decimal(restarted.report()['provisional_hold_usd']) == 0
    restarted.reserve('B', 'frozen-B', '.7')
    assert restarted.begin_event('B', 'call-B', 'model', '.7', 'request-B') is True


def test_verified_lower_fee_releases_only_the_remaining_risk_hold(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite')
    provisional(store)
    store.record_event('call-A', cost_usd='.12', verified=True, receipt_id='gen-A')
    assert Decimal(store.report()['known_usd']) == Decimal('.12')
    assert Decimal(store.report()['available_usd']) == Decimal('.88')
    store.reserve('B', 'frozen-B', '.88')


def test_existing_legacy_reservation_cannot_dispatch_after_exposure_exceeds_cap(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite')
    provisional(store)
    # Represents an already persisted reservation from the previous buggy version.
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO reservations VALUES (?,?,?,?)', ('B', 'frozen-B', '.8', 'active'))
    store.record_event('call-A', cost_usd='.3', verified=True, receipt_id='gen-A')
    restarted = BudgetStore(store.path, clock=lambda: NOW)
    with pytest.raises(BudgetError):
        restarted.begin_event('B', 'call-B', 'model', '.8', 'request-B')
    assert Decimal(restarted.report()['available_usd']) == Decimal('-.1')
    assert restarted.report()['dispatch_blocked'] is True
    assert len(restarted.report()['events']) == 1


def test_unbounded_provisional_history_is_not_treated_as_final_spending(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite')
    store.import_history('old', cost_usd='.1', receipt_id='gen-old')
    with pytest.raises(BudgetError):
        store.reserve('B', 'frozen-B', '.8')
    assert store.report()['known_usd'] == '0.1'
    assert store.report()['available_usd'] is None
    store.import_history('old', cost_usd='.2', verified=True, receipt_id='gen-old')
    store.reserve('B', 'frozen-B', '.8')


def test_first_receipt_breach_is_durable_and_preserves_observed_charge(tmp_path):
    store = store_at(tmp_path / 'budget.sqlite')
    store.reserve('A', 'frozen-A', '.6')
    store.begin_event('A', 'call-A', 'model', '.2', 'request-A')
    with pytest.raises(BudgetError):
        store.record_event('call-A', cost_usd='.3', receipt_id='gen-A')
    report = BudgetStore(store.path, clock=lambda: NOW).report()
    assert report['unknown_events'] == 1
    assert report['violations'][0]['prior_cost'] is None
    assert Decimal(report['violations'][0]['observed_cost']) == Decimal('.3')
    with pytest.raises(BudgetError):
        store.finish('A')


def test_previous_database_schema_is_upgraded_without_resetting_spend_or_holds(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE authorization (singleton INTEGER PRIMARY KEY, reference TEXT, total TEXT, expires TEXT)')
        db.execute('CREATE TABLE reservations (run TEXT PRIMARY KEY, fingerprint TEXT, amount TEXT, status TEXT)')
        db.execute('CREATE TABLE events (id TEXT PRIMARY KEY, run TEXT, kind TEXT, bound TEXT, fingerprint TEXT, cost TEXT, verified INTEGER, receipt TEXT UNIQUE)')
        db.execute('INSERT INTO authorization VALUES (1,?,?,?)', ('old-approval', '1', '2099-01-01T00:00:00+00:00'))
        db.execute('INSERT INTO reservations VALUES (?,?,?,?)', ('A', 'frozen-A', '.6', 'complete'))
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)', ('call-A', 'A', 'model', '.3', 'request-A', '.1', 0, 'gen-A'))
    store = BudgetStore(path, clock=lambda: NOW)
    report = store.report()
    assert report['authorization']['reference'] == 'old-approval'
    assert report['known_usd'] == '0.1'
    assert report['violations'] == []
    assert Decimal(report['provisional_hold_usd']) == Decimal('.2')
    assert Decimal(report['available_usd']) == Decimal('.7')
    with pytest.raises(BudgetError, match='balance'):
        store.reserve('B', 'frozen-B', '.8')
