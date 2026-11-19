"""多家雲算力廠商接入層（ADR 0037）。

模型以「廠商:模型」定址，例如 ``openrouter:google/gemini-4-neon``、``anthropic:claude-fable-5-2``；
沒有前綴的舊寫法一律是 OpenRouter。``VendorTransport`` 掛在 ``Client`` 使用的 httpx 連線上：
OpenRouter 的請求去掉前綴後照舊送出；其他廠商交給各自的轉接器，轉成該廠商的格式、用該廠商的
金鑰送到該廠商的網址，回應再轉回 MRC 內部統一格式（與 OpenRouter chat completions 相同的欄位）。
路由、收據與預算預留的程式不需要知道廠商。

費用：Anthropic 只回報 token 用量，不回報金額；``usage.cost`` 由用量 × 建構時凍結的價格表算出，
並以 ``cost_basis`` 標明是計算值，廠商原始回應整份留在 ``vendor.response`` 供查核。
"""
from decimal import Decimal
import hashlib
import json
import re

import httpx

from .client import CHAT, GENERATION

OPENROUTER = 'openrouter'
ANTHROPIC = 'anthropic'
ANTHROPIC_MESSAGES = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2026-10-21'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
_FINISH = {'end_turn': 'stop', 'stop_sequence': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length',
           'refusal': 'refusal'}
_MILLION = Decimal(1000000)


def split_address(model, vendors):
    """回傳 ``(廠商, 模型)``。第一個冒號前的字若不含 ``/`` 才可能是廠商前綴（OpenRouter 的
    ``作者/模型:free`` 不是）；前綴不是已登記的廠商就拒絕，不猜。"""
    if not isinstance(model, str) or not model:
        raise ValueError('model address required')
    prefix, separator, name = model.partition(':')
    if not separator or '/' in prefix:
        return OPENROUTER, model
    if prefix not in vendors or not name:
        raise ValueError('Unregistered vendor prefix in model address: ' + prefix)
    return prefix, name


def _money(value):
    text = format(value.normalize(), 'f')
    return text if text != '-0' else '0'


def anthropic_cost(price, usage):
    """用量 × 凍結價格。缺用量、或出現價格表沒有涵蓋的計費項目（快取讀寫）時回傳 None（費用未知）。"""
    if not isinstance(usage, dict):
        return None
    tokens_in, tokens_out = usage.get('input_tokens'), usage.get('output_tokens')
    if type(tokens_in) is not int or type(tokens_out) is not int or tokens_in < 0 or tokens_out < 0:
        return None
    if any(usage.get(key) for key in ('cache_read_input_tokens', 'cache_creation_input_tokens')):
        return None
    threshold = price.get('long_context_tokens')
    long = threshold is not None and tokens_in > threshold
    rate_in = Decimal(price['long_input_per_mtok' if long else 'input_per_mtok'])
    rate_out = Decimal(price['long_output_per_mtok' if long else 'output_per_mtok'])
    return _money((tokens_in * rate_in + tokens_out * rate_out) / _MILLION)


def _system_and_turns(messages):
    system, turns, leading = [], [], True
    for message in messages:
        role = message.get('role')
        if role == 'system':
            if not leading or not isinstance(message.get('content'), str):
                raise ValueError('Only leading text system messages are supported')
            system.append(message['content'])
            continue
        leading = False
        if role == 'user':
            turns.append({'role': 'user', 'content': message['content']})
        elif role == 'assistant':
            blocks = []
            if isinstance(message.get('content'), str) and message['content']:
                blocks.append({'type': 'text', 'text': message['content']})
            for call in message.get('tool_calls') or []:
                blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                               'input': json.loads(call['function']['arguments'] or '{}')})
            if blocks:
                turns.append({'role': 'assistant', 'content': blocks})
        elif role == 'tool':
            content = message.get('content')
            block = {'type': 'tool_result', 'tool_use_id': message['tool_call_id'],
                     'content': content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}
            # 平行工具結果要放在同一則 user 訊息。
            if turns and turns[-1]['role'] == 'user' and isinstance(turns[-1]['content'], list) \
                    and all(item.get('type') == 'tool_result' for item in turns[-1]['content']):
                turns[-1]['content'].append(block)
            else:
                turns.append({'role': 'user', 'content': [block]})
        else:
            raise ValueError('Unsupported message role for vendor translation')
    return system, turns


