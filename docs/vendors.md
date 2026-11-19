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
OpenRouter 的模型可寫 `openrouter:作者/模型`（送出時去掉前綴；`accepted_models` 要同時列帶前綴的位址與不含前綴的名稱，因為 OpenRouter 回報的是後者）或維持舊寫法。

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

## 試跑等級的費用上界（`pilot_bounds.ListPriceBounds`）

白話：真實試跑前，系統要先知道「每次呼叫最多花多少」。正式的檢查器要有供應商簽署的合約才放行，試跑沒有；
這裡用「牌價 × token 上限」算出每次呼叫的最壞花費，並在每次送出前核對請求沒有超過這些上限。

- 上界 = 輸入牌價 ×（輸入位元組上限 ＋ 餘裕）＋ 輸出牌價 × 輸出 token 上限；Jev 分類用每次固定價（`per_call_usd`）。
- 送出前逐筆檢查：模型有定價、`max_tokens` 不超過上限、訊息與工具定義的位元組數不超過上限；任一不符就不送。
- **限制**：這是牌價推算，不是供應商保證。廠商調價或把推理 token 另計時，實際費用可能超過上界；帳本會記下並停止
  後續派送，但那一筆不封頂。真正的硬上限是設了額度上限的專用金鑰。只用於使用者授權的試跑，不是正式資格。
- 已測：`tests/test_pilot_bounds.py` 4 項（上界計算、未定價或上界過低拒絕、超限請求送出前擋下、接上 `BudgetedCore` 實跑）。

## 階段 0 連線驗證腳本（`cloudyfan_jev.pilot`）

**會發出真實、要付費的請求**，只在使用者當次授權後執行。

```bash
# 不收費：列出 OpenRouter 上名稱含 gpt-oss 的模型與價格
python -m cloudyfan_jev.pilot models --openrouter-key-file <金鑰檔> --match gpt-oss
# 付費（上限 US$1）：每個模型各跑一題極短的算術題
python -m cloudyfan_jev.pilot connect --root runtime/pilot-connect-01 --cap 1 --confirm-paid \
  --anthropic-key-file <金鑰檔> --anthropic-model claude-haiku-5-5 --anthropic-model claude-sonnet-5-5 --anthropic-model claude-fable-5-2 \
  --openrouter-key-file <金鑰檔> --openrouter-model <作者/模型> --openrouter-provider <供應商代號> \
  --openrouter-provider-name <供應商名稱> --openrouter-input-price <每百萬輸入價> --openrouter-output-price <每百萬輸出價>
```

- 金鑰檔必須是一般檔案、剛好一行；內容只交給連線層，不顯示、不寫入收據或報告。
- 沒加 `--confirm-paid` 一律拒絕。
- 每題走完整堆疊：廠商路由、收據、模型身分核對、預算預留、牌價上界。超過階段上限就不送出。
- 輸出每個模型一行（狀態、token、費用與費用來源），並寫 `connect-report.json`。
- 已測：`tests/test_pilot_connect.py` 6 項（MockTransport）。**尚未對真實廠商執行過。**
- 金鑰也可用 `--anthropic-env-file <.env 路徑> --anthropic-env-name ANTHROPIC_API_KEY`：腳本只取出這一個變數，其他變數不保留，錯誤訊息不帶檔案內容。
