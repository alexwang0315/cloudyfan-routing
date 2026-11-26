# 顧問先分流與使用者控制（ADR 0038 實作說明）

依據：`docs/adr/0038-mrc-advisor-triage-verifiable-vs-open-tasks.md`（CloudyFan 根目錄，2026-11-26 使用者核定）。
本文記錄實作做了什麼、怎麼接，以及哪些還沒驗證。所有測試都離線（`httpx.MockTransport`），沒有呼叫任何真實模型。

## 白話說明

MRC 多了一種路由模式「顧問先分流」（`advisor_triage`），要明示選用，預設關閉。每題先請 Haiku 5.5 判斷這題能不能驗收：

- 能驗收就走路徑一，便宜模型先答，答錯才升級。
- 開放題走路徑二，由 Sonnet 直接作答；高風險改用 Fable。

使用者可以在送題時帶 `overrides`，推翻自動判斷。對話裡用 skill 的文字指令，Claude Code 裡用 mod 的面板，兩者送出同一組覆寫。MRC 逐項檢查，不合規的一律拒絕，並回明確的錯誤碼。

## 資料形狀

### `TriageConfig`（`src/cloudyfan_jev/triage.py`）

| 欄位 | 意思 |
| --- | --- |
| `triager` | 分流顧問設定（Haiku 5.5 xhigh）；要明示 `open_source`，分流費照實計入 |
| `pool` | 路徑二與「指定模型」可用的池內設定：`{設定識別: 設定（含 tier）}` |
| `path2_default`／`path2_high_risk` | 路徑二預設與高風險用的設定識別，必須在 `pool` 內 |
| `path1_mode` | 路徑一沿用的既有模式：`cloudyfan` 或 `cloudyfan_plus`（交卷前評審） |
| `path2_max_tokens` | 路徑二輸出上限，至少 32,000 |

設定寫進 `configuration_descriptor()['advisor_triage']`，每題凍結。沒有提供 `TriageConfig` 時，描述、模式清單與預算都和以前一模一樣。

### `overrides`（`POST /v1/tasks` 選填欄位）

| 欄位 | 值 | 作用 |
| --- | --- | --- |
| `risk` | `normal`／`high` | 路徑二用預設或高風險設定（路徑一不受影響，照實記錄） |
| `route` | `path1`／`path2` | 推翻分流顧問；分流顧問仍會呼叫一次，原判斷照實保存 |
| `setting` | 池內設定識別 | 這題只用這個設定作答一次，不升級 |
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
6. 分兩種執行方式：
   - `ladder`：以 `<id>-p1` 子任務跑既有的 `cloudyfan`／`cloudyfan_plus`，Jev 升級規則一字不改。
   - `single`：池內單一設定作答一次，再用環境的驗收器驗收。
7. `result.json` 保存 `triage`、`plan`、`branch`、`overrides`。費用另列 `triage_usd`。

HTTP 的 `outcome.json` 與 `GET /v1/tasks/<id>/result` 多一個 `routing` 物件，供每題一行回報使用。它只出現在 `advisor_triage` 的題目。

### 預算預留（`BudgetedCore._admit_advisor_triage`）

預留額 = 分流顧問上界 × 2（含重問），加上所有可到達分支中最貴的一條。

- 路徑二永遠可到達，因為分流失敗或建不出驗收器都會轉過去。
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
  - `tests/test_advisor_triage.py`：Core 流程、重問與備案、`no_verifier`、覆寫、續跑不重付、預算預留與 `max_usd`。
  - `tests/test_http_triage_overrides.py`：HTTP 欄位、合約允許清單、池外設定、凍結、不帶覆寫時行為不變。
  - `tests/test_controls.py`：指令解析、高風險提醒與每題回報。
  - mod：`claude plugin validate` 通過；`claude plugin test` 5 項通過；TypeScript 6.0.3 型別檢查無誤。
- 還沒驗證：
  - 沒有用真實模型跑過這個模式。分流準確率只來自 2026-11-26 的 72 題測驗，花費都是牌價計算值。
  - mod 只用測試工具與驗證器檢查過，還沒在 App 裡實際點過面板。
  - 在 Claude Code 輸入框打 `/風險 高` 這類中文斜線字會不會被當成未知指令，沒有測。skill 建議改用 `/mrc 風險 高` 或單題標記。
- 還沒做：
  - Monitor 顯示分流路徑、理由、轉送與備案次數（ADR 0038「後果」），以及 Monitor 轉送 `overrides`。這兩項要在 Monitor 倉庫另外處理。
