# 顧問先分流與使用者控制（ADR 0038 實作說明）

依據：`docs/adr/0038-mrc-advisor-triage-verifiable-vs-open-tasks.md`（CloudyFan 根目錄，2026-11-26 使用者核定）。
本文記錄實作做了什麼、怎麼接，以及哪些還沒驗證。所有測試都離線（`httpx.MockTransport`），沒有呼叫任何真實模型。

## 白話說明

MRC 多了一種路由模式「顧問先分流」（`advisor_triage`），要明示選用，預設關閉。每題先請 Haiku 5.5 判斷這題能不能驗收：

- 能驗收就走路徑一，便宜模型先答，答錯才升級。
- 開放題走路徑二，也就是顧問先答流程（ADR 0034）。開源顧問先試著自己回答；沒把握就由同一個顧問派給池內設定，預設 Sonnet，高風險改用 Fable（2026-11-26 使用者定案）。

使用者可以在送題時帶 `overrides`，推翻自動判斷。對話裡用 skill 的文字指令，Claude Code 裡用 mod 的面板，兩者送出同一組覆寫。MRC 逐項檢查，不合規的一律拒絕，並回明確的錯誤碼。

## 資料形狀

### `TriageConfig`（`src/cloudyfan_jev/triage.py`）

| 欄位 | 意思 |
| --- | --- |
| `triager` | 分流顧問設定（Haiku 5.5 xhigh）；要明示 `open_source`，分流費照實計入 |
| `pool` | 路徑二顧問可派送、以及「指定模型」可用的池內設定：`{設定識別: 設定（含 tier）}` |
| `path2_default`／`path2_high_risk` | 路徑二派送的第一順位（一般／高風險），必須在 `pool` 內 |
| `path1_mode` | 路徑一沿用的既有模式：`cloudyfan` 或 `cloudyfan_plus`（交卷前評審） |
| `path2_max_tokens` | 路徑二輸出上限，至少 32,000 |

路徑二的顧問本身沿用 Core 的 `AdvisorConfig`，也就是 ADR 0034 的開源顧問與裁判；沒有它就不能建立分流模式。顧問提示是 ADR 0034 的原提示，再加一段 `OPEN_ADVISOR_RULE`：「需要判斷、建議或決策的題目，沒有把握就派送」，一起凍結。設定寫進 `configuration_descriptor()['advisor_triage']`，每題凍結。沒有提供 `TriageConfig` 時，描述、模式清單與預算都和以前一模一樣。

### `overrides`（`POST /v1/tasks` 選填欄位）

| 欄位 | 值 | 作用 |
| --- | --- | --- |
| `risk` | `normal`／`high` | 路徑二派送的第一順位用預設或高風險設定（路徑一不受影響，照實記錄） |
| `route` | `path1`／`path2` | 推翻分流顧問；分流顧問仍會呼叫一次，原判斷照實保存 |
| `setting` | 池內設定識別 | 這題只用這個設定作答一次，不經顧問、不升級 |
| `escalation` | `auto`／`off` | `off`：路徑一只做首答與驗收，不呼叫 Jev、不升級 |
| `max_usd` | 字串金額 | 這題最壞預留超過它就在任何呼叫前拒跑 |

合約（`RegisteredContract`）以 `allowed_overrides` 列出可改的項目。允許 `max_usd` 時必須設 `max_usd_ceiling`。只有 `advisor_triage` 合約可以允許覆寫；其他合約一帶 `overrides` 就回 `override_not_allowed`。合約描述只在允許覆寫時多一個 `overrides_policy`，既有合約的凍結描述不變。

錯誤碼（建立時檢查，不寫任何紀錄、不呼叫模型）如下：

- `overrides_not_object`、`overrides_empty`、`unsupported_override_field`：422。
- `override_not_allowed`：403。
- `invalid_override_value`、`setting_not_in_pool`、`max_usd_exceeds_contract`、`override_conflict`（指定模型又要求自動升級）：422。
- `max_usd_below_worst_case`：409。

覆寫是原請求的一部分，凍結在 `job.json`。用同一個 `run_id` 換覆寫重送會回 `frozen_task_changed`。容量佇列（`scheduler.py`）目前不收 `overrides`，會照舊回 `unsupported_task_fields`。

### 每題流程（`triage.run`）

1. 寫 `manifest.json`，內容是設定描述、題目、模式，以及有帶時的覆寫。
2. 環境必須提供 `verifier_available(task) -> bool`，沒宣告就在任何呼叫前拒跑。
3. 呼叫分流顧問（`run-<id>-triage`）。格式錯或供應商拒收就重問一次（`run-<id>-triage-2`）。仍錯就走路徑二，記為 `triage_fallback`。
4. 用 `effective_plan()` 決定實際路徑。路徑一但建不出驗收器時，改走路徑二，記為 `no_verifier`。
5. 寫 `triage-decision.json`，內容是分流原判斷、覆寫與實際計畫。
6. 分三種執行方式：
   - `ladder`（路徑一）：以 `<id>-p1` 子任務跑既有的 `cloudyfan`／`cloudyfan_plus`，Jev 升級規則一字不改。
   - `advisor`（路徑二）：開源顧問先看題目，自己回答或改派。
     - 改派時第一順位是依風險決定的設定，其後依顧問的有效建議順序。
     - 回答無法驗收就停下，不再改派。
     - 顧問回覆無法使用時照 ADR 0034 結束，記為 `advisor_incomplete`，不設預設接手。
     - 顧問與派送作答的輸出上限都是 `path2_max_tokens`。
   - `single`：指定模型，或路徑一關閉升級時，單一設定作答一次，再用環境的驗收器驗收。
