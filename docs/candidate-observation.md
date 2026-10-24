# 操作員候選版本與資格觀測（Issue 25）

候選池頁增加版本、canonical 類別及觀測模式選擇；每個原主備位置保留，
另列準備階段已保存的排除識別。操作員可用鍵盤展開每個候選的設定、證據
範圍、效期及原因。舊候選原有欄位與操作保留，不以觀測建立派送資格。

## 讀取契約

受控 `assemble_monitor` 及既有 `App` 的操作員入口提供：

```http
GET /monitor/pools?id=<saved-pool-id>&category=<canonical-category>&mode=cloudyfan
```

原回應新增 `operator_observation`，版本為 `mrc-observation-v1`，其 `candidates`
包含目前查詢脈絡、已保存版本、各池原位置與原有排除紀錄。只能用已保存
版本及明確類別／模式查詢，不能傳入原始檔案位置或資格資料。客戶端
`/v1/monitor` 不加入這個操作員投影；既有 task 的 `observation` 字串也不變。

[CandidateObserver](../src/cloudyfan_monitor/candidate_observation.py) 只從已登錄
的受控 Core descriptor 對照精確設定及原 QCS 證據，採欄位與原因白名單。
公開候選旗標、榜分、已登錄契約或示範結果都不授予正式資格。沒有適用證據
的列顯示未提供；跨類別證據顯示無法確認；缺額顯示不適用；未執行的任務
派送授權顯示尚未檢查。合成及研究範圍與正式資格分開。

目前只顯示既有三維證據的引用與估計，不重算、刷新或核定資格。
健康明示未提供、供應商配額明示未知；後續健康接入另有工作單。
完整成本區間仍是估計，不是實付費用或可強制費用上界。

## 驗收範圍

[HTTP驗收](../tests/test_candidate_observation_http.py) 使用真實 loopback HTTP、
受控組裝、身分、Core／BudgetedCore／TaskService／Store，以及 MockTransport
模型上游。涵蓋登錄與資格分離、合成 QCS、未啟用版本、跨類別、缺項、
安全查詢、原排除識別、重複讀取不新增呼叫／費用，以及原任務30天到期。

[桌機入口](../scripts/qa_candidate_desktop.cjs) 啟動獨立的合成 HTTP fixture，
使用已安裝 Chrome 的 1280×800 桌機，保留全頁及 viewport 截圖、HTTP回應
與實際 mock 呼叫紀錄。每次輸出目錄必須全新；未下載瀏覽器或使用真實模型。

```sh
CLOUDYFAN_CORE_ROOT=<prepared-core> node scripts/qa_candidate_desktop.cjs <new-private-output>
CLOUDYFAN_CORE_ROOT=<prepared-core> python3 scripts/check --quick
```

本頁描述本機觀測範圍，不表示正式資格、主線服務接通、部署、真實故障改善
或節費成立。現有任務存取、證據保存、預算、核帳及未知費停止契約保持原義。
