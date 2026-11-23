"""試跑等級的費用上界（ADR 0037 後果；沿用 2026-10-30 試跑計畫的做法）。

每次呼叫的上界 = 牌價 ×（輸入位元組上限 + 固定餘裕）＋ 牌價 × 輸出 token 上限。一個 token 至少佔
一個位元組，所以輸入 token 不會超過位元組數加上訊息框架的餘裕。送出前逐筆核對請求沒有超過這些
上限，否則拒絕。

**限制**：這是牌價推算，不是供應商簽署的保證（正式資格見 provider_bounds／component_bounds）。
廠商調價、或把推理 token 另計於 ``max_tokens`` 之外時，實際費用可能超過上界；那時帳本會記下違規並
停止後續派送，但那一筆不會被封頂。真正的硬上限是設有額度上限的專用金鑰。只用於使用者授權的試跑。
"""
from decimal import Decimal

from .budget import EnforcedBound
from .client import CHAT, DECISIONS
from .receipt_store import _encoded

KIND = 'pilot-list-price-bounds-v1'
_MILLION = Decimal(1000000)


def _text(value):
    text = format(value.normalize(), 'f')
    return text


class ListPriceBounds:
    def __init__(self, prices, *, max_input_bytes, max_output_tokens, overhead_tokens=2000, local_tools=()):
        for name, number in (('max_input_bytes', max_input_bytes), ('max_output_tokens', max_output_tokens),
                             ('overhead_tokens', overhead_tokens)):
            if type(number) is not int or number < 0:
                raise ValueError(name + ' must be a non-negative integer')
        self.prices = {model: dict(price) for model, price in prices.items()}
        self.max_input_bytes, self.max_output_tokens = max_input_bytes, max_output_tokens
        self.overhead_tokens = overhead_tokens
        # 只核可明列的本機確定性工具；預算層在呼叫時傳入的是工具參數，看不到 external 旗標。
        self.local_tools = frozenset(local_tools)
        for model in self.prices:
            Decimal(self.ceiling(model))

    def descriptor(self):
        return {'kind': KIND, 'prices': self.prices, 'max_input_bytes': self.max_input_bytes,
                'max_output_tokens': self.max_output_tokens, 'overhead_tokens': self.overhead_tokens,
                'supplier_guaranteed': False,
                **({'local_tools': sorted(self.local_tools)} if self.local_tools else {})}

    def ceiling(self, model):
        price = self.prices[model]
        if 'per_call_usd' in price:
            return _text(Decimal(price['per_call_usd']))
        tokens_in = self.max_input_bytes + self.overhead_tokens
        # 有長上下文價格時一律用較高的那一組，上界才成立。
        rate_in = max(Decimal(price['input_per_mtok']), Decimal(price.get('long_input_per_mtok', 0)))
        rate_out = max(Decimal(price['output_per_mtok']), Decimal(price.get('long_output_per_mtok', 0)))
        return _text((tokens_in * rate_in + self.max_output_tokens * rate_out) / _MILLION)

    def model_bounds(self):
        return {model: EnforcedBound(self.ceiling(model), 'pilot: list price x token limits, not supplier-guaranteed')
                for model in self.prices}

    def _covered(self, model, bound):
        return model in self.prices and Decimal(self.ceiling(model)) <= Decimal(bound.usd)

    def __call__(self, kind, identity, payload, bound):
        if kind == 'tool':
            # 本機確定性工具沒有 API 費用；外部工具不在試跑範圍。
            return identity in self.local_tools and Decimal(bound.usd) == 0
        return kind == 'model' and self._covered(identity, bound)

    def verify_dispatch(self, client, endpoint, payload, bound):
        model = payload.get('model')
        if not self._covered(model, bound):
            return False
        if 'per_call_usd' in self.prices[model]:
            return endpoint == DECISIONS
        if endpoint != CHAT or type(payload.get('max_tokens')) is not int:
            return False
        size = len(_encoded(payload.get('messages'))) + len(_encoded(payload.get('tools') or []))
        return payload['max_tokens'] <= self.max_output_tokens and size <= self.max_input_bytes

    def verify_post(self, client, endpoint, payload, bound):
        return self.verify_dispatch(client, endpoint, payload, bound)
