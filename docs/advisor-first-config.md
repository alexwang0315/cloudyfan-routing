# 顧問先答路徑（路徑二）的設定、凍結與預算預留（#72）

依據：ADR 0034 第 3、5、6、7 點。本單只做「設定與帳務的地基」，顧問先答的實際作答流程在 #73。

## 白話說明

路徑二是讓開源顧問先看題目：能答就直接答，答不了就指定池內的模型與強度去做。本單先把這個角色的設定做出來：

- 顧問與裁判各用哪個開源模型、強度、供應商，並用 `open_source: True` 明示它是開源角色。
- 每題開跑時把顧問設定、評分快照與池內可建議的設定凍結進 manifest；之後改設定，舊題重讀或續跑都會被拒絕。
- 開跑前先算出路徑二最壞情況要花多少錢並預留；缺任何一個費用上界就拒跑。
- 記帳時把顧問、裁判分開列出；明示開源的角色在「零模型費用情境」中不計入正式答題成本，但研究全額照收據記錄。

## 已做

- `src/cloudyfan_jev/advisor.py`：`AdvisorConfig`（`advisor`、`judge`、`score_snapshot`、`max_attempts`＝3、`judge_calls_per_attempt`＝1、`pool`）。角色設定必須有 `model`、`model_version`、`effort`、`provider`、`provider_name`、`accepted_models` 與布林 `open_source`。`pool` 省略時以 Core 現行低／中／高設定補上並凍結；每項必須標明 `tier`（low／mid／high）。
- 收據 `call_id` 約定：顧問 `<prefix>-advisor`、裁判 `<prefix>-a<i>-judge`（`advisor_call_id`、`judge_call_id`、`role_of`），供 #73 使用。
- `Core(..., advisor_first=AdvisorConfig(...))`：
  - `configuration_descriptor()` 多一個 `advisor_first` 欄位（未設定時不出現）。
  - `read_completed()` 把 `advisor_first` 納入比對，新增、移除或修改都會被拒絕。
  - `workflow_modes()` 只有在設定顧問時才包含 `advisor_first`；`run(mode='advisor_first')` 會轉給 `run_advisor_first()` 掛鉤，本單中該掛鉤在任何 I/O 前丟出 `NotImplementedError`，等 #73 實作。
  - `_with_costs()` 認得 `advisor`、`judge` 角色（也讀 `result['advisor']['calls']`），以角色設定核對收據身分，並多列 `advisor_usd`、`judge_usd`。
- `BudgetedCore._admit('advisor_first')`：預留＝顧問上界 × 1 ＋ 池內最高上界 × `max_attempts` × `max_turns` ＋ 裁判上界 × `max_attempts` × `judge_calls_per_attempt` ＋ 工具上界 × `max_attempts` × `max_turns` × `max_tool_calls`。顧問直接回答也算一次作答，所以這是保守上限。開源角色同樣需要已確認的上界。
- `ledger.summarize(client, advisor=config)`：每列多 `role`，另列 `roles` 與 `hypothetical_open_source_roles_zero_usd`。

### 兩個「零模型費用情境」數字不是同一個指標

帳本的 `hypothetical_open_source_roles_zero_usd` 和每題結果 `costs` 裡的 `hypothetical_open_source_zero_usd` 名字很像，但算法不同，不能拿來互相比較或對帳：

| 欄位 | 出處 | 哪些呼叫計為零 | 範圍 |
|---|---|---|---|
| `hypothetical_open_source_zero_usd` | `Core._with_costs()`（每題結果） | 池內 `open_source` 層級的作答（開源首答）＋明示 `open_source: True` 的顧問／裁判 | 單題結果引用的呼叫 |
| `hypothetical_open_source_roles_zero_usd` | `ledger.summarize(client, advisor=config)` | 只有明示 `open_source: True` 的顧問／裁判角色；開源層級作答照收據計入（帳本列沒有作答層級資訊） | 整個 client 根目錄的全部收據 |

