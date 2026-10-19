# 明確範圍的 429 費用政策接入 — 2026-10-20

`Text429FeePolicy` 是由應用程式明確啟用的政策歸因器，預設 Client 不啟用；不是 generation 收據、費用上界或正式供應商資格。輸入是事先取得的 OpenRouter 官方政策頁內容雜湊、精確模型／供應商清單與有效期限，並納入 Core descriptor／任務凍結資料。

只有固定 Chat Completions 模型及 provider.only、禁止 provider fallback、非 BYOK、純文字 messages、沒有 plugins/tools/stream/附件且明確 upstream shared-pool429無答案的請求才適用。Client 原始 intent／response 先保存；既有 usage.cost 或真 generation 核帳收據優先。唯讀 GET 無法取得收據且符合已載入政策時，另保存 `rejection-fee.json`，綁定 request hash、原始 response hash、generation ID 及政策來源。usage／Core／ledger 使用獨立 cost_source `provider_error_no_charge_policy`，不改寫 API 回覆或偽造 reconciled.json。

已依政策歸因、尚無真實收據的呼叫，若政策不符、逾期、來源或證據變動，就維持未知並阻擋新 POST；非零回應費用或後續實際核帳金額不會被政策零費覆蓋。後續真正費用收據取代政策數字而非相加。這個接入不清除歷史未知費用，不變更既有嘗試上限、分類規則或正式資格。

政策本身不是派送授權或預算上界。尚未使用政策證據的呼叫仍由應用的授權／派送前檢查負責；不應把初始化成功當成永久有效的政策資格。

本輪在獨立目錄複製三筆上輪真429原始檔，只新增唯讀核帳，驗證 GET404 與政策證據區分；上輪原始目錄不改動。再通過50項相關 MockTransport 回歸及完整離線整合；見 [離線證據](evidence/rejection-fees-offline-20261020.json)。新50題實測另以新目錄、新題目及凍結計畫執行，不回寫上輪結果。

## v2 本機提交審查修正

v1 為上述歷史實驗版本。v2 將唯讀核帳與政策零費歸因分開：`reconcile()` 先載入原始回應及真正收據，即使政策過期或來源已改變也能嘗試 GET；只有取得有效真實收據才取代政策零費。GET 仍失敗且政策無效時，保留 GET 證據並維持未知／停止。原始回應與既有政策證據均不改寫。

原始 `response.json` 不允許包含 `reconciliation`、`rejection_fee_evidence` 或 `duplicate_generation_id` 等衍生欄位；它們只能來自各自獨立的受檢查紀錄。混入原始檔時拒絕載入，不能藉由先偽裝成已知費用而跳過政策證據驗證。v2 的 descriptor 版本不同，舊凍結流程不會靜默使用新語義。新增回歸完全使用 MockTransport，不刷新任何真實 API。
