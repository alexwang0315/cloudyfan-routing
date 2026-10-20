# 公開 SQL 驗收案例建置器 — 2026-10-20

`sql_public_suite.build_public_sql_contract` 是可選、事前建置的通用接入。可信應用提供公開輸入案例、獨立 Python 計算器、計算器與語料版本及需要覆蓋的資料特徵；建置器檢查後產生一般 `SqlContract`。沒有從自然語言自動推導正確答案，也沒有在路由期間調用 oracle、隱藏評分器或額外模型。

## 為何需要

新 50 題的 F32 兩個開源答案漏處理重複資料／不存在的主資料，F48 的 CloudyFan 答案在聚合前排除了沒有有效明細的主資料。原兩個公開案例仍通過，答案沒有進入選模／修正。這是有限案例未區分語意的問題，不能靠檢查有沒有 `DISTINCT` 或禁止 `INNER JOIN` 修正：`EXISTS`、分組、子查詢等不同 SQL 都可能正確。

原來 `SqlContract` 及路由語義保持不變；採用建置器的應用必須建立新版本、新公開契約及新 run，不能把新檢查套回舊分數。此處沒有重新審閱 S01～S08 或更改正式放行資格。

## 使用

```python
from cloudyfan_jev.sql_public_suite import build_public_sql_contract

def public_sum(data):
    return [[sum(row['value'] or 0 for row in data['numbers'])]]

contract = build_public_sql_contract(
    version='sum-v2', prompt='Sum non-NULL values; empty sum is zero.',
    tables={'numbers': [('value', 'INTEGER')]}, expected_columns=['total'],
    public_cases=[
        {'id': 'empty', 'tables': {'numbers': []}},
        {'id': 'mixed', 'tables': {'numbers': [
            {'value': None}, {'value': -2}, {'value': 5}, {'value': 5}]}},
    ],
    oracle=public_sum, oracle_version='independent-python-sum-v1',
    corpus_version='public-number-edges-v1', required_coverage=[
        {'kind': 'empty_table', 'table': 'numbers'},
        {'kind': 'null_value', 'table': 'numbers', 'column': 'value'},
        {'kind': 'duplicate_key', 'table': 'numbers', 'columns': ['value']},
    ],
)
# Pass contract to the existing run_sql(core, fresh_run_id, contract).
# Real API authority and qualified budgets are separate; this creates no API call.
```

全部案例輸入、計算出的 expected、來源版本、建置器程式雜湊及覆蓋證據都成為公開 prompt／凍結身分的一部分。應用若不願公開某筆資料或 expected，就不能透過此介面輸入。建置器沒有 hidden／private oracle 參數；這個形狀限制不能辨認管理者把私人資訊放入合法欄位，資料是否可公開仍由可信應用負責。

## 通用覆蓋契約

| kind | 宣告欄位 | 實際檢查 |
|---|---|---|
| `empty_table` | `table` | 至少一個公開案例的該表為空 |
| `null_value` | `table`, `column` | 至少一筆真實 NULL |
| `duplicate_key` | `table`, `columns` | 指定欄位組合至少重複一次 |
| `orphan_reference` | 子表 `table`／`column`、`parent_table`／`parent_column` | 存在非 NULL 的引用在主表沒有對應值 |
| `parent_without_children` | 同上 | 主表存在非 NULL 的鍵，在子表沒有對應值 |

規則必須非空、已知、完整、無重複且有實際 witness，不能只勾選「已覆蓋」。引用兩端須同型別，不以 Python 的比較假裝驗證 SQLite 跨型別 affinity。哪些軸對任務有意義、還需要哪些語意邊界，仍由應用依公開需求宣告；一個規則有 witness 不表示整個資料域足夠。

最多 32 個公開案例、32 條覆蓋規則，保留既有每表列數、總 bytes、欄位／結果與執行限制，超限拒絕而非截斷。資料先以既有 `SqlContract` 檢查型別／形狀，再驗證覆蓋；之後才調用可信 oracle。每個 oracle 對獨立輸入複本計算兩次，拒絕輸入改寫、不一致輸出、例外或不合格期望列。錯誤只發出固定 admission 訊息，不回傳原始例外或資料。

完成後只留下物化的公開資料；執行時沿用現有獨立 SQLite worker、資源界線、flat 公開診斷及保存／續跑邏輯。目前[順序式路由](sequential-routing.md)的 CloudyFan 最多三次生成、兩次二元分類，對照模式最多兩次生成。這不改變suite判分規則或歷史兩次生成結果。新覆蓋、語料／oracle 版本或建置器實作會改變契約身分，舊 run 不得靜默接續。未使用建置器的既有 `SqlContract` 不會自動獲得這些檢查。

oracle 是事前建置階段的可信 Python 程式，不在 SQL worker 的隔離內；不得採用模型提供的程式碼，應用須自行管理其 CPU／I/O 及來源。oracle 版本是應用聲明，物化資料雜湊與雙次計算不等於驗證了其語意或來源。

## 證據與限制

通用回歸使用與實驗無關的 accounts/access/payments 合成資料及 Python 集合／聚合計算，檢查重複、孤兒引用、空表、NULL、無明細主資料、不同正確 SQL 寫法、固定修正次數、續跑不重送與來源改變拒絕。另保留反例：錯誤但確定性的應用 oracle 仍可能通過；重複計算不是獨立語意核定，也不證明一般 SQL 等價。

事後離線分析只讀原始已公開 intent 的契約與保存答案，不載入原實驗 hidden fixtures、reference SQL 或隱藏期望。新的共用公開語料與 Python 計算在重播前保存。八筆答案重播中，三筆已知漏判被拒絕，其餘五筆仍通過；這是針對已知錯誤的回歸證據，不是盲測、母體誤收率或實測節費改善。

原 50 題成績仍為 CloudyFan 48/50、全大模型 50/50、便宜先答 50/50、開源先答 49/50，API US$5.805611568 不變。沒有新增付費測試、核帳 API、部署或推送。新增公開案例會增加 context、驗收耗時及潛在模型費，尚無真實量測；不能從離線重播推定新正確率、CDR 或商業獲利。

實際測試及原始檔完整性見[本輪離線證據](evidence/sql-public-suite-offline-20261020.json)；正式 gate 仍待核定。

## 本機提交審查

[提交前證據](evidence/sql-public-suite-precommit-20261020.json)另外驗證獨立暫存版本：1,166 項測試、Ruff、模擬示範及套件建置通過。先前 1,182 項屬於含另輪尚未提交費用接入的較大工作區，包含其額外 16 項測試；原收據與資料完整保留，不將這個數字歸給較小的提交版本。上輪費用接入及 50 題報告仍留在工作區，未混入本次提交。
