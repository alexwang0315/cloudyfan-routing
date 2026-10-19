# 有界供應商 429 升級 — 2026-10-19

## 行為

費用已確認、generation ID 唯一且供應商身分證據一致的生成請求，若明確回傳 HTTP429／error.code429，且沒有任何 choices，現在會記為 `provider_rate_limited`，耗用當次 attempt。

- CloudyFan：進入既有 low/mid → 必要時 mid/high Jev 二元選擇，再執行唯一第二次生成。
- low_first／open_source_first：直接使用 high 作唯一第二次生成。
- fixed_high：沒有更高設定可升級，因此停止，不自動重試同一個 high。
- 第二次生成仍遭拒：最終 `unverifiable`，沒有第三次生成。原本兩個 attempt、每 attempt 的 max_turns、最多兩次分類及共同預算預留公式不變。

429 沒有任務答案，因此不呼叫注入驗收器去驗收空字串；公開診斷由獨立版本 `core-provider-diagnostics-v1` 提供 `passed: null` 與 `PROVIDER_RATE_LIMITED`。這是可用性未知，不是模型答錯或成功。Jev 與修正模型只收到這份安全診斷，不收到原始上游錯誤、隱藏答案或私人資訊。

## 費用與身分邊界

維持既有費用來源：API usage.cost 或與 generation ID 相符的核帳收據；核帳取代回應費用，不重複加算。429 可以有非零費用，失敗請求費、分類費及升級費全部計入。

**沒有將 429、GET404 或沒有 token 當成零費。** 若費用缺失，仍先作既有唯讀核帳；無法確認就停止，不送分類或升級。逾時、其他 HTTP 錯誤、部分 answer/tool choice、不符模型／供應商、重複或缺失 generation ID 同樣停止。分類器自己的429不進入生成升級或自動重試。

拒絕回應可能沒有 actual model 欄位；此時以凍結 requested model、供應商錯誤 metadata 及可取得的身分資料核對，保留 actual model 為未知。已提供的 actual model／provider 或 routing requested 欄位若不符，不能以缺欄位例外放行。

上一輪 fresh50 的政策零費是獨立實驗接續的證據，不冒充 production generation 收據。此修正不把該實驗政策硬編碼成通用零費資格，不清除歷史未知費用，也不提升 RG-04 正式資格。未確認費用的同形429仍會停止；應由既有帳務整合先提供可確認的費用證據，不能靠升級功能繞過。

## 保存與驗證

Core descriptor 的 engine_version 從 0.1.0 更新為 0.1.1，避免將新規則套用在舊凍結 manifest。原 fresh50 全部檔案及既有報告保留；沒有補跑、重分級、改寫舊結果或以離線試算宣稱降本。未完成請求可在原費用確認後接續，已付費回應不重送；完成結果可重讀且沒有新 POST。

MockTransport 回歸涵蓋三條二元選擇、直接升級、第二次拒絕、未知初答／修正費、GET核帳與續跑、逾時、其他HTTP、部分choices、模型／供應商／ID異常、預算及舊版manifest隔離。完整結果見 [離線證據](evidence/provider-429-offline-20261019.json)。本次沒有新增真實 API、部署或推送；真實供應商恢復效果尚未驗證。
