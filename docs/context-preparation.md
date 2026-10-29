# 參考區塊與確定性擷取準備（Issue50）

## 新任務格式（明示選用）

`POST /v1/tasks` 可以在原本的 `run_id`、`contract_id`、`prompt` 之外，加上：

```json
{"context": {"format": "reference-blocks-v1", "compression": "extractive-v1",
             "blocks": [{"id": "a", "text": "...", "omittable": true, "priority": 0}]}}
```

- 沒有 `context` 的舊格式完全不變。
- 欄位驗證：
  - 未知欄位、重複 `id`、`id` 格式不符、優先序超出 0–1000、非文字內容，都拒絕。
  - 區塊內偽造的 `role` 等欄位也拒絕，不能用來降級系統或工具權限。
- 契約要有 `RegisteredContract.context_policy` 才接受這個格式，否則回 `context_preparation_not_registered`。政策會寫進註冊描述，並和任務一起凍結。

## 準備規則（`context_preparation.prepare`）

- **只做整塊保留或省略**：沒有 LLM 摘要、embedding、改寫，也不呼叫工具或來源；不在區塊內截斷，原 bytes 不變。
- **原 prompt 永遠受保護**，排在最前面。區塊用固定框架 `\n\n[參考區塊 <id>]\n<text>` 依原順序附加。
- **可省略要兩邊同意**：呼叫端標 `omittable: true`，且政策 `allow_omit` 也允許，才可省略；否則照樣保留，並記錄原因 `server_policy_disallows_omission`。
- **先保留必要內容**：預算為 `max_prepared_bytes − framing_reserve_bytes − output_reserve_bytes`。必要內容超過預算，就在付費前回 `required_context_over_limit`。
- **再選可省略區塊**：依優先序（數字小者先）、再依到達順序放入；放不下的大區塊整塊省略（`remaining_budget_insufficient`），繼續看後面還能放得下的。最終內容維持原順序。
- **上限**：輸入總量、區塊數、單塊大小與準備時間都有上限。
- **其他付費前拒絕**：
  - 政策要求 token 保障時，第一版沒有綁定 tokenizer 與精確模型的可信計數方法，回 `token_method_untrusted`，不用字元平均冒充硬限制。
  - 政策的 `qualification.content_preparation_version` 不等於準備版本時，回 `context_preparation_not_qualified`。未壓縮的資格或合成測試，不會自動變成這個準備版本的正式資格。
- **最後一道上下文檢查**：每次實際派送前，仍由 Core 的 `generation_payload` 檢查 `max_context_bytes`。

## 凍結與重送

- `TaskService.create` 在寫入任何紀錄、付費之前完成準備，並把結果存進私有 `job.json` 的 `prepared`。內容包括版本、原 prompt 與送出內容的雜湊、各區塊的長度、決定與原因、政策雜湊、資格，以及實際送出內容。
- 實際派送與排隊準備都透過 `task_prompt(job)` 取用凍結內容。
- 原樣重送只比對原請求與註冊契約，不重新準備或派送。內容、區塊、優先序或格式有任何改變，都回 `frozen_task_changed`。

## Monitor 接線與安全明細

- Monitor 的 `POST /v1/tasks` 接受同樣的選用 `context` 並傳給 Core。綁定指紋涵蓋整個請求，所以同一 `run_id` 改變區塊會衝突。
- 操作員用 `GET /monitor/context-preparations/<client>/<run_id>` 查看；客戶端用 `GET /v1/tasks/<run_id>/context-preparation` 只看自己的任務。內容只有：
  - 準備版本、量測方法、預算、原始與送出長度。
  - 各區塊的長度、優先序、是否可省略、保留／省略與原因。
  - 資格範圍（是否為合成接線）。
  - 實際送出的使用者內容是否與凍結內容一致。
- 區塊識別由呼叫端提供，可能帶語意，所以只給每客戶端隔離的 HMAC 摘要；原文、prompt 與區塊內容都不出現。原文存取到期回 `raw_access_expired`，不重新開啟。
- 桌機在「前綴與快取觀測」頁的任務明細中顯示「參考區塊準備」。
- 驗證範圍為離線：真實 loopback 主服務 HTTP、Core／BudgetedCore／TaskService 搭配 MockTransport，涵蓋新格式、中文與程式內容、省略大區塊、原順序、舊格式、重送與衝突、超限／偽造欄位／未註冊版本拒絕、沒有政策的契約、客戶端隔離與到期；另有 headed 桌機驗收。這是合成接線，不代表一般語意無損、品質不變或節費。