def translate_request(payload, model):
    """MRC 內部請求 → Anthropic Messages API。不送取樣參數與 thinking 設定（新模型思考常開，
    深度由 effort 控制）；模型的思考區塊不回傳（整段不回傳是 API 允許的做法）。"""
    system, turns = _system_and_turns(payload['messages'])
    body = {'model': model, 'max_tokens': payload['max_tokens'], 'messages': turns}
    if system:
        body['system'] = '\n\n'.join(system)
    effort = (payload.get('reasoning') or {}).get('effort')
    if effort is not None:
        if effort not in EFFORTS:
            raise ValueError('Unsupported effort for Anthropic: ' + str(effort))
        body['output_config'] = {'effort': effort}
    if payload.get('tools'):
        body['tools'] = [{'name': tool['function']['name'],
                          **({'description': tool['function']['description']}
                             if 'description' in tool['function'] else {}),
                          'input_schema': tool['function'].get('parameters') or {'type': 'object'}}
                         for tool in payload['tools']]
        if payload.get('tool_choice', 'auto') != 'auto':
            raise ValueError('Only tool_choice auto is supported for Anthropic')
        body['tool_choice'] = {'type': 'auto'}
    return body


def _message(data):
    text, calls = [], []
    for block in data.get('content') or []:
        if block.get('type') == 'text':
            text.append(block.get('text', ''))
        elif block.get('type') == 'tool_use':
            calls.append({'id': block['id'], 'type': 'function',
                          'function': {'name': block['name'],
                                       'arguments': json.dumps(block.get('input') or {}, ensure_ascii=False)}})
    message = {'role': 'assistant', 'content': ''.join(text) if text else None}
    if calls:
        message['tool_calls'] = calls
    return message


