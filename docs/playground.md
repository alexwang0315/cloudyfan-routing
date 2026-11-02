# 模擬試問

## 目的

在 Monitor 頁面輸入一題，看它在 MRC 內怎麼走：每次作答用哪個模型池、公開驗收結果、Jev 怎麼選、花多少錢。用來確認流程與畫面，不用來判斷真實模型的表現。

## 真的與假的

| 項目 | 狀態 |
| --- | --- |
| `Gateway`、`TaskService`、`BudgetedCore`、`Core`、任務紀錄與帳本 | 真的 MRC 程式，與一般任務同一條路徑 |
| 模型回答、模型名稱（`example/*`）、費用、耗時 | 合成資料，上游是 `MockTransport` |
| 每個模型池通過與否、Jev 的選擇 | 由操作員選的情境決定，不是依題目判斷 |
| 答案 | 固定是算術題的 `2` 或 `3`，不是在回答輸入的題目 |

頁面全程標示模擬。這裡的結果不能當成真實模型的選模、成本或正確率證據。

## 邊界

- 只有 `gateway.demo` 為真、由 `build_demo` 或 `build_local_integration` 組裝的環境才提供；其他環境回 `playground_not_available`。`--simulation50` 目前也不提供。
- 需要操作員憑證：`GET /monitor/playground`、`POST /monitor/playground/ask`。任務以 `studio` 客戶端身分建立，瀏覽器不能指定模型或模型池；模式沿用目前設定。
- 情境存在 `ContextVar`，只影響送出這題的執行流程，不改其他任務看到的上游設定。
- 不讀任何金鑰，也不呼叫真實模型。題目會保存在該模擬環境的資料目錄。

## 試跑

```bash
./scripts/preview --port 18767 --data runtime/playground-preview
```

開啟 `http://127.0.0.1:18767/#playground`。

## 還沒做的事

接真實模型需要專用金鑰、真實模型池與費用上界，也要先決定沒有公開驗收方式的一般問題如何判斷對錯。這些都不在這個頁面的範圍內。