所以就算只有一題，帳本數字也可能比每題數字高（差額就是開源層級作答的費用）。數字本身維持原樣，沒有改算法。
- `RegisteredContract(mode='advisor_first')` 只有在 Core 有顧問設定時才能產生描述；模式本來就凍結在每題 manifest，中途不切換。

## 已測（全部離線，MockTransport）

`tests/test_advisor_config.py`：設定驗證、凍結與複本、未設定時描述與模式不變、改設定後拒絕重讀與續跑、預留額計算（含工具）、缺上界拒跑、停用裁判時不需裁判上界、舊模式預留不受影響、角色記帳與零費用情境、收據身分不符拒絕、ledger 角色欄位、註冊契約選用檢查。

## 仍未知／未做

- 顧問先答流程、建議解析、池內比對與派送（#73）；迴圈守衛裁判（後續單）。
- 3 次、1 次等數值是 ADR 的起始設定，未經實測驗證。
- `pool` 目前以設定識別對應固定設定；與評分快照入池資格的自動對照留給 #73。
- 沒有呼叫任何真實模型，費用數字都只是測試用假值。

## 顧問候選對照評分快照（2026-11-18）

ADR 0034 第 2 點要求顧問只能建議「現行評分快照中低／中／高池已入池的固定設定」。建立方式：

```python
config = AdvisorConfig.from_snapshot(snapshot, now=now, advisor=..., judge=..., requirements=None)
```

- 候選＝快照 `pools.low/mid/high` 的每筆已入池設定，鍵為 `config_id`，附 `tier` 與 `score_selection`（快照 ID、政策 ID、設定 ID）。開源池不列入；自動換池停用的池（`pool_override.disabled_tiers`）不列入。
- 快照必須通過雜湊驗證且在有效期內，否則建立時就拒絕；可另給 `requirements`（domain、capabilities、context_tokens）過濾。
- 快照身分與有效期凍結在描述的 `pool_source`；**新題**開跑時若快照已過期，在任何模型呼叫前拒跑。已開始的題目續跑沿用建立時凍結的候選（ADR 0001）。
- 舊的手動 `pool`／預設低中高設定照舊可用，描述不新增欄位；正式接入時應改用 `from_snapshot`。
- 未做：主服務在自動換池發布新快照後，要用新快照重建 `AdvisorConfig`（屬主服務組裝，尚未接）。→ 重建工具已補，見下方「換池後重建顧問設定」。

## 換池後重建顧問設定（2026-11-19）

- `AdvisorConfig.refreshed(snapshot, now=...)`：自動換池發布新快照後，回傳以新快照候選組成的**新**設定
  （顧問、裁判、作答上限、迴圈守衛、經驗庫連結沿用）；快照沒變就回傳原設定，原設定不被改動。
- 主服務要把新設定註冊成**新的契約**（新的 Core／契約 ID），舊契約保留：任務服務以建題時凍結的設定核對，
  原地換掉設定會讓舊題讀結果時得到 `frozen_configuration_changed`。
- 已測：`tests/test_advisor_snapshot_pool.py` 新增 2 項（換池後候選層級跟著新快照、舊設定不變、過期快照拒絕）。
- 未做：Monitor 的版本登記（路由池版本 → 契約）還沒有「換池後自動註冊新契約並切換」的流程，需另行決定做法。

## 其他介面認得路徑二（2026-11-19）

- Ollama 介面：`advisor_incomplete` 視為已結束的題目，回 422、`error: advisor_incomplete`，帶最終費用與來源、不帶答案
  （先前被當成「還沒做完」回 409，且沒有最終費用）。
- 任務建議（`task_recommendations`）：凍結的情境接受 `advisor_first` 與 `cloudyfan_plus`。路徑二沒有 Jev 分類，
  建議為「不適用」；路徑一加強版的 Jev 選層與純路徑一相同，分類校準建議照常適用。建議仍然只供觀察、不改路由（ADR 0017）。
