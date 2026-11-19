"""多家雲算力廠商接入層（ADR 0037）：「廠商:模型」定址、Anthropic 轉接、費用由用量與凍結價格計算。

全部離線：兩邊的廠商網址都只接 httpx.MockTransport，不呼叫任何真實模型。
"""
import json

import httpx
import pytest

from cloudyfan_jev.client import CHAT, Client, usage
from cloudyfan_jev.demo import ArithmeticEnvironment, example_config
from cloudyfan_jev.engine import Core, Limits
from cloudyfan_jev.vendors import (ANTHROPIC_MESSAGES, AnthropicVendor, VendorTransport,
                                   anthropic_cost, split_address, translate_request)

PRICES = {'claude-haiku-5-5': {'input_per_mtok': '0.10', 'output_per_mtok': '0.50',
                               'long_context_tokens': 100000, 'long_input_per_mtok': '0.50',
                               'long_output_per_mtok': '2.50'},
          'claude-sonnet-5-5': {'input_per_mtok': '2', 'output_per_mtok': '10'},
          'claude-fable-5-2': {'input_per_mtok': '8', 'output_per_mtok': '40'}}


class AnthropicAPI:
    def __init__(self, text='2', *, status=200, usage=None, stop_reason='end_turn', content=None):
        self.text, self.status, self.stop_reason, self.content = text, status, stop_reason, content
        self.usage = usage or {'input_tokens': 1000, 'output_tokens': 200}
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        if self.status != 200:
            return httpx.Response(self.status, headers={'request-id': 'req_err1'},
                                  json={'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'busy'}})
        content = self.content if self.content is not None else [
            {'type': 'thinking', 'thinking': '', 'signature': 'sig'}, {'type': 'text', 'text': self.text}]
        return httpx.Response(200, json={'id': f'msg_{len(self.requests):04d}', 'type': 'message', 'role': 'assistant',
                                         'model': body['model'], 'content': content,
                                         'stop_reason': self.stop_reason, 'usage': self.usage})


class OpenRouterAPI:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': {'id': request.url.params.get('id'), 'total_cost': 0.001}})
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': f'gen-or-{len(self.requests)}', 'model': body['model'],
            'provider': 'Example', 'usage': {'cost': .001},
            'choices': [{'message': {'role': 'assistant', 'content': '3'}, 'finish_reason': 'stop'}]})


def routed(anthropic, openrouter=None):
    openrouter = openrouter or OpenRouterAPI()
    vendor = AnthropicVendor('anthropic-secret-key', PRICES, transport=httpx.MockTransport(anthropic))
    transport = VendorTransport(httpx.MockTransport(openrouter), {'anthropic': vendor})
    return httpx.Client(transport=transport), openrouter


def post(http, model, **extra):
    return http.post(CHAT, headers={'Authorization': 'Bearer openrouter-secret-key'},
                     json={'model': model, 'max_tokens': 4096,
                           'messages': [{'role': 'system', 'content': 'Answer briefly.'},
                                        {'role': 'user', 'content': '1+1?'}], **extra})


# ---- 定址 ----

@pytest.mark.parametrize('address,expected', [
    ('openrouter:google/gemini-4-neon', ('openrouter', 'google/gemini-4-neon')),
    ('anthropic:claude-fable-5-2', ('anthropic', 'claude-fable-5-2')),
    ('google/gemini-4-neon', ('openrouter', 'google/gemini-4-neon')),          # 沒有前綴：照舊走 OpenRouter
    ('meta/llama-4:free', ('openrouter', 'meta/llama-4:free')),                # OpenRouter 的 :free 路線不是廠商前綴
    ('openrouter:meta/llama-4:free', ('openrouter', 'meta/llama-4:free')),
    ('plain-model', ('openrouter', 'plain-model')),
])
def test_addresses_split_into_vendor_and_model(address, expected):
    assert split_address(address, {'openrouter', 'anthropic'}) == expected


def test_unregistered_vendor_prefix_is_rejected_not_guessed():
    with pytest.raises(ValueError, match='vendor'):
        split_address('acme:model-x', {'openrouter', 'anthropic'})


# ---- OpenRouter 照舊 ----

def test_openrouter_prefix_is_stripped_on_the_wire_and_key_is_untouched():
    http, openrouter = routed(AnthropicAPI())
    response = post(http, 'openrouter:google/gemini-4-neon')
    assert response.status_code == 200
    [request] = openrouter.requests
    assert json.loads(request.content)['model'] == 'google/gemini-4-neon'
    assert request.headers['authorization'] == 'Bearer openrouter-secret-key'
    assert str(request.url) == CHAT


