# RG-01 SQL Environment：公開驗收與工程中斷

狀態：**實作／離線驗證範圍，正式 RG-01 資格仍 pending**。`sql.py` 是獨立套件的 tool-free `Environment` 接入；`sql_executor.py` 只抽取舊工作區的無資料 parser／SQLite 執行 helpers，不依賴舊 `cloudyfan` 套件、Pydantic、實驗題庫、oracle 或隱藏答案。

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

`SqlContract` 將所有公開欄位複製為不可變 JSON 字串。`task()` 包含公開指示、schema、例子、期望結果與執行限制；`contract_version` 為 `sql-environment-v1:<sha256>`，綁定使用者契約版本、全部公開內容、executor 的限制版本、SQLite 版本與 native limits。資料、期望值或 runtime 描述改變，原 run 必須拒絕重用；更改 adapter 的驗收語義須升版，不能沿用舊 SQL 實驗契約。

`run_sql` 明確要求核心採用 `SQL_DIAGNOSTICS`（`sql-public-diagnostics-v1`、producer `sql-environment-v1`、4096 bytes），然後透過既有 `Core.run`／`BudgetedCore.run`，綁定 task、fresh environment factory 及版本。直接手動使用 `Core.run` 的可信應用仍須自己正確綁定這三者；此便利介面不防止管理者刻意繞過，也不授權付費。

`SqlEnvironment.tools_schema()` 為空；沒有工具往返，兩階段各最多一個生成請求。首答通過不分類；否則沿用低／中→必要時中／高，只有一次修正。`call()` 一律拒絕，不讓模型建立任意 SQL／檔案工具。一般 Core 的多 turn／工具能力與既有路由規則未改動。

## 公開契約與執行邊界

- 使用明確的受信任表／欄位定義；只接受既有 allowlist 的 INTEGER／TEXT 宣告。每個例子須提供全部表、完整欄位及公開期望列，不接受額外 private／hidden 欄位。
- 上限為 32 表／32 公開例子／每表每例 1000 筆輸入、整份公開設定 1 MiB；cell 僅限 int64、字串與 null。期望結果須符合欄數及現有 100 列／32768 cell bytes 上限。
- 保留 `sqlite-readonly-contract-v2` 的 SQL parser 與執行限制：僅 `{"sql": "..."}`、單一 SELECT／WITH、既有函式 allowlist、禁止寫入／extension／ATTACH／遞迴 CTE、全新記憶體資料庫、query_only／authorizer／setlimit／progress handler。
- SQL 20000 字元、回應／SQL UTF-8 32768 bytes、32 欄、expression depth 100、compound SELECT 20 terms、編譯 VM 25000 operations、函式參數 16、執行 VM 100000 步、每 500 步檢查、fixture 0.25 秒。精確值與 SQLite native limits 會出現在公開 task，不能為修正通過而調大。
- factory 在第一個模型 POST 前以每個 fixture 的獨立記憶體 DB 做 preflight，確認資料／DDL、SQLite build 與限制可用。它不會證明題目語義或 expected 的正確性，也不會執行模型答案。

結果比對欄名、允許型別、完整列及順序；NULL、重複值、空表、日期／排序語義仍須由任務契約定義。沒有公開例子時返回 `passed=None`，不是成功或模型答錯。

## 診斷投影與錯誤分類

Environment 僅返回 `passed` 與 flat string list `checks`；Core 再加入 diagnostic version／source。只發出固定 allowlist 的 code／compiler category，不回傳原始 SQLite exception、SQL 片段、樣本內容、巢狀 feedback 或 hidden 結果。相同 code 去重，32 個公開例子的診斷仍受完整 provenance 在內的 4096-byte 契約檢查。

| 分類 | 結果／後續 | 例子 |
|---|---|---|
| 公開答案不合格 | `passed=False`；沿用唯一修正，最終仍不合格為 failed。 | 壞 JSON／空答案、unsafe SQL、unknown column、欄名／型別／列不符、確定的 VM 步數或 compound／輸出界限。 |
| 無公開可驗條件 | `passed=None`；核心仍可走同一個有界補救流程，最終未知為 unverifiable。 | 空 samples；未知不當成通過。 |
| 工程／驗收環境異常 | 拋 `SqlValidationUnavailable`，category=`engineering`；當下停止，不分類、不發出新修正。 | 不相容 build、fixture 載入失敗、runtime 改變、I/O／記憶體／未知 SQLite 問題、驗收 wall timeout。 |

例如 `SQL_EXECUTION_ERROR:unknown_column`、`SQL_RESOURCE_LIMIT:compound_select_limit` 可讓模型對照公開 schema／上限修正，不需要錯誤訊息內的識別字。無法判定的 SQLite 錯誤保守歸工程中斷，不猜測為模型失敗；memory error 也可能來自編譯上限，未能可靠區分時同樣停止。

**工程分類是此新 adapter 的明確語義**：與舊函式把不相容環境回成 False 不同；wall timeout 也不以模型答錯計算。舊函式、題庫、答案和歷史分數未修改。既有核心／HTTP 的通用工程中斷與帳務處理介面未改動，本輪主驗收仍是 Core.run。

## 中斷、預算與證據

答案已由 Client 保存後才驗收。工程中斷不寫成功或錯誤答案結論；明確恢復會用原 run／contract 重讀原回應、重建記憶體 DB 並重驗，不重送已付費 POST。BudgetedCore 保留未完成流程的完整預留與已知／未知費用，只有流程完成才依既有規則釋放；API timeout 的未知費用亦不會被這個 adapter 清掉。

[回歸測試](../tests/test_sql_environment.py)以真 SQLite、手算公開期望與 MockTransport 驗證首答、兩層選擇／唯一修正、錯誤分類、資料變更拒絕、診斷上限、無重送及共享預算。實際完整離線結果、來源指紋與原始收據位置見[本輪證據](evidence/rg01-sql-offline.json)。沒有模型或供應商 API 呼叫。

## 正式仍待核定

公開有限例子可能被常數 SQL 通過；本接入不宣稱消除 false acceptance。仍需具名核定任務範圍、語義／expected 的獨立審閱、覆蓋／反例與模型資格。SQLite 在同程序內執行，合作式 progress／wall checks 不是 OS 隔離或硬 deadline；任意不受信任 SQL 的程序／記憶體隔離尚未提供。RG-01 及其他正式 gate 保持 pending，供應商費用保障依 RG-04 另補。