7. `result.json` 保存 `triage`、`plan`、`branch`、`overrides`，走路徑二時還有 `advisor`（顧問的呼叫與決定）。費用另列 `triage_usd`、`advisor_usd`、`judge_usd`。

HTTP 的 `outcome.json` 與 `GET /v1/tasks/<id>/result` 多一個 `routing` 物件，供每題一行回報使用。它只出現在 `advisor_triage` 的題目。

### 預算預留（`BudgetedCore._admit_advisor_triage`）

預留額 = 分流顧問上界 × 2（含重問），加上所有可到達分支中最貴的一條。

- 路徑二永遠可到達，因為分流失敗或建不出驗收器都會轉過去。它照顧問先答流程的預留，以分流池計算：顧問一次，加上每次作答的池內最高上界乘以回合上限、每次作答一次裁判，最多三次作答。
- 路徑一階梯照該模式原本的預留。
- 單一作答以該設定上界乘以回合上限。
- 指定模型時只剩一條分支；關閉升級時，路徑一只算首答。

## 使用者控制

- **共用定義**：`src/cloudyfan_jev/controls.py` 的 `DEFINITION`，內含單題標記、整段指令、高風險判斷清單與面板預設按鈕。`python -m cloudyfan_jev.controls` 提供 `prepare`、`report`、`show`、`definition` 四個指令。
- **對話 skill**：`clients/skills/mrc-controls/SKILL.md`。
- **mod**：`clients/mrc-controls-mod/`，在 Claude Code（終端機、桌面 App、VS Code、手機）上提供：
  - 狀態列：顯示目前設定、上一題的一行回報與累計花費。
  - 「MRC 控制」面板：`/mrc` 開啟，切換風險、路徑、模型、升級與花費上限。
  - 高風險確認按鈕：命中判斷清單、又還沒設定風險時，先擋下訊息，由使用者選「改用高風險送出／維持一般送出／取消」。
  - 面板的設定以 `mrc-controls（mod）` 區塊附在訊息後面，skill 把它當成單題標記，所以兩種前端送出的是同一組 `overrides`。
  - `hooks/definition.ts` 由 `controls definition --typescript` 產生，`tests/test_controls.py` 核對沒有漂移。

## 驗證範圍

- 已做的離線測試：
  - `tests/test_advisor_triage.py`：Core 流程、重問與備案、`no_verifier`、開放題的顧問自答／派送／無法使用、覆寫、續跑不重付、預算預留與 `max_usd`。
  - `tests/test_http_triage_overrides.py`：HTTP 欄位、合約允許清單、池外設定、凍結、不帶覆寫時行為不變。
  - `tests/test_controls.py`：指令解析、高風險提醒與每題回報。
  - mod：`claude plugin validate` 通過；`claude plugin test` 5 項通過；TypeScript 6.0.3 型別檢查無誤。
- 還沒驗證：
  - 沒有用真實模型跑過這個模式。分流準確率只來自 2026-11-26 的 72 題測驗，花費都是牌價計算值。
  - 2026-11-26 開放題顧問先答測驗（盲測計畫 §11.12，顧問暫用 Haiku 5.5 xhigh，10 題）：
    - 用原本的顧問提示：10 題全部自答、從不派送，品質約 6.3～6.4／8，比 Sonnet 低。
    - 加上 `OPEN_ADVISOR_RULE`：10 題全部派送，但顧問的第一順位一律是 Opus xhigh。推測原因是一般模式的池清單不附價格。
    - 開源顧問本身還沒測。
- 待使用者定案：
  - 開放題的池清單要不要附價格給顧問，或把開放題的池限定為 Sonnet（預設）加 Fable（高風險）。
    - 目前派送第一順位固定依風險，不採用顧問的第一選擇。
    - 有驗收器而且沒過時，第二順位起照顧問建議，可能派到 Opus。
    - 池裡放哪些設定是部署設定，不改程式就能限定；附價格要改程式。
  - 顧問回覆無法使用時，目前照 ADR 0034 結束（`advisor_incomplete`），不自動派給 Sonnet。
  - mod 只用測試工具與驗證器檢查過，還沒在 App 裡實際點過面板。
  - 在 Claude Code 輸入框打 `/風險 高` 這類中文斜線字會不會被當成未知指令，沒有測。skill 建議改用 `/mrc 風險 高` 或單題標記。
- 還沒做：
  - Monitor 顯示分流路徑、理由、轉送與備案次數（ADR 0038「後果」），以及 Monitor 轉送 `overrides`。這兩項要在 Monitor 倉庫另外處理。
