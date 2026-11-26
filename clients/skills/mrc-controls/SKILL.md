---
name: mrc-controls
description: 使用者透過 MRC（CloudyFan 模型路由）送題時，讓使用者用文字指令控制流程：單題標記（#高風險、#路徑二、#模型 sonnet-xhigh、#不升級、#上限 0.5）與整段對話設定（/風險 高、/路徑 自動、/升級 關、/上限 無）；偵測到可能高風險時先問使用者；每題回報一行路徑、模型、風險、花費。觸發：送題給 MRC 顧問先分流契約、使用者打出上述標記或指令、問「這題走哪條路／花多少」。
---

# MRC 使用者控制

MRC 的「顧問先分流」模式（ADR 0038）會自動判斷每題走哪條路：
- 能驗收的題走路徑一：便宜模型先答，答錯才升級。
- 開放題走路徑二：開源顧問先試著自己回答，沒把握就派送，預設派給 Sonnet，高風險派給 Fable。自動判斷不一定合每個人的需求，這個 skill 讓使用者直接下指令改流程。

這份 skill 只負責把使用者的指令翻成 MRC 的 `overrides`，並把結果報給使用者。能不能用、准不准，一律由 MRC 逐項檢查決定。

## 指令

解析規則寫在 `cloudyfan_jev.controls`，不要自己猜。下表方便你向使用者說明。

| 要控制的 | 只套用這一題（放在訊息開頭） | 整段對話（單獨一行，直到改回） | 對應的 `overrides` |
| --- | --- | --- | --- |
| 風險（開放題派送用哪個模型） | `#高風險`、`#一般風險` | `/風險 高`、`/風險 一般` | `risk`: `high`／`normal` |
| 路徑 | `#路徑一`、`#路徑二` | `/路徑 一`、`/路徑 二`、`/路徑 自動` | `route`: `path1`／`path2` |
| 模型 | `#模型 sonnet-xhigh` | `/模型 sonnet-xhigh`、`/模型 自動` | `setting`：池內設定識別 |
| 升級 | `#不升級` | `/升級 關`、`/升級 開` | `escalation`: `off` |
| 花費上限 | `#上限 0.5` | `/上限 0.5`、`/上限 無` | `max_usd`：字串金額（美元） |

單題標記優先於整段設定。「自動」「無」「開」表示交回 MRC 的預設判斷。指定模型時，這題只用那個設定作答，不經顧問、不會升級。

在 Claude Code 裡，以 `/` 開頭的字可能被當成斜線指令。裝了 `mrc-controls` mod 時改用 `/mrc 風險 高` 這種寫法，或直接用控制面板。沒裝 mod 時改用單題標記。

## 每則訊息的流程

1. **解析。** 把使用者這則訊息原文從標準輸入送進：

   ```bash
   python -m cloudyfan_jev.controls prepare --state <這段對話的設定檔>
   ```

   要在 cloudyfan-mrc 的環境中執行。設定檔放在這段對話專用的路徑，同一段對話一直用同一個。輸出是 JSON，包含 `prompt`、`overrides`、`needs_confirmation`、`risk_signals`、`notice`。
   - 這則訊息若附有 `mrc-controls（mod）` 的說明區塊，表示使用者在控制面板上設定了單題標記。把區塊裡的標記接在訊息開頭，再一起送進來。
   - 回傳 `{"error": ...}` 時，把錯誤原因照實告訴使用者，不送題。

2. **只有設定、沒有題目**（`prompt` 是空字串）：回覆 `notice`（例如「目前設定：高風險／路徑 自動分流／…」），不送題。

3. **可能是高風險**（`needs_confirmation` 是 true）：先問使用者，不要替使用者決定。
   - 說明命中了哪幾類：`risk_signals` 的 `label`，例如「對外發布、牽涉客戶」。
   - 說明大約多花多少：用 `python -m cloudyfan_jev.controls definition` 輸出的 `high_risk_note`。
   - 使用者回答後，用同一則訊息重跑：`prepare --state <同一個設定檔> --risk high`（或 `normal`），再往下走。

4. **送題。** `POST /v1/tasks` 帶 `run_id`、`contract_id`、`prompt`。`overrides` 不是 null 時才加上這個欄位，原樣送出，不要改寫或補欄位。不帶 `overrides` 的題目，MRC 的行為和以前完全一樣。

5. **回報。** 拿到結果後（`GET /v1/tasks/<run_id>/result` 的 JSON），從標準輸入送進：

   ```bash
   python -m cloudyfan_jev.controls report --state <同一個設定檔>
   ```

   把印出的「本題：…」那一行原樣附在回答最後，例如「本題：開放題／anthropic:claude-sonnet-5-5 xhigh／一般風險／約 US$0.11」。第二行「累計：…」是這段對話的累計花費，使用者問起時再提。

## MRC 拒絕時的錯誤碼

| 錯誤碼 | 白話說明 | 下一步 |
| --- | --- | --- |
| `override_not_allowed` | 這份合約不允許改這一項（或完全不收覆寫） | 告訴使用者這項在這份合約不能改；拿掉那個標記再送 |
| `unsupported_override_field` | MRC 不認得的控制項 | 回報給維護者，這是 skill 與 MRC 版本不一致 |
| `invalid_override_value` | 值不合規（例如上限不是正的金額） | 請使用者改值 |
| `setting_not_in_pool` | 指定的模型不在這份合約的模型池裡 | 請使用者改用池內設定，或改回 `/模型 自動` |
| `max_usd_exceeds_contract` | 上限超過合約允許的最高金額 | 請使用者降低上限 |
| `max_usd_below_worst_case` | 這題最壞情況可能花超過上限，MRC 在花任何錢之前就拒跑 | 建議提高上限，或加 `#不升級`、`#模型 <較便宜的設定>` 降低最壞花費 |
| `override_conflict` | 指定模型又要求自動升級，兩者矛盾 | 拿掉其中一個 |
| `frozen_task_changed` | 同一個 `run_id` 已用別的設定送過；題目建立後設定就凍結 | 換一個新的 `run_id` |

## 不能繞過的規則

- 指定的模型必須在合約的模型池內，也要有已確認的費用上界、並計入預算預留。介面上能點、指令能打，不代表 MRC 會接受。
- 設定在建立題目時凍結，續跑不能改。
- MRC 會同時記下「分流顧問原本的判斷」與「覆寫後的結果」。這些紀錄用來校正分流提示，不會自動改路由（ADR 0017）。
