# 前綴與快取觀測 — Issue46

操作員可在 Monitor 的「前綴與快取觀測」頁，或透過 `GET /monitor/cache-observations/<client>/<run_id>`，查看某個任務實際送出的每個請求：

- 是否有可匹配的固定前綴提示。
- 供應商回應是否回報快取用量。

客戶端只能用 `GET /v1/tasks/<run_id>/cache-observations` 查看自己的任務。`GET /monitor/cache-observations` 只列任務身分與已登記的供應商支援，不讀收據內容。

## 只讀，不改變路由

- 資料只來自 Core 已寫入、不再修改的 `calls/<call_id>/intent.json` 與 `response.json`，以 `run-<任務內部 ID>-` 前綴對應任務。
- 不新增 cache-control、不改 TTL、不重排訊息或工具、不預熱、不探測、不固定會話，也沒有答案快取。
- 觀測不影響原路由排序。任何 GET 都不送出模型、來源或核帳請求。

## 前綴怎麼算

- **前綴**：實際序列化訊息中，位於第一則含任務 prompt 的訊息之前、逐項完全相同的連續訊息。含 prompt 的訊息是動態內容，不納入前綴，即使兩個任務的 prompt 相同也一樣。不使用相似度、子字串或只看模型名稱。
- **匹配範圍**：只比較同一客戶端、仍在合法原文存取期內、實際送出且有回應的先前請求。以下任一項不同就不借用：
  - 協定（端點）。
  - 序列化版本 `jev-client-json-v1`。
  - 請求設定，排除 `messages`、每個任務不同的 `session_id`，以及不影響前綴的 `max_tokens`。
  - 模型設定：`model_version`、`provider`、`effort`、`execution_location`、`quantization`。
  - 帳號：執行器的收據根目錄，只以帶密鑰摘要比對。
- **身分摘要**：以 Monitor 主機私密金鑰 `.cache-observation.key`（0600，不輸出）衍生每個客戶端自己的 HMAC 金鑰，逐則訊息累積成鏈。公開投影只列訊息數、位元組數與 32 字元摘要，不含原文、金鑰或可跨客戶對照的全域指紋；同樣內容在不同客戶端得到不同摘要。

## 狀態分開呈現

| 欄位 | 狀態 | 意思 |
| --- | --- | --- |
| 前綴提示 | `matchable_hint` | 本機提示窗口內，有更早的相同前綴請求 |
| 前綴提示 | `expired` | 有相同前綴，但超出本機提示窗口 |
| 前綴提示 | `no_prior_match` | 沒有更早的相同前綴 |
| 前綴提示 | `not_applicable` | 沒有固定前綴，或設定無法對應 |
| 前綴提示 | `unsupported` | 供應商沒有登記相容的快取支援與資料政策 |
| 快取用量 | `observed` | 實際回應的 `usage` 回報 `prompt_tokens_details.cached_tokens`／`cache_write_tokens`，或 `cache_read_input_tokens`／`cache_creation_input_tokens` |
| 快取用量 | `unknown` | 回應有完成但未回報這些欄位；不填零 |
| 快取用量 | `not_applicable` | 只有 intent、未送出或中斷；不能形成熱快取 |
| 快取用量 | `unsupported` | 供應商沒有登記相容的快取支援與資料政策 |

- 本機提示窗口是 300 秒的保守值，與供應商來源 TTL 分開列出；目前沒有供應商提供 TTL。先前命中不保證下次命中。
- 供應商支援由主機設定 `app.cache_support` 明確登記，例如 `{'example': {'source_ttl_seconds': None, 'data_policy': 'compatible'}}`。預設是空的，所以未登記的供應商一律顯示 `unsupported`；這不影響它依原政策作答。

## 原文到期與限制

- 原文存取到期、生命週期不是 active 或不存在時，回傳 `raw_access_expired`，不重新開啟 prompt 或請求內容。
- 上限：每個任務最多 32 個請求、比對同一客戶端最近 64 個任務、單檔 1 MiB。超過時標示，不冒充完整。

## 驗證範圍

- 驗證為離線：真實本機 loopback 主服務 HTTP 搭配 MockTransport。
- 涵蓋：同前綴提示、其他客戶端／模型／未登記供應商不借用、回應回報快取用量、超出提示窗口、客戶端隔離與到期不重開、只有 intent 或中斷不形成熱快取、動態 prompt 不納入前綴；另有 headed 桌機驗收。
- 合成上游不代表真實供應商的快取行為或費用。
