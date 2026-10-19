# RG-01 工程與離線證據：正式審閱包

日期：2026-10-19。**工程／離線審閱材料，不是正式核定**。基線
`bdc3b084bb2db680afaf42d17cf33b953cbec850`；最新來源指紋、實際命令、
成功／失敗收據及測試數以[本輪離線證據](evidence/rg01-review-ready-offline.json)為準。
歷史 v1 的 991 項結果保留於[前輪證據](evidence/rg01-sql-offline.json)，不能冒充 v2 的驗證。

## 這次集中處理的範圍

固定審查材料為 `sql-review-catalog-v1` 的 S01～S08；每類五組**公開、合成**資料，
不是正式 holdout 或客戶資料，也沒有新增模型實驗。這是可供核定的具體候選範圍，
不自行批准首版範圍、不停用其他入口，也不將一般 SQL 當成已合格領域。

| ID | 任務與明確語義 | 主要反例 |
|---|---|---|
| S01 | 所有訂單 SUM；忽略 NULL；空／全 NULL 為 NULL。 | 錯誤 COALESCE 為零、漏掉 void 訂單。 |
| S02 | paid 訂單；ISO 日期左含右不含；NULL 日期排除；ID 升序。 | 月初排除、下月一日誤收。 |
| S03 | paid、按 customer 分組；NULL 單獨一群；COUNT 包含 NULL amount；保留重複金額。 | COUNT(amount)、SUM(DISTINCT amount)。 |
| S04 | 每位已宣告客戶 LEFT JOIN；沒有 paid 訂單為零；忽略無主訂單。 | COUNT(*) 誤計、INNER JOIN 漏列。 |
| S05 | 沒有 paid 訂單的客戶；NULL 外鍵不匹配。 | NOT IN 的 NULL 陷阱、未區分 void。 |
| S06 | 每個 paid customer 分群取最大 amount；NULL 最後；同額取最小 ID。 | 同額取最大 ID。 |
| S07 | 按 customer／日期／ID 的 ROWS 累計；NULL amount 貢獻零；日期 NULL 最先。 | 把整群總額當逐列累計。 |
| S08 | 文字 UNION 去重；NULL 一次；SQLite BINARY、NULL 最先。 | UNION ALL、反向排序。 |

輸入前提明寫於每個公開 prompt：客戶／訂單 ID 各自唯一且非 NULL；customer
引用可以 NULL 或不存在；amount 為 int64／NULL 且總和不溢位；day 為有效 ISO
日期或 NULL；status 為 paid／void；label 可重複及 NULL。五組包括一般資料、
負值／同日／同額、全空、客戶無訂單、全 NULL amount。
沒有核定浮點／時區／貨幣精度、任意日期字串、溢位語義、遞迴、寫入或外部資料庫。

## 核對方法與完成證據

| 要求 | 實作／檢查入口 | 本輪能證明的事 |
|---|---|---|
| R01 固定語義與資料 | `sql_review.cases()`／`datasets()`；公開契約版本。 | 可重現的有限任務、資料、欄名／順序及假設。 |
| R02 獨立答案 | 每類獨立 Python oracle、第一組另有手算常數；空／全 NULL 專項測試。 | SQL reference 與不同實作的預期結果一致；不是具名人員已批准答案。 |
| R03 反例與誤收 | `sql_review.review()`；每類已知錯誤查詢及複製首組常數答案的測試。 | [有限覆蓋報告](evidence/rg01-sql-coverage.json)中的錯誤被拒絕。不能推得所有錯誤均可拒絕。 |
| R04 子程序與資源 | `sql_process.py`／`sql_worker.py`、`tests/test_sql_process.py`。 | 新 PID、私有有界 pipes、CPU／SQLite allocator／檔案寫入上限、逾時 kill/reap、拒絕繼承環境與 FD。 |
| R05 修正與中斷 | `tests/test_sql_environment.py`、真實子程序逾時＋BudgetedCore 回歸。 | 最多兩個生成階段；工程中斷不觸發分類／修正，已保存答案與預算可續跑且不重送。 |
| R06 公開／隱藏隔離 | 僅傳公開 contract／單一 fixture 至 worker；review-only reference、mutants／oracle 不放入 prompt。 | 新介面沒有 hidden grader 輸入口；公開診斷僅固定 flat codes。既有隱藏評分規則不變。 |
| R07 完整離線驗證 | `./scripts/check --integration`；原始收據及來源 hash。 | 固定版本的本機／模擬上游結果。沒有真實模型、供應商或部署資格。 |
| R08 核定輸入 | 本文件、資格包、覆蓋報告及離線摘要。 | 有明確待核定清單；不代簽 owner／reviewer、不修改正式 gate。 |

