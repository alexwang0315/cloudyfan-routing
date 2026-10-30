# 桌機客戶帳務

Monitor 導覽的「客戶帳務」可設定售價、記錄客戶美元入帳、人工核帳並保存扣款。第一版是內部美元帳本；沒有刷卡或銀行轉帳。原費用帳本仍呈現核心的原始成本及未知費，不因客戶調價而改寫。

## 如何計價

- 一般 OpenRouter 請求：使用 `/api/v1/generation?id=…` 的 `total_cost` 乘上有效倍率。金鑰用量或帳戶 credits 不分攤為個別客戶費用，模型牌價也不取代實際帳務。
- 開源／本地服務：對精確模型 ID 設定輸入、輸出各自的 USD／每百萬 token 售價，再套有效倍率；即使研究估算或供應商費用為 0，仍可收取服務費。
- 倍率 `1` 為原價、`0.9` 為九折、`1.2` 為加成20%。客戶覆寫取代全域倍率；移除後沿用全域。`0` 是操作員明示的免費售價。
- 金額以十進位字串保存，單筆客戶費用四捨五入到小數12位。倍率範圍0–100，模型單價上限1,000,000 USD／百萬 token；正數入帳上限1,000,000,000 USD，備註必填。

預設未啟用客戶計費。設定保存後只對之後接受的新任務生效；每件任務固定費率版本、客戶有效倍率與模型單價。已接受任務不隨調價重算，啟用前的舊任務不補扣。缺少售價或可核對的 token 用量時保留待核；事後新增售價不回填該任務原先缺失的設定。

人工「核帳並扣款」逐一處理已結束或中斷任務的各次請求，涵蓋分類、生成與失敗請求中可核實的費用；重複核帳不重複扣款。每批最多20次讀取，經過30秒時間預算後停止安排新讀取，剩餘項目明列待核，須再手動核帳；以持久化的嘗試順序讓未處理及最久未嘗試的項目先行，前批持續404不會擋住後方請求。單次同步HTTP仍受連線／讀取逾時控制，期限只能在標頭或資料區塊到達時檢查，因此不是嚴格的整段牆鐘上限。已確認的部分可先記帳，其餘仍待核；客戶餘額只代表已入帳紀錄，不能視為全部任務的最終結清額。負餘額表示待收款，不會改變核心派送限制。

## OpenRouter 憑證與唯讀資料

只在明示 `--openrouter-billing` 時載入帳務憑證。可指定 `--billing-key-file /absolute/path/.env`，僅解析下列名稱，不執行 shell、不搜尋其他 `.env`，不讀取其他服務設定：

- `OPENROUTER_BILLING_API_KEY`，優先於 `OPENROUTER_API_KEY`。
- `OPENROUTER_MANAGEMENT_API_KEY` 為選用管理金鑰，只供帳戶範圍 `/credits` 查詢。

指定檔案時只使用該檔案，不與環境中的其他帳戶憑證混合；未指定檔案才使用這三個環境變數。啟動、一般頁面讀取與定時更新皆不自動讀取供應商 API；操作員按「更新 OpenRouter 帳務資料」才查詢 `/key` 及已設定管理金鑰的 `/credits`。本輪經使用者授權複製的憑證位於核心專案 `.env`，權限0600且由Git忽略；請勿將它加入提交或輸出內容。

金鑰的所有期間累計用量、每日／每週／每月用量及 `limit_reset` 分開保存與顯示；週期未知時不以累計推算當期。欄位依 [OpenRouter 金鑰 API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key)，實際請求成本依[單次生成帳務 API](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)，帳戶總額依[管理金鑰 credits API](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits)。

```sh
./scripts/preview --port 18767 --data runtime/billing-preview-20261023 \
  --openrouter-billing --billing-key-file ../cloudyfan-mrc/.env
```

此預覽仍使用合成模型與合成客戶帳務；只有明示更新的 OpenRouter 帳務來源可讀真實用量。一般受控主服務使用 `assemble_monitor(..., billing_source=OpenRouterAccounting(...))` 注入同一唯讀適配器，真實核帳時須保留原請求的 generation ID、模型身分與實際原生 token 用量。OpenRouter 回報的費用不包含外部 BYOK 供應商另行收取的費用；帳戶／金鑰範圍分開呈現，管理金鑰未設定時帳戶總額保持未知。

## 權限、重試與證據

操作員可讀寫 `/monitor/billing` 相關管理入口；客戶端只可 GET `/v1/billing` 查看自己的已入帳紀錄、餘額與待核項目，不能讀其他客戶或供應商成本、利潤參數、原始帳務。

入帳使用固定冪等編號：同編號同內容回傳原紀錄，不同內容拒絕。桌機分頁保存尚未確認的編號與入帳內容，沒有保存登入 token；回應遺失後，即使完整重新載入，也須明確確認並沿原編號重試。客戶不存在或分頁儲存不可用時停止入帳並提示核對帳本。關閉分頁後的跨分頁恢復尚未提供，請先核對帳本再建立新入帳單。

供應商成功回應的原始 UTF-8 JSON 與 SHA256 保存在私有 Monitor SQLite 中，頁面與客戶投影不夾帶原文。操作員可透過 `GET /monitor/billing/evidence/<sha256>` 明示查核原始資料。證據沿用[保存邊界](../../docs/adr/0010-monitor-access-and-evidence-retention.md)，本功能沒有實作資料刪除。

實作決策見 [ADR-0012](../../docs/adr/0012-monitor-customer-tariffs-and-internal-usd-ledger.md)；本輪實際驗收結果及未完成邊界將另記於[交接紀錄](main-thread-handoff.md)。
