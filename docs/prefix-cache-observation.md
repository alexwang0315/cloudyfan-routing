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

## 明示採用的快取親和性 — Issue47

- 政策 `prefix-affinity-v1` 由操作員在 Monitor 頁或 `POST /monitor/cache-affinity/activate`（需目前 `revision`）明示採用；預設不採用。狀態存在 `cache-affinity.sqlite`（0600）。
- 任務在 `POST /v1/tasks` 被接受前凍結當時的版本；同一任務重送保留第一次的版本。啟用或停用只影響之後建立的任務，既有、重送與歷史任務不重新排序。
- 適用範圍：目前只接到 Backup 執行期（`BackupCategoryCore`）。QCS 計畫每池只有一個已校準設定，沒有可打破的平手，所以不受影響。
- Core 在全部閘門（健康、資格、本機登記、本次失敗、期限、容量窗口）之後，只在同池、完整成本區間與整件速度統計量／區間完全相同的等價集合內，把各候選實際要送出的首輪請求交給 Monitor。Monitor 用與 #46 相同的前綴鏈，只比較同一客戶端、同一設定、實際送出且有回應、在本機提示窗口內的先前請求，回傳最長相同前綴的候選。沒有提示、超窗、錯配或未登記支援時不偏好任何候選。
- Core 只接受等價集合內的偏好，否則照原排序並記錄原因（`affinity_preference_outside_equivalent_set`、`affinity_port_failed` 等）。決定寫進 Core 的選擇 checkpoint，重播只接受等價集合內的保存選擇。
- Monitor 對每個任務、每個決策點只決定一次並保存，所以排隊與實際執行讀到同一筆。
- 換成另一個候選時，既有經濟規則仍要求精確分類校準；缺少時照舊直接選模，不借用其他配對，也不增加生成或分類次數。費用預留沿用原本按池保守計算的上界，不因快取提示降低；不預熱、不探測、不競速。
- 操作員在任務觀測中看到：凍結版本、主機提示、Core 實際採用／略過（等價候選、原排序、實際選擇、原因），以及實際送出的模型。客戶端自己的觀測不含這些內部資料。
- 時間：提示窗口與收據的 `started_at` 都用真實時鐘；原文存取到期仍依服務時鐘。
- 驗證範圍為離線：Core 單元測試（等價集合、各種略過、容量閘門優先、重播竄改）、真實 loopback HTTP 搭配 Backup Core 與 MockTransport（未採用不改排序、採用後打破平手並保存、健康閘門優先、只影響新任務、啟用需操作員與目前 revision），以及 headed 桌機驗收。合成接線不代表真實快取命中或降本。

## 費用意義分開呈現 — Issue48

操作員的任務觀測對每個請求列出下列欄位，任務另有費用摘要：

| 欄位 | 來源與意思 |
| --- | --- |
| 快取結果 `cache_result` | `hit`：回應回報讀取量大於 0。`miss`：回報讀取量為 0。`unknown`：沒有回報讀取量，不等於未命中。寫入量另列。先前命中不保證下次命中。 |
| Token 用量 | 回應 `usage` 的 `prompt_tokens`、`completion_tokens`；沒有就是「未回報」。 |
| API 實付 `api_cost` | 只取 BudgetedCore 帳本的權威值一次。`authoritative`：有金額。`unknown`：待核、不填零。`not_recorded`：帳本沒有這筆。核帳以既有介面、同一原收據身分取代原值，不加算。不用 token 差、公開牌價或推測折扣重算；供應商沒有快取子項時，照樣保留原本的總額。 |
| 保守費用上界 `conservative_bound_usd` | 該請求在帳本記錄的可強制上界。這是限制，不是花費，也不會被快取折扣降低。 |
| 原預留 | 任務層的預留金額與狀態。這也是上界，不是花費。 |
| 客戶用量費 | 依任務接受時固定的費率，來自客戶帳本；有待核項目時標待核。不因快取觀測折扣、補扣或退款。 |

- 客戶端自己的觀測只看到客戶用量費；API 實付、上界與預留屬操作員資料。
- 讀取不新增核帳、來源或模型請求，也不改寫原收據或帳本。
- 驗證範圍為離線：真實 loopback HTTP 搭配 MockTransport 的合成回應，涵蓋寫入（miss）、讀取（hit）、沒有快取子項、未知費用以及之後以既有核帳取代、客戶端可見範圍；另有 headed 桌機驗收。