class AnthropicVendor:
    """直連 Anthropic Messages API 的轉接器。``prices`` 是 ``{模型: 每百萬 token 價格}``，
    建構時凍結；沒有價格的模型在送出前拒絕。不自動重試、不啟用拒答時換模型（模型身分須與設定一致）。"""
    name, provider_name = ANTHROPIC, 'Anthropic'

    def __init__(self, key, prices, *, transport=None, api_version=ANTHROPIC_VERSION, url=ANTHROPIC_MESSAGES,
                 timeout=600, workspace_id=None):
        if not isinstance(key, str) or not key:
            raise ValueError('Anthropic API key required')
        # 沒有綁定工作區的金鑰，每個請求都要帶 anthropic-workspace-id（階段 0 實測得知）。
        if workspace_id is not None and not re.fullmatch(r'[A-Za-z0-9_\-]{1,128}', workspace_id):
            raise ValueError('Invalid Anthropic workspace id')
        self._workspace_id = workspace_id
        self._key, self.url, self.api_version = key, url, api_version
        self.prices = json.loads(json.dumps(prices))
        for model, price in self.prices.items():
            for field in ('input_per_mtok', 'output_per_mtok'):
                if Decimal(price[field]) < 0:
                    raise ValueError('Invalid price for ' + model)
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    def descriptor(self):
        """凍結進設定的部分：網址、API 版本與價格表（不含金鑰）。"""
        return {'vendor': self.name, 'url': self.url, 'api_version': self.api_version, 'prices': self.prices,
                'workspace_scoped_by_header': self._workspace_id is not None}

    def _json(self, status, body):
        text = json.dumps(body, ensure_ascii=False).replace(self._key, '[REDACTED]')
        return httpx.Response(status, content=text.encode(), headers={'content-type': 'application/json'})

    def handle(self, payload, model, address):
        price = self.prices.get(model)
        if price is None:
            return self._json(400, {'provider': self.provider_name,
                                    'error': {'code': 400, 'message': 'vendor_model_not_priced'}})
        body = translate_request(payload, model)
        headers = {'x-api-key': self._key, 'anthropic-version': self.api_version,
                   'content-type': 'application/json'}
        if self._workspace_id is not None:
            headers['anthropic-workspace-id'] = self._workspace_id
        response = self._http.post(self.url, json=body, headers=headers)
        try:
            data = response.json()
        except ValueError:
            data = None
        vendor = {'name': self.name, 'api_version': self.api_version, 'http_status': response.status_code,
                  'request_id': response.headers.get('request-id'), 'response': data}
        if response.status_code != 200 or not isinstance(data, dict) or data.get('type') != 'message':
            error = (data or {}).get('error') if isinstance(data, dict) else None
            digest = hashlib.sha256((vendor['request_id'] or response.text).encode()).hexdigest()[:24]
            status = response.status_code if response.status_code != 200 else 502
            # Anthropic 不對失敗的請求計費（ADR 0037 的假設，首次試跑對照帳單確認）。
            return self._json(status, {
                'id': 'gen-anthropic-err-' + digest, 'provider': self.provider_name,
                'error': {'code': status, 'message': (error or {}).get('type', 'vendor_error')},
                'usage': {'cost': '0', 'cost_basis': 'vendor_error_not_billed'}, 'vendor': vendor})
        usage = data.get('usage') or {}
        cost = anthropic_cost(price, usage)
        returned = data.get('model')
        canonical_usage = {'prompt_tokens': usage.get('input_tokens'),
                           'completion_tokens': usage.get('output_tokens'), 'vendor_usage': usage}
        if cost is not None:
            canonical_usage.update(cost=cost, cost_basis='computed_from_vendor_token_usage')
        return self._json(200, {
            'id': 'gen-anthropic-' + str(data.get('id')), 'provider': self.provider_name,
            # 回報的模型與請求不同時照實呈現，由 Core 的身分核對擋下。
            'model': address if returned == model else self.name + ':' + str(returned),
            'choices': [{'message': _message(data),
                         'finish_reason': _FINISH.get(data.get('stop_reason'), data.get('stop_reason'))}],
            'usage': canonical_usage, 'vendor': vendor})


class VendorTransport(httpx.BaseTransport):
    """依「廠商:模型」把生成請求分送到各廠商；其他請求（Jev 分類、核帳查詢）照舊交給 OpenRouter。"""

    def __init__(self, openrouter, vendors):
        if OPENROUTER in vendors:
            raise ValueError('openrouter is the built-in default vendor')
        self._openrouter, self._vendors = openrouter, dict(vendors)
        self._names = {OPENROUTER, *self._vendors}

    def descriptor(self):
        return {name: vendor.descriptor() for name, vendor in sorted(self._vendors.items())}

    def handle_request(self, request):
        url = str(request.url.copy_with(query=None))
        if request.method == 'GET' and url == GENERATION:
            gid = request.url.params.get('id', '')
            if any(gid.startswith('gen-' + name + '-') for name in self._vendors):
                # 其他廠商的呼叫沒有 OpenRouter 核帳紀錄；不把編號送給 OpenRouter。
                return httpx.Response(404, json={'error': {'code': 404, 'message': 'vendor_generation'}})
            return self._openrouter.handle_request(request)
        if request.method != 'POST' or url != CHAT:
            return self._openrouter.handle_request(request)
        payload = json.loads(request.read())
        vendor, model = split_address(payload.get('model'), self._names)
        if vendor == OPENROUTER:
            if model == payload['model']:
                return self._openrouter.handle_request(request)
            content = json.dumps({**payload, 'model': model}).encode()
            headers = [(k, v) for k, v in request.headers.raw
                       if k.lower() not in (b'content-length', b'transfer-encoding')]
            return self._openrouter.handle_request(
                httpx.Request('POST', request.url, headers=headers, content=content, extensions=request.extensions))
        return self._vendors[vendor].handle(payload, model, payload['model'])

    def close(self):
        self._openrouter.close()
