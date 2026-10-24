# 操作員候選版本、資格與健康觀測（Issues 25、26）

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
有精確對應的受控 HealthStore 時，另外提供目前健康觀測；沒有該來源的
契約明示健康未提供。供應商配額維持未知，派送授權仍尚未檢查。
完整成本區間仍是估計，不是實付費用或可強制費用上界。

## 目前健康

每列的 `health` 僅在凍結候選、任務類別、精確設定指紋、供應商及契約內的
健康政策均可信對應時，呼叫既有 `HealthStore.assess`。健康列提供原觀測
時間、目前判定時間、原快照識別、政策指紋、時效、熔斷與冷卻期限，以及
全部適用的安全原因代碼；不以健康通過替代三維資格或執行准入。

健康資料缺少、過期、來源時點不可信、降級、冷卻中與待明確恢復均沿用
HealthStore 的判定。原 TTL 的精確邊界即失效；冷卻時間經過及新觀測發布
都不會自行關閉既有熔斷，仍須受控主服務明確恢復。觀測入口不提供恢復操作。
原生可用率及端點延遲／吞吐量只顯示來源提供的視窗、分位數與單位，不補造
P95 等缺少統計量，也不把端點速度當整件任務速度。

設定、類別或政策對照失敗會明示證據無法確認。單列健康來源損壞則顯示
無法讀取，其他列繼續各自查核；私人錯誤不進入 API 或畫面。原準備排除紀錄
若只有設定識別，繼續保留其原理由，不補造設定、健康或主備位置。

候選頁每次既有輪詢都重新取得目前所選版本／類別／模式的觀測，因此相同
候選版本仍可看到健康過期或熔斷狀態改變。輪詢不發布健康、不紀錄成功／失敗、
不恢復、不探測模型、不更新來源或核帳；目前健康不回填已保存的任務決策。
跨項目完整故障隔離及輪詢焦點／展開狀態保留由後續工作處理。

## 驗收範圍

[HTTP驗收](../tests/test_candidate_observation_http.py) 使用真實 loopback HTTP、
受控組裝、身分、Core／BudgetedCore／TaskService／Store，以及 MockTransport
模型上游。涵蓋登錄與資格分離、合成 QCS、未啟用版本、跨類別、缺項、
安全查詢、原排除識別、重複讀取不新增呼叫／費用，以及原任務30天到期。

[健康 HTTP 驗收](../tests/test_candidate_health_http.py) 使用真正
BackupCategoryCore／HealthStore 與可控時鐘，驗證原 TTL 邊界、冷卻、待明確
恢復、缺少精確供應商資料、多個原始原因、類別／政策隔離及單一合成壞快照。
實際完成一件 MockTransport 任務後，比對原健康資料庫、任務原始證據指紋、
費用／預留及30天到期；既有 Monitor 衍生摘要更新仍可正常進行。

[桌機入口](../scripts/qa_candidate_desktop.cjs) 啟動獨立的合成 HTTP fixture，
使用已安裝 Chrome 的 1280×800 桌機，保留全頁及 viewport 截圖、HTTP回應
與實際 mock 呼叫紀錄。每次輸出目錄必須全新；未下載瀏覽器或使用真實模型。

[健康桌機入口](../scripts/qa_candidate_health_desktop.cjs) 另以隔離合成
HealthStore 驗證同版本自動輪詢、TTL、冷卻、待恢復及鍵盤可讀的多項原因。
僅 fixture 的私人程序管道能設定時間、發布、紀錄或明確恢復；產品 HTTP
沒有這些測試指令。保留讀前／讀後的實際呼叫、費用、健康及原任務證據指紋。

```sh
CLOUDYFAN_CORE_ROOT=<prepared-core> node scripts/qa_candidate_desktop.cjs <new-private-output>
CLOUDYFAN_CORE_ROOT=<prepared-core> node scripts/qa_candidate_health_desktop.cjs <new-private-output>
CLOUDYFAN_CORE_ROOT=<prepared-core> python3 scripts/check --quick
```

本頁描述本機觀測範圍，不表示正式資格、主線服務接通、部署、真實故障改善
或節費成立。現有任務存取、證據保存、預算、核帳及未知費停止契約保持原義。
