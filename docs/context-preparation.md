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


## 逾時、無縮減與超限：付費前回退或拒絕（Issue51）

- 準備結果多了 `outcome`：
  - `prepared`：已整塊選取。
  - `no_reduction`：每個區塊都放得下，送出完整原文。
  - `fallback_original`：準備逾時，付費前回退完整原文。
- 準備逾時時，只有政策 `fallback` 明示為 `original_if_fits`、且完整原文放得下，才在付費前回退原文。回退只送一次，不增加 attempt 或分類。否則依情況拒絕：
  - `context_preparation_timeout`：政策不允許回退。
  - `preparation_unavailable_original_over_limit`：完整原文也放不下。
- 框架與輸出預留已超過上下文時，回 `output_reserve_exceeds_context`，與必要內容超限的 `required_context_over_limit` 分開。
- 長度分開記錄：原始、準備後、實際送出。第一版沒有綁定精確 tokenizer 的本機計數，所以 `token_estimate` 為未知（`token_method: unavailable`），不用字元平均估值；供應商回報的輸入 token 另列。
- 費用預留沿用 BudgetedCore 依每次呼叫可強制上界的保守預留，不因內容變短而降低。預算授權不足時，在任何 POST 前停止。
- 準備在 Core 寫任務紀錄之前完成，所以付費前的拒絕不會留下任務紀錄（零 POST）。Monitor 另存安全的拒絕紀錄 `context-decisions.sqlite`（客戶端、run_id、原因碼、請求雜湊、時間；不存原文），觀測顯示 `rejected_before_dispatch` 與原因。
- 準備耗時用的時鐘是模組層級的 `context_preparation._now`，主機測試可替換，用來驗證逾時與回退。
- 驗證範圍（#51）為離線：Core 單元測試，以及真實 loopback HTTP 搭配 MockTransport，涵蓋逾時回退且只送一次、不允許回退、原文放不下、預留超過上下文、token 方法不可信、無縮減、預算保障不足時零 POST；另有 headed 桌機驗收。


## 接續、工具往返與損壞：沿用凍結內容（Issue52）

- 每次實際派送或接續前，Core 以 `verify_frozen` 核對凍結準備：送出內容雜湊、原 prompt 雜湊、註冊政策版本與政策雜湊；請求有沒有參考區塊，也要和凍結紀錄一致。任何不符都回 `frozen_preparation_unverifiable`，不自動修復、不重新準備、不重送。
- 只有新任務才準備。原樣重送只比對原請求與註冊契約，就算政策或 tokenizer 之後更新，也不會用新規則重寫舊任務；契約改變時照舊回 `frozen_task_changed`。
- 中斷後接續、品質升級與工具往返，每一輪送出的使用者內容都是同一份凍結內容。工具呼叫與回應成對保留；新增的工具結果與公開診斷不自動摘要或刪除，超限依 Core 原本的上下文檢查安全停止。生成次數、分類次數、local_only、工具權限、資料位置與已付費收據回放都維持原規則。
- 前綴觀測把準備版本、結果與區塊選取納入匹配範圍。有參考區塊的任務與一般任務、或選取不同的任務，不互相借用前綴提示；不為保留快取提示而刪除必要內容。
- 操作員的準備明細列出每次實際生成請求是否與凍結內容一致，以及供應商回報的輸入 token；凍結內容損壞時顯示 `unverifiable`，不重開原文。
- 驗證範圍（#52）為離線：Core 測試涵蓋中斷後接續不重新準備、工具往返每輪內容一致且工具呼叫成對、損壞不重送、規則更新不改寫舊任務；Monitor 經真實 loopback HTTP 搭配 MockTransport，涵蓋品質升級後每次派送一致、前綴不跨準備方式借用、損壞顯示不可驗證且接續不送出、停用後原樣重送不重新準備；另有 headed 桌機驗收。
