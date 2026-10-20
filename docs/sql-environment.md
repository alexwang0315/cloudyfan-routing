# RG-01 SQL Environment v2：公開驗收、子程序與工程中斷

狀態：**實作／離線驗證範圍，正式 RG-01 資格仍 pending**。目前 v2 使用獨立標準庫子程序執行每個 fixture；v1 的同程序實測保留為歷史。`sql.py` 是獨立套件的 tool-free `Environment` 接入；`sql_executor.py` 只抽取舊工作區的無資料 parser／SQLite 執行 helpers，不依賴舊 `cloudyfan` 套件、Pydantic、實驗題庫、oracle 或隱藏答案。

## 使用與凍結

```python
from cloudyfan_jev.sql import SQL_DIAGNOSTICS, SqlContract, run_sql

contract = SqlContract(
    version="sum-public-v1",
    prompt="Sum all numbers and return column total.",
    tables={"numbers": [("n", "INTEGER")]},
    expected_columns=["total"],
    samples=[{"id": "example-1", "tables": {"numbers": [{"n": 1}, {"n": 2}]},
              "expected": [[3]]}],
)
# Construct the application's Core with diagnostic_contract=SQL_DIAGNOSTICS.
# Tests inject httpx.MockTransport. Real execution additionally requires the
# existing BudgetedCore authorization and qualified complete bounds.
result = run_sql(controlled_core, "sql-job-001", contract)
```

`samples` 的輸入和 `expected` 都必須是**可公開的契約例子**。此介面沒有 hidden fixtures、private oracle、外部 DB 路徑或任意驗收函式的參數。不要把私人答案當 expected 傳入，因為公開契約會放進模型 prompt。

新的可選[公開案例建置器](sql-public-suite.md)在建立 `SqlContract` **之前**，由可信應用的獨立計算器物化公開 expected、檢查宣告覆蓋並綁定來源版本；執行時不保留 callable，也不改變此 Environment 或既有契約的語義。僅使用原介面的應用不會自動增加覆蓋。

`SqlContract` 先拒絕欄位型別不符、NOT NULL 的空值及 INTEGER PRIMARY KEY 的 NULL 自動編號，避免 SQLite affinity 改寫公開資料。它將所有公開欄位複製為不可變 JSON 字串。`task()` 包含公開指示、schema、例子、期望結果與執行限制；`contract_version` 為 `sql-environment-v2:<sha256>`，綁定使用者契約版本、全部公開內容、executor 的限制版本、SQLite 版本與 native limits、worker 程式雜湊、Python／平台及程序資源策略。資料、期望值或 runtime 描述改變，原 run 必須拒絕重用；更改 adapter 的驗收語義須升版，不能沿用舊 SQL 實驗契約。

`run_sql` 明確要求核心採用 `SQL_DIAGNOSTICS`（`sql-public-diagnostics-v2`、producer `sql-environment-v2`、4096 bytes），然後透過既有 `Core.run`／`BudgetedCore.run`，綁定 task、fresh environment factory 及版本。直接手動使用 `Core.run` 的可信應用仍須自己正確綁定這三者；此便利介面不防止管理者刻意繞過，也不授權付費。

`SqlEnvironment.tools_schema()` 為空；沒有工具往返，每個作答階段最多一個生成請求。首答通過不分類；目前 CloudyFan 採[順序式路由](sequential-routing.md)：低／中選模後先作答驗收，仍未通過才中／高選模最後作答，最多三次生成。對照模式仍最多兩次；這是執行器版本變更，SQL驗收規則不變。`call()` 一律拒絕，不讓模型建立任意 SQL／檔案工具。

## 公開契約與執行邊界

- 使用明確的受信任表／欄位定義；只接受既有 allowlist 的 INTEGER／TEXT 宣告。每個例子須提供全部表、完整欄位及公開期望列，不接受額外 private／hidden 欄位。
- 上限為 32 表／32 公開例子／每表每例 1000 筆輸入、整份公開設定 1 MiB；cell 僅限 int64、字串與 null。期望結果須符合欄數及現有 100 列／32768 cell bytes 上限。
- 保留 `sqlite-readonly-contract-v2` 的 SQL parser 與執行限制：僅 `{"sql": "..."}`、單一 SELECT／WITH、既有函式 allowlist、禁止寫入／extension／ATTACH／遞迴 CTE、全新記憶體資料庫、query_only／authorizer／setlimit／progress handler。
- SQL 20000 字元、回應／SQL UTF-8 32768 bytes、32 欄、expression depth 100、compound SELECT 20 terms、編譯 VM 25000 operations、函式參數 16、執行 VM 100000 步、每 500 步檢查、fixture 合作式 0.25 秒。精確值與 SQLite native limits 會出現在公開 task，不能為修正通過而調大。
- factory 在第一個模型 POST 前以每個 fixture 的獨立記憶體 DB 做 preflight，確認資料／DDL、SQLite build 與限制可用。它不會證明題目語義或 expected 的正確性，也不會執行模型答案。

