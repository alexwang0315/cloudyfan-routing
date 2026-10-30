---
name: mrc-task
description: 把一件文字子任務交給本機 CloudyFan MRC（模型路由核心），讀回答案、各層實際模型、Jev 選池理由與費用。使用者要求「交給 MRC」「用 CloudyFan 路由跑跑看」或要觀察 MRC 如何選模時使用。
---

# 交給 CloudyFan MRC 的子任務

Claude Code 在這裡是 MRC 的**一般客戶**：只能送題目、看自己任務的結果。模式（cloudyfan、low_first 等）與模型池由 Monitor 操作員決定，客戶不能指定。

## 前置條件

1. 本機已有 Monitor 服務在 loopback 執行。目前唯一不花錢的方式是示範服務（上游為 MockTransport）：

   ```bash
   ./scripts/preview --legacy-demo --port 18791 --data <全新的資料夾>
   ```

   在背景執行；資料夾要用新的，不要覆寫 `runtime/` 下既有的證據資料夾。
2. 設定位址：`export CLOUDYFAN_MRC_ORIGIN=http://127.0.0.1:18791`。
3. 憑證：示範服務加 `--demo`，使用公開的示範客戶憑證（服務必須自報為示範模式）。其他服務由使用者提供 `CLOUDYFAN_MRC_TOKEN`；不要從 `.env` 或其他檔案自行讀取，也不要把憑證放進指令參數或輸出。

## 用法

```bash
./scripts/mrc-task --demo check
./scripts/mrc-task --demo ask --prompt "題目文字"
echo "較長的題目" | ./scripts/mrc-task --demo ask
./scripts/mrc-task --demo show <run_id> [--full]
```

- `ask` 會產生 `cc-<時間>-<亂數>` 的 run ID；指定相同 `--run-id` 與相同內容重送，只會讀回原結果，不會重新派送或重複計費。
- 輸出為 JSON 摘要：`status`、`answer`、`path`（每一層模型與是否通過）、`jev_decisions`（觸發原因、回報選項、實際選池、保守 fallback）、`costs`。
- 結束碼：0 通過；1 未通過或無法確認；2 工程錯誤（JSON 內有 `code`）。

## 回報時注意

- 輸出含 `"demo": true` 時，答案、模型與費用全是合成資料。向使用者回報時要明講，不能說成真實模型能力或降本成果。示範的驗收器只認算術題，其他題目的「通過」沒有意義。
- `unverifiable`（無法確認）不等於失敗；`costs.unknown_events` 大於 0 代表有費用未知。
- 不送任何機密：`.env` 內容、金鑰、個資都不能放進題目。
- 失敗不要自動換 run ID 重試；先把錯誤碼回報給使用者。
