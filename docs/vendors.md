# 多家雲算力廠商接入層（ADR 0037）

## 白話說明

MRC 以前只會透過 OpenRouter 呼叫模型。現在模型可以寫成「廠商:模型」，例如
`openrouter:google/gemini-4-neon`、`anthropic:claude-fable-5-2`，MRC 會把請求送到對的廠商、用對的金鑰。
沒有前綴的舊寫法照舊走 OpenRouter。

## 怎麼用

```python
import httpx
from cloudyfan_jev.client import Client
from cloudyfan_jev.vendors import AnthropicVendor, VendorTransport

anthropic = AnthropicVendor(anthropic_key, prices)          # prices：每百萬 token 價格，建構時凍結
http = httpx.Client(transport=VendorTransport(httpx.HTTPTransport(), {'anthropic': anthropic}))
client = Client(http, openrouter_key, receipt_root)         # 其餘照舊
```

設定中的模型寫 `anthropic:claude-fable-5-2`，`accepted_models` 也寫同一個位址，`provider_name` 寫 `Anthropic`。
OpenRouter 的模型可寫 `openrouter:作者/模型`（送出時去掉前綴，`accepted_models` 寫不含前綴的名稱）或維持舊寫法。

價格表格式：`{'claude-haiku-5-5': {'input_per_mtok': '0.10', 'output_per_mtok': '0.50', 'long_context_tokens': 100000,
'long_input_per_mtok': '0.50', 'long_output_per_mtok': '2.50'}, ...}`；長上下文三個欄位可省略。

## 已做

- `src/cloudyfan_jev/vendors.py`：`split_address`、`VendorTransport`、`AnthropicVendor`、`translate_request`、`anthropic_cost`。
- Anthropic 轉接：system／user／assistant／工具呼叫與工具結果的雙向轉換、effort、停止原因、拒答、錯誤回應；
  費用由用量與凍結價格計算並標明 `cost_basis`；廠商原始回應留在收據。
- 金鑰隔離：OpenRouter 金鑰不送往 Anthropic；回應落地前遮蔽金鑰。
- 核帳查詢遇到其他廠商的編號，在本機回 404，不把編號送給 OpenRouter。

## 已測（`tests/test_vendors.py`，18 項，全部 MockTransport）

位址解析與未登記前綴拒絕；OpenRouter 前綴去除與原樣通過；Anthropic 的網址、金鑰、版本與請求格式；
回應轉換與費用計算（含長上下文價格、未涵蓋計費項目記為未知）；未定價模型送出前拒絕；工具呼叫往返；
拒答；錯誤回應；經 `Client`／`Core` 跑完一題並留下收據。

## 未做／未知

- **沒有對 Anthropic 或 OpenRouter 發出過任何真實請求**；請求格式是依文件寫的，要第一次小量試跑才算驗證。
- Anthropic 的費用上界驗證（`verify_bound`）與真實服務組裝還沒寫。
- 錯誤請求記 0 元是假設，待對照帳單。
- 不回傳思考區塊、不自動重試、不啟用拒答換模型（見 ADR 0037 第 6 點）。
- 價格表手動維護；目前沒有從廠商自動取得價格。
- 快取用量（cache read／write）沒有定價，出現時費用記為未知。