def test_unprefixed_requests_and_other_endpoints_pass_through_unchanged():
    http, openrouter = routed(AnthropicAPI())
    post(http, 'google/gemini-4-neon')
    http.get('https://openrouter.ai/api/v1/generation', params={'id': 'gen-or-1'})
    assert json.loads(openrouter.requests[0].content)['model'] == 'google/gemini-4-neon'
    assert openrouter.requests[1].method == 'GET'


# ---- Anthropic 轉接 ----

def test_anthropic_request_uses_its_own_key_url_and_format():
    api = AnthropicAPI()
    http, openrouter = routed(api)
    response = post(http, 'anthropic:claude-fable-5-2', reasoning={'effort': 'high'},
                    provider={'only': ['anthropic']}, session_id='run-1')
    assert response.status_code == 200 and openrouter.requests == []
    [request] = api.requests
    assert str(request.url) == ANTHROPIC_MESSAGES
    assert request.headers['x-api-key'] == 'anthropic-secret-key'
    assert request.headers['anthropic-version'] == '2026-10-21'
    assert 'authorization' not in request.headers           # OpenRouter 金鑰不會送到 Anthropic
    assert 'openrouter-secret-key' not in request.content.decode()
    body = json.loads(request.content)
    assert body == {'model': 'claude-fable-5-2', 'max_tokens': 4096, 'system': 'Answer briefly.',
                    'messages': [{'role': 'user', 'content': '1+1?'}],
                    'output_config': {'effort': 'high'}}


def test_anthropic_response_becomes_the_internal_shape_with_computed_cost():
    http, _ = routed(AnthropicAPI('2', usage={'input_tokens': 1000, 'output_tokens': 200}))
    data = post(http, 'anthropic:claude-fable-5-2').json()
    assert data['id'] == 'gen-anthropic-msg_0001' and data['provider'] == 'Anthropic'
    assert data['model'] == 'anthropic:claude-fable-5-2'
    assert data['choices'] == [{'message': {'role': 'assistant', 'content': '2'}, 'finish_reason': 'stop'}]
    # 1000 × $8/M + 200 × $40/M = 0.008 + 0.008
    assert data['usage']['cost'] == '0.016'
    assert data['usage']['cost_basis'] == 'computed_from_vendor_token_usage'
    assert data['usage']['prompt_tokens'] == 1000 and data['usage']['completion_tokens'] == 200
    assert data['vendor']['name'] == 'anthropic' and data['vendor']['response']['id'] == 'msg_0001'
    assert 'anthropic-secret-key' not in json.dumps(data)


def test_long_context_tier_and_unpriced_usage():
    assert anthropic_cost(PRICES['claude-haiku-5-5'], {'input_tokens': 100000, 'output_tokens': 1000}) == '0.0105'
    assert anthropic_cost(PRICES['claude-haiku-5-5'], {'input_tokens': 100001, 'output_tokens': 1000}) == '0.0525005'
    # 沒有對應價格的用量（快取讀寫）不猜：費用未知。
    assert anthropic_cost(PRICES['claude-sonnet-5-5'],
                          {'input_tokens': 10, 'output_tokens': 10, 'cache_read_input_tokens': 5}) is None
    assert anthropic_cost(PRICES['claude-sonnet-5-5'], {'input_tokens': 10}) is None


def test_model_without_frozen_price_is_refused_before_any_request():
    api = AnthropicAPI()
    http, _ = routed(api)
    response = post(http, 'anthropic:claude-opus-5-5')
    assert response.status_code == 400 and response.json()['error']['message'] == 'vendor_model_not_priced'
    assert api.requests == []


def test_tool_calls_round_trip_between_formats():
    request = translate_request({'model': 'anthropic:claude-sonnet-5-5', 'max_tokens': 1024, 'tool_choice': 'auto',
        'tools': [{'type': 'function', 'function': {'name': 'run_sql', 'description': 'Run a query',
                                                     'parameters': {'type': 'object', 'properties': {}}}}],
        'messages': [{'role': 'user', 'content': 'count rows'},
                     {'role': 'assistant', 'content': None, 'tool_calls': [
                         {'id': 'toolu_1', 'type': 'function', 'function': {'name': 'run_sql', 'arguments': '{"q": "x"}'}},
                         {'id': 'toolu_2', 'type': 'function', 'function': {'name': 'run_sql', 'arguments': '{"q": "y"}'}}]},
                     {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': '3'},
                     {'role': 'tool', 'tool_call_id': 'toolu_2', 'content': '4'}]}, 'claude-sonnet-5-5')
    assert request['tools'] == [{'name': 'run_sql', 'description': 'Run a query',
                                 'input_schema': {'type': 'object', 'properties': {}}}]
    assert request['tool_choice'] == {'type': 'auto'}
    assert request['messages'][1] == {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'run_sql', 'input': {'q': 'x'}},
        {'type': 'tool_use', 'id': 'toolu_2', 'name': 'run_sql', 'input': {'q': 'y'}}]}
    # 平行工具結果合併在同一則 user 訊息。
    assert request['messages'][2] == {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': '3'},
        {'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': '4'}]}
    api = AnthropicAPI(stop_reason='tool_use', content=[
        {'type': 'text', 'text': 'checking'},
        {'type': 'tool_use', 'id': 'toolu_9', 'name': 'run_sql', 'input': {'q': 'z'}}])
    http, _ = routed(api)
    choice = post(http, 'anthropic:claude-sonnet-5-5').json()['choices'][0]
    assert choice['finish_reason'] == 'tool_calls' and choice['message']['content'] == 'checking'
    assert choice['message']['tool_calls'] == [{'id': 'toolu_9', 'type': 'function',
        'function': {'name': 'run_sql', 'arguments': '{"q": "z"}'}}]


