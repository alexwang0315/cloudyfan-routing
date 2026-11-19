"""試跑等級的費用上界（牌價 × token 上限）；不呼叫任何模型。"""
from datetime import datetime, timezone
import json

import httpx
import pytest

from cloudyfan_jev.budget import Authorization, BudgetedCore, BudgetError, BudgetStore
from cloudyfan_jev.client import CHAT, Client
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.pilot_bounds import ListPriceBounds

PRICES = {'anthropic:claude-fable-5-2': {'input_per_mtok': '8', 'output_per_mtok': '40'},
          'oss/first': {'input_per_mtok': '0.2', 'output_per_mtok': '0.6'},
          'typesafe/jev-1.13': {'per_call_usd': '0.001'}}


def bounds(**kw):
    return ListPriceBounds(PRICES, max_input_bytes=20000, max_output_tokens=4096, overhead_tokens=1000, **kw)


def test_ceiling_is_list_price_times_token_limits():
    b = bounds()
    # (20000 + 1000) × $8/M + 4096 × $40/M = 0.168 + 0.16384
    assert b.ceiling('anthropic:claude-fable-5-2') == '0.33184'
    assert b.ceiling('typesafe/jev-1.13') == '0.001'
    table = b.model_bounds()
    assert table['oss/first'].usd == '0.0066576' and 'list price' in table['oss/first'].evidence
    assert b.descriptor()['kind'] == 'pilot-list-price-bounds-v1'


def test_admission_accepts_only_priced_models_at_or_above_the_ceiling():
    b = bounds()
    table = b.model_bounds()
    assert b('model', 'oss/first', {}, table['oss/first']) is True
    assert b('model', 'unpriced/model', {}, table['oss/first']) is not True
    from cloudyfan_jev.budget import EnforcedBound
    assert b('model', 'oss/first', {}, EnforcedBound('0.001', 'too low')) is not True


def request(model, *, max_tokens=4096, text='hi'):
    return {'model': model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': text}]}


def test_dispatch_is_refused_when_the_request_exceeds_the_limits_behind_the_bound():
    b = bounds()
    bound = b.model_bounds()['oss/first']
    assert b.verify_dispatch(None, CHAT, request('oss/first'), bound) is True
    assert b.verify_post(None, CHAT, request('oss/first'), bound) is True
    assert b.verify_dispatch(None, CHAT, request('oss/first', max_tokens=8192), bound) is not True
    assert b.verify_dispatch(None, CHAT, request('oss/first', text='x' * 30000), bound) is not True
    payload = request('oss/first')
    del payload['max_tokens']
    assert b.verify_dispatch(None, CHAT, payload, bound) is not True   # 沒有輸出上限就沒有上界


def test_budgeted_run_stops_before_posting_an_oversized_request(tmp_path):
    calls = []

    def api(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={'id': f'gen-{len(calls)}', 'model': 'example/open_source',
            'provider': 'Example', 'usage': {'cost': .0001},
            'choices': [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]})
    profiles, decision = example_config()
    prices = {p['model_version']: {'input_per_mtok': '1', 'output_per_mtok': '2'} for p in profiles.values()}
    prices[decision['version']] = {'per_call_usd': '0.001'}
    verifier = ListPriceBounds(prices, max_input_bytes=20000, max_output_tokens=256, overhead_tokens=1000)
    store = BudgetStore(tmp_path / 'budget.sqlite', clock=lambda: datetime(2026, 11, 19, tzinfo=timezone.utc))
    store.authorize(Authorization('pilot-stage-cap', '1', '2026-11-20T00:00:00+00:00'))

    def run(max_tokens, name):
        core = Core(Client(httpx.Client(transport=httpx.MockTransport(api)), 'k', tmp_path / name), profiles, decision,
                    limits=Limits(max_turns=1, max_tokens=max_tokens))
        runner = BudgetedCore(core, store, model_bounds=verifier.model_bounds(), tool_bounds={}, verify_bound=verifier)
        return runner.run(name, {'prompt': 'Return only the integer sum of 1 and 1.'}, ArithmeticEnvironment,
                          contract_version='v1', mode='open_source_first')
    assert run(256, 'ok')['status'] == 'passed' and len(calls) == 1
    with pytest.raises(BudgetError):
        run(512, 'too-big')          # 設定的輸出上限超過上界所依據的值：送出前就擋下
    assert len(calls) == 1
