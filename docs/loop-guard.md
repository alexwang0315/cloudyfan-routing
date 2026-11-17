# 迴圈守衛裁判（路徑二，#74）

依據：ADR 0034 第 10 點第 2、4、5 點。顧問先答流程見 `docs/advisor-first.md`（#73），設定與預算見 `docs/advisor-first-config.md`（#72）。

## 白話說明

作答模型有時會在同一個錯誤上打轉：同一個工具、同一種錯誤一再出現，浪費回合與費用。迴圈守衛做三件事：

1. **程式先偵測**：同一次作答裡，同一個錯誤指紋（工具名＋錯誤 `code`＋`category`）出現 ≥ 2 次，或工具回報的驗收失敗檢查碼與上一次完全相同，就算「疑似卡住」。供應商限流、無回應、逾時、環境故障屬運作性失敗，不算。
2. **再請便宜的開源裁判確認**：把觸發細節、去重後的錯誤與次數、最近 2 次工具呼叫整理成 ≤ 8 KB 的公開摘要給裁判，裁判只能回 `{"stuck", "confidence", "redirect"}`。把握度 ≥ 0.7 才插一句「換個做法」的提示；每次作答最多問一次裁判。
3. **插過仍卡住就換人**：提示插入後又撞上已重複過的同一錯誤，就結束這次作答（`stop='loop_guard_stuck'`），路徑二照原流程改派顧問的下一組建議。

另有**影子模式**（預設）：照樣偵測、呼叫裁判並記錄「如果是正式模式會不會插提示」，但絕不插提示、不提前結束，作答結果與關閉守衛時相同。這讓我們先收集裁判準不準的證據，再決定是否正式啟用。

## 已做

- `src/cloudyfan_jev/loop_guard.py`（新模組）：
  - `LoopGuardConfig(mode='shadow'|'active', repeat_threshold=2, confidence_threshold=0.7, digest_limit_bytes=8192)`；建構時驗證，`descriptor()` 連同裁判系統提示、提示前綴、運作性失敗清單一起凍結。
  - `failure_signal()`／`Detector`：工具錯誤 `{"ok": false, "error": {"code","category"}}` 依指紋累計；驗收結果 `{"passed": false, "checks": [...]}` 比對排序去重後的檢查碼與上一次是否相同。`Detector(seed_checks=...)` 可帶入上次驗收檢查碼（供 #79 使用）。
  - `build_digest()`：先經 `routing.project_public`（私有欄位如 `expected_answer` 直接拒絕），超過上限就依序把長字串截到 2000／500／200／64 字，最後只留工具名；結果必定 ≤ 上限。
  - `parse_verdict()`：嚴格 JSON，只允許三個鍵；`stuck` 必須是布林、`confidence` 是 0～1 的數字（布林不算）、`redirect` 是字串且 ≤ 500 字，`stuck` 為真時不可為空。不合規一律不插提示。
  - `LoopGuard.turn_hook` 與 `guarded_attempt()`：只依賴 `Core._attempt(..., turn_hook=...)` 與一份裁判設定，不綁定路徑二，#79 可直接沿用。
  - `experience_findings(result)`：把守衛紀錄轉成契約經驗庫（ADR 0036，#76）可收的結構化發現：模型、池內設定、錯誤指紋與次數、裁判判斷、處置、本次作答最後是否通過；不含題目內容。
- `engine.py` 的小改動：
  - `_attempt` 多一個選用參數 `turn_hook`：每個含工具呼叫的回合結束後呼叫，可在對話末尾附提示，或回傳停止原因結束作答。掛鉤提前結束時沒有最終答案，驗收記為 `{'passed': False, 'checks': ['LOOP_GUARD_STUCK']}`（`core-turn-hook-diagnostics-v1`），不呼叫環境驗收器。未傳 `turn_hook` 時行為完全不變。
  - `_advisor_delegated_attempt` 改用 `guarded_attempt`；`judge_calls_per_attempt=0` 時照舊走原本的 `_attempt`，結果也不會有 `loop_guard` 欄位。
  - `_with_costs` 多收 `attempt['loop_guard']['calls']` 的裁判收據，計入 `judge_usd`；裁判呼叫不放進 `attempt['calls']`，作答呼叫清單維持只有作答模型。
- `AdvisorConfig` 多一個 `loop_guard` 欄位（預設影子模式），凍結在 manifest；改模式或門檻後舊題續跑會被拒絕。
- 證據：每次觸發的守衛紀錄另存 `runs/<id>/loop-guard-a<i>.json`（不可覆寫）。裁判收據 `call_id` 為 `run-<id>-a<i>-judge`，續跑讀舊收據、不重付。
- 守衛紀錄的 `action`：`redirected`、`redirected_still_stuck`、`shadow_recorded`、`below_threshold`、`not_stuck`、`judge_invalid`、`judge_rejected`（裁判供應商 429）。

## 已測（全部離線，MockTransport）

`tests/test_loop_guard.py`（36 項）：重複工具錯誤觸發並插提示、單一或不同錯誤不觸發、4 種運作性失敗不觸發、驗收檢查碼相同觸發（順序不同視為相同）／不同不觸發、把握度 0.69 或判定沒卡住不插提示、剛好 0.7 會插、8 種不合規 JSON 不插、裁判 429 只記錄、每次作答最多一次裁判、插過仍卡住即結束並改派下一組（下一次作答有自己的額度）、插過後出現新的不同錯誤則繼續、影子模式作答請求與結果和關閉守衛完全相同、預設為影子模式、摘要 ≤ 8 KB 且只含最近 2 次工具呼叫、私有欄位被擋、經驗發現結構與證據檔、設定凍結與改設定拒絕續跑、續跑不重付裁判、設定驗證、偵測器帶入上次檢查碼。

## 仍未知／未做

- 0.7、2 次、1 次、8 KB 都是起始值；裁判準不準、插提示有沒有用，完全沒有真實數據，需先在影子模式累積紀錄。
- 只看工具結果的兩種形狀（`ok:false`＋`error`、`passed:false`＋`checks`）；其他環境若用不同格式回報錯誤，偵測不到。運作性失敗靠 `category`／`code` 名單判斷，名單之外的運作性錯誤會被誤當成卡住。
- 沒有工具的題目（例如目前的 SQL 題）只有一次最終驗收，同一次作答內不會觸發守衛；跨作答的「與上次相同」只在 `Detector(seed_checks=...)` 層支援，路徑二沒有啟用（每次改派是不同模型、只看原題）。
- 經驗發現只產生結構化資料，寫入契約經驗庫留給 #76。
- 路徑一加強版（#79）已接上，裁判設定由 `CloudyfanPlusConfig` 另給，見 `docs/cloudyfan-plus.md`。
- `http_api` 的 provenance 仍只認一般層級（#73 起已知的限制），未處理路徑二。
- 沒有呼叫任何真實模型；費用數字都是測試假值。
