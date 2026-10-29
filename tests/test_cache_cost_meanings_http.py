"""#48：分清快取用量、API 實付、客戶用量費與保守上界；真實主服務 HTTP，上游僅 MockTransport。"""
from decimal import Decimal
import json

import httpx
import pytest

from cloudyfan_monitor import demo
from test_cache_observations_http import SUPPORT, _observe
from test_local_http_integration import loopback_network as loopback_network
from test_saved_paired_outcomes_http import paired_http as paired_http


def _cache_upstream(monkeypatch, usage):
    original = demo.Upstream.__call__

    def with_cache(self, request):
        response = original(self, request)
        body = json.loads(response.content)
        if 'choices' in body:
            body['usage'].update(usage(len(self.calls)))
        return httpx.Response(response.status_code, json=body)

    monkeypatch.setattr(demo.Upstream, '__call__', with_cache)


def _store(tracer, mode):
    return tracer.bundle.app.gateway.service.contracts['demo-' + mode].runner.store


def test_write_miss_then_read_hit_keep_usage_ledger_bound_and_reservation_apart(paired_http, monkeypatch):
    _cache_upstream(monkeypatch, lambda n: {'prompt_tokens_details': {
        'cached_tokens': 0 if n == 1 else 16, 'cache_write_tokens': 16 if n == 1 else 0}})
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'cost-write', 'open_source_first')
    paired_http.run('studio', 'cost-read', 'open_source_first', prompt='2+2 PRIVATE_PAIRED_CANARY')
    write = _observe(paired_http, 'studio', 'cost-write').json()
    read = _observe(paired_http, 'studio', 'cost-read').json()
    first, second = write['calls'][0], read['calls'][0]
    assert first['cache_result'] == 'miss' and first['cache_usage']['cache_write_tokens'] == 16
    assert second['cache_result'] == 'hit' and second['cache_usage']['cache_read_tokens'] == 16
    for value, call in ((write, first), (read, second)):
        cost = call['api_cost']
        assert cost['status'] == 'authoritative' and cost['source'] == 'budget_ledger'
        # 實付只取帳本一次；上界是限制不是花費，且與實付不同欄。
        assert Decimal(cost['amount_usd']) == Decimal(value['costs']['api_known_usd'])
        assert call['conservative_bound_usd'] is not None
        assert Decimal(call['conservative_bound_usd']) >= Decimal(cost['amount_usd'])
        assert value['costs']['api_unknown_events'] == 0
        assert value['costs']['api_complete_usd'] == value['costs']['api_known_usd']
        assert value['costs']['reservation'] is not None and 'saving' not in json.dumps(value)
    # 先前命中不等於下次保證：命中只標在實際回報讀取的那一次。
    assert first['cache_result'] != 'hit'


def test_provider_without_cache_items_keeps_the_aggregate_cost_and_unknown_cache_result(paired_http):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'aggregate-only', 'open_source_first')
    call = _observe(paired_http, 'studio', 'aggregate-only').json()['calls'][0]
    assert call['cache_result'] == 'unknown' and call['cache_usage']['status'] == 'unknown'
    assert call['api_cost']['status'] == 'authoritative' and call['api_cost']['amount_usd'] is not None
    assert call['tokens'] == {'prompt_tokens': 20, 'completion_tokens': 1}


def test_unknown_fee_stays_unknown_until_existing_reconciliation_replaces_it_once(paired_http):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.bundle.upstream.unknown_cost = True
    paired_http.run('studio', 'unknown-fee', 'open_source_first')
    value = _observe(paired_http, 'studio', 'unknown-fee').json()
    assert value['calls'][0]['api_cost']['status'] == 'unknown' and value['calls'][0]['api_cost']['amount_usd'] is None
    assert value['costs']['api_unknown_events'] == 1 and value['costs']['api_complete_usd'] is None
    store = _store(paired_http, 'open_source_first')
    import sqlite3
    with sqlite3.connect(store.path) as db:
        event, receipt = db.execute('SELECT id, receipt FROM events WHERE cost IS NULL').fetchone()
    # 既有核帳介面要求同一個原收據身分；不另造收據。
    store.record_event(event, cost_usd='0.0002', verified=True, receipt_id=receipt)
    reconciled = _observe(paired_http, 'studio', 'unknown-fee').json()
    cost = reconciled['calls'][0]['api_cost']
    assert (cost['status'], cost['amount_usd'], cost['verified']) == ('authoritative', '0.0002', True)
    assert reconciled['costs']['api_known_usd'] == '0.0002', 'Reconciliation must replace, not add'
    assert reconciled['costs']['api_unknown_events'] == 0


@pytest.mark.parametrize('principal', ['studio'])
def test_customer_sees_only_its_own_usage_charge_not_api_cost_bounds_or_reservation(paired_http, principal):
    paired_http.bundle.app.cache_support = SUPPORT
    paired_http.run('studio', 'customer-costs', 'open_source_first')
    value = _observe(paired_http, 'studio', 'customer-costs', principal=principal).json()
    assert set(value['costs'] or {}) <= {'customer_usage_charge'}
    assert all('api_cost' not in row and 'conservative_bound_usd' not in row for row in value['calls'])
    operator = _observe(paired_http, 'studio', 'customer-costs').json()
    assert operator['costs']['customer_usage_charge']['status'] in ('available', 'not_provided', 'unknown')
