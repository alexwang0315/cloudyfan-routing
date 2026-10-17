import httpx
import pytest

from cloudyfan_jev.client import CHAT, Client
from cloudyfan_jev.ledger import summarize


def test_partial_and_completed_intents_all_counted(tmp_path):
    requests = []
    def handler(req):
        requests.append(req)
        if len(requests) == 2:
            raise KeyboardInterrupt()
        return httpx.Response(200, json={'id': 'gen-first', 'model': 'example/model',
            'provider': 'Example', 'usage': {'cost': .01}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        api = Client(http, 'mock-only', tmp_path)
        api.call('completed', CHAT, {})
        with pytest.raises(KeyboardInterrupt): api.call('interrupted', CHAT, {})
        result = summarize(api)
    assert result['calls'] == 2 and result['unknown_calls'] == 1
    assert result['known_unique_cost_usd'] == '0.01' and result['total_api_usd'] is None
    assert 'raw' not in str(result) and 'mock-only' not in str(result)


@pytest.mark.parametrize('ids', [('a', 'b'), ('z-original', 'a-duplicate')])
def test_duplicate_generation_is_unresolved_not_double_charged(tmp_path, ids):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            'id': 'gen-duplicate', 'usage': {'cost': .01}}))) as http:
        api = Client(http, 'mock-only', tmp_path)
        api.call(ids[0], CHAT, {})
        api.call(ids[1], CHAT, {})
        result = summarize(api)
    assert result['known_unique_cost_usd'] == '0.01'
    assert result['total_api_usd'] is None and result['unknown_calls'] == 1


def test_empty_ledger_cost_zero_without_network(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('Unexpected request'))) as http:
        result = summarize(Client(http, 'mock-only', tmp_path))
    assert result['total_api_usd'] == '0' and result['calls'] == 0