## 資源限制的精確邊界

v2 預設用新的標準庫 Python 子程序驗收每個 fixture。使用 `-I -S -B`、空環境、
`close_fds=True`、新 session、無 shell；worker 不匯入 httpx、provider SDK 或憑證。
作業系統可能自行加入 locale／CoreFoundation 環境名稱，不繼承父程序 secrets。
stdin 上限 2 MiB、stdout 上限 256 KiB，stderr 不向模型或呼叫端回傳。
父程序包含 pipe backpressure／等待退出的 3 秒 deadline，逾時會 kill 並 wait/reap；
實際返回仍受 OS 排程與程序建立時間影響，不是即時系統的精確 3 秒服務承諾。

子程序必須驗證 CPU 2 秒、core dump 0、regular-file bytes 0、open files 32，並設定／
讀回 SQLite **hard_heap_limit 32 MiB**。測試包含真正超額配置被拒絕及 CPU 無窮迴圈
被 OS 終止。SQLite 的額度限制的是 SQLite allocator，**不是 Python／整個程序的 RAM**；
檔案額度 0 不表示不能讀檔，也不禁止 pipe／socket。SQL 的 authorizer／函式 allowlist
仍限制可執行操作。此機制不是完整 OS 檔案／網路 sandbox，不對 SQLite/native 漏洞作隔離保證。

要求整個地址空間限制時，設定 `SqlContract(..., require_address_space=True)`；worker
必須成功設定及讀回 256 MiB `RLIMIT_AS`，否則 **preflight 在模型 POST 前拒絕**。
本機 macOS／Python 實測拒絕 `RLIMIT_AS`／`RLIMIT_DATA` 設定；因此該嚴格配置
在此主機不可用，沒有無聲回退。預設與嚴格策略都進契約指紋。
支持該限制的目標 OS 還需要實機資源／配置取證；不能用此處的拒絕測試宣稱 Linux
或任何其他平台已合格。資源限制程式已提供，**目標部署的完整記憶體與安全隔離資格仍待核定**。

## 誤收與品質限制

單一公開範例會被 `SELECT 3 AS total` 這類常數答案通過，前輪回歸保留此證據；
新材料使用不同資料、空集合與 NULL 以拒絕指定常數答案及已知 mutants。
然而任何有限公開資料仍可能被特製 SQL 記住，不能將有限 mutation 全過稱為
一般 SQL 正確性證明。審閱者必須決定任務範圍、可接受誤收風險與客戶實際驗收方式。
需要隱藏評分時，只能在流程結束後獨立評分，不得把其結果回傳選模或修正。

## 正式審閱與保留項

| 決定／證據 | 現況 | 誰需要處理 |
|---|---|---|
| 是否採用 S01～S08 作首版領域，以及實際資料／入口 | 候選，未批准。 | 首版 scope owner。 |
| 獨立答案、語義歧義、反例覆蓋與可接受誤收 | 工程材料具備；具名審閱未完成。 | 領域 reviewer；不能把實作者自檢冒充獨立人員簽核。 |
| 可接受的執行隔離及精確目標 OS | portable process 路徑有本機證據；嚴格 AS 在本機拒絕；完整安全 sandbox 未核定。 | 部署／安全 reviewer 決定適用保障並取證。 |
| 模型資格、供應商費用、實際品質／成本主張 | 本輪沒有新證據。 | 分別沿 RG-02、RG-04、RG-07，不混成 RG-01 尚缺實作。 |

十項正式 gate 全數保留 pending；本輪沒有模型／帳戶 API、供應商聯絡、硬體操作、
模型下載、服務部署或推送。沒有用事後分析覆寫舊題、舊成本、路由規則或門檻。

## 後續語義審閱（2026-10-19）

[逐題技術審閱](rg01-semantic-review-20261019.md)重新核對原 40 組 expected／Python／SQL 一致、14 個原 mutants 拒絕，同時確認 13 個原樣本漏收的事後診斷查詢，及最終和／中間累加的 int64 邊界待釐清。這是新增審閱發現，不回寫本包的凍結題庫／成績；不能據原五組通過宣告整類新資料正確。正式語義、覆蓋、數值域與責任核定仍 pending。

## 新版實作與離線結果

後續有界政策、13 個反例與嚴格 Linux launcher 已實作，見[本批交付](rg01-implementation-20261019.md)及[新收據摘要](evidence/rg01-tickets-offline-20261019.json)。1,098 項完整離線測試通過；本文件原有審閱／決策時點的狀態保留。Linux 目標攻擊取證與正式具名核定仍未完成，不回寫舊成績或把本機 pass 當正式資格。
