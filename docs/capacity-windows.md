# 原始回應的限流窗口

受控派送可註冊 `capacity-windows-v1`，保存供應商要求的等待時間，並在同一實體限額範圍的下一次派送前檢查。未註冊 `windows_version` 的政策沿用原行為。已註冊但缺少正確版本的 backend 時停止派送。

這項證據只限制容量。它不證明供應商健康、候選資格、答案品質、剩餘額度或零費用。窗口到期仍須通過原健康、公開驗收、費用、資料位置、時間及次數限制。既有政策允許的可用性備援仍要求可信身分、明確無答案與已知費用；未知費用或未決傳輸會停止，啟用窗口時也不自動送出帳務 GET。

## 白名單與時間

host 以精確 binding、group、header、format、constraint 註冊規則。支援的 header 為 `retry-after`、`ratelimit-reset`、`x-ratelimit-reset`、`x-ratelimit-reset-requests`、`x-ratelimit-reset-tokens`；`date` 只提供原時間參照。格式須明示為 `retry_after`、`unix_seconds` 或 `delta_seconds`。

整數相對秒數以原回應的保存時間換算。HTTP-date Retry-After 與絕對 Unix 秒數還須有原始、標準 GMT Date，和本機原觀測時間相差不超過一秒。宣告 Unix 秒數的欄位不能改用 HTTP-date。換算結果最多為已註冊的有界窗口（上限一天）。不猜測毫秒或平均用量。

缺失、無效、非有限、過期或矛盾來源留下安全的未知原因；同一實體範圍及限制的同批重設時間互相矛盾時，不選其中一個當成可信結果。未知不因時間經過自行消失，普通壞 header 只影響其明示群組。時鐘倒退或 backend 無法查核時停止派送。

原回應只從 server 控制的私有 intent/response 收據讀取。窗口證據核對保存回應雜湊、call ID、凍結 route、endpoint、模型及實際 wire 雜湊；已註冊外部工具則核對其原 configuration、endpoint 及 wire。原 canonical request 雜湊保留原用途，不稱作實際 wire 證明。模型、適用本機執行與外部工具沿用各自的原費用收據。

## 顯式受控匯入

host 可在 `CapacityWindows` 建立時提供不可改寫的 `controlled_sources`。每筆資料精確包含：

```python
{
    "binding": "controlled-binding",
    "group": "shared-account",
    "scope": {"kind": "account", "ref": "controlled-account"},
    "source_version": "reviewed-rate-source-v1",
    "evidence_ref": "original-rate-evidence-v1",
    "status": "rate_limited",
    "observed_at": original_observed_at,
    "valid_until": original_valid_until,
    "values": [["retry-after", "30"]],
}
```

binding、group、scope 及來源版本必須符合已保存配置中的 `controlled_import` 來源，並有對應的 header 規則。這是明示限流事實及原始時間證據，不從美元點數推導 RPM 或 TPM。原始值只接受白名單與有界內容，HTTP 客戶端不能提供、修改或註冊此資料。

以 `CapacityObserver(store, window_backend=windows)` 連接同一個容量 store 後，操作員既有的 `POST /monitor/capacity/refresh` 才會呼叫 `import_sources()`。GET 不匯入、不重新觀測時間，也不發送任何來源請求。重複匯入只讀取已保存的相同事實，不延長其時間或清除未知。

尚未匯入的來源有持久化 pending 義務。精確涵蓋它的政策可由未啟用狀態首次啟用；啟用後，pending 會阻止停用、切成舊模式、新版本或重新命名來源來繞過限制。匯入與義務完成在同一交易中發布；失敗、缺失或不符的保存資料不視為已完成。成功匯入後，原窗口或未知狀態繼續保護同一實體範圍。

## 查核與界限

`GET /monitor/capacity` 的 `dispatch.windows` 顯示原時間、最早再評估時間、來源種類與版本、不可改寫事件及 pending 義務。有效窗口跨 key、程序與政策版本共用實體 scope，不以可改名的 group 或版本切開計數。實際 429/5xx 和派送前排除分開保存，不推定供應商重置時間或剩餘額度。

每次判斷最多回傳 32 個原證據參照，採最保守的相關未知／窗口；每次明示受控匯入最多 128 筆事實。活動群組投影最多 128 筆，事件投影最多 100 筆，另提供總數及是否完整的欄位。這些是有界投影，不是完整供應商歷史。原紀錄仍留在容量 store；窗口事件與兩個費用帳本分開。

`GET /monitor/snapshot` 的每個已完成任務可顯示原候選 `capacity_window`。投影依該任務的凍結政策、配置、實際 profile 與實體 scope 再核對原證據參照；不拿今天的健康或窗口重算昨天的原因。操作員介面區分送出前跳過、送出後的可用性備援，以及公開驗收後的品質補救。一般客戶只可讀自己的安全結果。

本功能的本機驗證使用真實 Core、HealthStore、TaskService、帳本和 loopback HTTP，所有模型、工具、來源與帳務上游均由 MockTransport 提供受控回應。這些證據不代表真實供應商故障恢復、容量改善或正式服務品質。