結果比對欄名、允許型別、完整列及順序；NULL、重複值、空表、日期／排序語義仍須由任務契約定義。沒有公開例子時返回 `passed=None`，不是成功或模型答錯。

## 診斷投影與錯誤分類

Environment 僅返回 `passed` 與 flat string list `checks`；Core 再加入 diagnostic version／source。只發出固定 allowlist 的 code／compiler category，不回傳原始 SQLite exception、SQL 片段、樣本內容、巢狀 feedback 或 hidden 結果。相同 code 去重，32 個公開例子的診斷仍受完整 provenance 在內的 4096-byte 契約檢查。

| 分類 | 結果／後續 | 例子 |
|---|---|---|
| 公開答案不合格 | `passed=False`；依凍結模式作有界修正，最終仍不合格為 failed。 | 壞 JSON／空答案、unsafe SQL、unknown column、欄名／型別／列不符、確定的 VM 步數或 compound／輸出界限。 |
| 無公開可驗條件 | `passed=None`；核心仍可走同一個有界補救流程，最終未知為 unverifiable。 | 空 samples；未知不當成通過。 |
| 工程／驗收環境異常 | 拋 `SqlValidationUnavailable`，category=`engineering`；當下停止，不分類、不發出新修正。 | 不相容 build、fixture 載入失敗、runtime 改變、I/O／記憶體／未知 SQLite 問題、驗收 wall timeout。 |

例如 `SQL_EXECUTION_ERROR:unknown_column`、`SQL_RESOURCE_LIMIT:compound_select_limit` 可讓模型對照公開 schema／上限修正，不需要錯誤訊息內的識別字。無法判定的 SQLite 錯誤保守歸工程中斷，不猜測為模型失敗；memory error 也可能來自編譯上限，未能可靠區分時同樣停止。

**工程分類是此新 adapter 的明確語義**：與舊函式把不相容環境回成 False 不同；wall timeout 也不以模型答錯計算。舊函式、題庫、答案和歷史分數未修改。既有核心／HTTP 的通用工程中斷與帳務處理介面未改動，本輪主驗收仍是 Core.run。

## 中斷、預算與證據

答案已由 Client 保存後才驗收。工程中斷不寫成功或錯誤答案結論；明確恢復會用原 run／contract 重讀原回應、重建記憶體 DB 並重驗，不重送已付費 POST。BudgetedCore 保留未完成流程的完整預留與已知／未知費用，只有流程完成才依既有規則釋放；API timeout 的未知費用亦不會被這個 adapter 清掉。

[回歸測試](../tests/test_sql_environment.py)以真 SQLite、手算公開期望與 MockTransport 驗證首答、有界選模／修正、錯誤分類、資料變更拒絕、診斷上限、無重送及共享預算。歷史SQL v2離線結果見[v2 證據](evidence/rg01-review-ready-offline.json)；順序式路由另以[專用回歸](../tests/test_sequential_routing.py)檢查，不把舊證據稱為新流程已驗證。沒有模型或供應商 API 呼叫。

## 子程序與資源策略

每個 fixture 使用新 PID、`-I -S -B`、不繼承父環境與 FD、有界 stdin／stdout；
父程序對 pipe I/O 與退出設 3 秒 deadline，所有錯誤／取消路徑 kill/reap。
子程序要求 CPU 2 秒、core dump／regular-file bytes 0、open files 32、SQLite
hard_heap_limit 32 MiB。這不限制 Python 的全部 RAM，也不是 OS security sandbox。
`require_address_space=True` 另要求 256 MiB RLIMIT_AS；不支持則 preflight 拒絕，
沒有回退。這項策略及 worker 的程式雜湊都加入凍結契約。完整邊界、實機限制與
審閱清單見[RG-01 正式審閱包](rg01-review-ready.md)。v1 receipt 不作 v2 資格證據。

## 正式仍待核定

公開有限例子可能被特製 SQL 通過；本接入不宣稱消除 false acceptance。
新[審閱材料](rg01-review-ready.md)提供八類任務、獨立 Python expected、手算與
反例；仍需具名核定任務範圍、語義、覆蓋與適用隔離。本機 macOS 拒絕 AS／DATA
額度設定；嚴格地址空間模式會在 POST 前停止，目標 OS 的完整記憶體／安全 sandbox
資格未核定。RG-01 及其他正式 gate 保持 pending；模型、供應商與品質主張分別沿
RG-02、RG-04、RG-07 補證，不用 mock 通過代替。