def test_refusal_is_reported_without_an_answer_and_still_costed():
    http, _ = routed(AnthropicAPI(stop_reason='refusal', content=[]))
    data = post(http, 'anthropic:claude-fable-5-2').json()
    assert data['choices'][0] == {'message': {'role': 'assistant', 'content': None}, 'finish_reason': 'refusal'}
    assert data['usage']['cost'] == '0.016'


def test_vendor_error_keeps_status_and_is_recorded_as_not_billed():
    http, _ = routed(AnthropicAPI(status=529))
    response = post(http, 'anthropic:claude-fable-5-2')
    data = response.json()
    assert response.status_code == 529 and data['provider'] == 'Anthropic'
    assert data['error'] == {'code': 529, 'message': 'overloaded_error'}
    assert data['usage'] == {'cost': '0', 'cost_basis': 'vendor_error_not_billed'}
    assert data['id'].startswith('gen-anthropic-err-')


def test_reconciliation_lookup_for_vendor_ids_never_reaches_openrouter():
    http, openrouter = routed(AnthropicAPI())
    response = http.get('https://openrouter.ai/api/v1/generation', params={'id': 'gen-anthropic-msg_0001'})
    assert response.status_code == 404 and openrouter.requests == []


# ---- 接進 Client 與 Core：收據、身分核對、費用 ----

def test_core_runs_a_task_on_an_anthropic_addressed_pool_with_receipts(tmp_path):
    api = AnthropicAPI('2')
    http, openrouter = routed(api)
    profiles, decision = example_config()
    profiles['open_source'].update(model='anthropic:claude-haiku-5-5', model_version='anthropic:claude-haiku-5-5',
                                   accepted_models=['anthropic:claude-haiku-5-5'], provider='anthropic',
                                   provider_name='Anthropic')
    core = Core(Client(http, 'openrouter-secret-key', tmp_path), profiles, decision,
                limits=Limits(max_turns=1, max_tokens=4096))
    result = core.run('r', {'prompt': 'Return only the integer sum of 1 and 1.'}, ArithmeticEnvironment,
                      contract_version='v1', mode='open_source_first')
    assert result['status'] == 'passed' and openrouter.requests == []
    # 1000 × $0.10/M + 200 × $0.50/M
    assert result['costs']['api_usd'] == '0.0002'
    [call] = [p for p in (tmp_path / 'calls').iterdir()]
    record = json.loads((call / 'response.json').read_text())
    info = usage(record)
    assert info['generation_id'] == 'gen-anthropic-msg_0001' and info['provider'] == 'Anthropic'
    assert info['cost_source'] == 'response_usage_cost' and 'anthropic-secret-key' not in record['raw']


def test_workspace_id_is_sent_only_when_configured():
    api = AnthropicAPI()
    vendor = AnthropicVendor('anthropic-secret-key', PRICES, transport=httpx.MockTransport(api),
                             workspace_id='wrkspc_test01')
    http = httpx.Client(transport=VendorTransport(httpx.MockTransport(OpenRouterAPI()), {'anthropic': vendor}))
    assert post(http, 'anthropic:claude-haiku-5-5').status_code == 200
    assert api.requests[0].headers['anthropic-workspace-id'] == 'wrkspc_test01'
    assert vendor.descriptor()['workspace_scoped_by_header'] is True
    plain = AnthropicAPI()
    http, _ = routed(plain)
    post(http, 'anthropic:claude-haiku-5-5')
    assert 'anthropic-workspace-id' not in plain.requests[0].headers
    with pytest.raises(ValueError):
        AnthropicVendor('k', PRICES, workspace_id='bad id with spaces')
