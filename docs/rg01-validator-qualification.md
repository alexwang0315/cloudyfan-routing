# RG-01 驗收器資格準備包

日期：2026-10-19；狀態：**prepared／正式資格 pending**。依[首版範圍草案](release-scope-draft.md)整理既有資料，不新增模型答案、題庫、路由門檻或驗收語義。這不是可由執行器載入的正式資格證書。

## 來源與可重用範圍

| 證據 | 支持什麼 | 不支持什麼 |
|---|---|---|
| `src/cloudyfan_jev/engine.py`、`diagnostics.py` 及核心測試 | `Core.run` 的嚴格 True／False／None、公開診斷、唯一修正、設定變更拒絕與已付費答案重用。 | 注入驗收器判斷語義一定正確、可任意執行 SQL。 |
| [診斷契約](public-diagnostics.md)、[跨接入矩陣](transport-contracts.md) | 核心／接入的離線契約；未知不是成功。 | 真實模型品質、客戶資料／部署安全。 |
| 外層 `src/cloudyfan/workflow_sql.py`、`tests/test_workflow_sql.py`、`tests/test_sql_execution_contract.py` | 有限合成 SQL、獨立 Python oracle、嚴格解析、資源約束、正反例與公開／隱藏分離的候選材料。 | 已抽入獨立核心、一般 SQL 正確性、程序層硬資源隔離。 |
| 外層 final50 離線契約結論、最新 hierarchical25 契約審查 | 先前存在輸出域、欄位含義與投影歧義；提醒審查清單必需。 | 用事後合理解讀補成事前無歧義，或直接重用舊成功率核定新 adapter。 |

外層來源以 workspace-relative path／SHA256 登記於[準備證據](evidence/release-evidence-preparation-20261019.json)，不複製私人 prompt、答案、原始收據或隱藏 fixtures。歷史最新 25 題是工具任務，與本次 SQL 候選不同；H01／H09／H33 三題契約未知、22 題是後驗非盲分析的限制保留於[歷史摘要](evidence.md)。

## 已有 SQL 契約與限制

歷史版本是 `business-sql-v2`／`business-sql-fixtures-v1`／`independent-python-v1`／`sqlite-readonly-contract-v2`。parser 接受一個僅含 `sql` 字串的 JSON object；重複 key、額外欄位、非有限數值、壞 Unicode 與過大內容拒絕。SQL 必須 SELECT／WITH 起頭、單一唯讀 statement；不得讀系統／未宣告表、載入 extension、寫入、ATTACH 或遞迴 CTE。函式使用明確 allowlist。

既有上限：SQL 20,000 字元、回應／SQL UTF-8 32,768 bytes、輸出 100 列／32,768 cell bytes、32 欄、expression depth 100、compound SELECT 20 terms、編譯 VM operations 25,000、函式參數 16、VM 步數 100,000、每 500 步檢查、fixture 0.25 秒、公開 feedback 4,096 bytes。SQLite 其他 native limits 依實際 build 讀取，不假定跨環境一致。

結果以**精確欄名、列型別、列內容與順序**比對；NULL／重複值／空表／日期邊界與排序均須由各任務契約決定，不能從 SQL 看起來合理或模型信心判通過。公開 fixtures 的 oracle 結果可生成公開診斷；隱藏結果只能留在最後獨立評分，不能進選模或修正。

SQLite 每個 fixture 使用新的 `:memory:` connection，關閉 extension，並啟用 query_only、authorizer、setlimit 與 progress handler。**這是同程序內的有限測試 harness，不是 OS sandbox**。0.25 秒是合作式檢查，可能在 callbacks 之間超過，沒有證明所有 prepare／函式／記憶體分配可被硬中止；正式非受信任 SQL 仍缺程序／記憶體／deadline 隔離證據。

## 可追溯驗證矩陣

這些是既有案例的重跑與檢查，沒有新增付費樣本；實際數量／收據以準備證據為準。

| ID | 覆蓋內容 | 既有測試／本輪檢查 | 正式狀態 |
|---|---|---|---|
| V01 | 正確 query 對獨立 oracle／手算公開例 | `test_reference_query_matches_independent_python_oracle`、`test_public_oracle_has_hand_calculated_examples` | 有限合成案例；領域審閱待核定。 |
| V02 | 日期、tie-break、聚合／重複語義反例 | `test_wrong_boundary_tie_and_aggregation_fail` | 既有反例已可測；不是完整語義覆蓋證明。 |
| V03 | 只背公開答案的 false acceptance | `test_constant_sample_answer_does_not_pass_hidden_fixtures` | 已知限制：公開可過、隱藏會失敗；不能回饋隱藏結果繼續修正。 |
| V04 | 寫入、外部表、extension、遞迴與多 statement 拒絕 | `test_unsafe_queries_are_rejected_without_mutating_public_fixture`、`test_resource_diagnostics_do_not_weaken_sql_authorization` | 本機合成拒絕證據；非一般安全認證。 |
| V05 | parser／欄名／型別／輸出與資源界限 | `test_response_json_schema_is_strict_and_errors_do_not_echo_input`、`test_float_results_and_wrong_column_names_are_rejected`、`test_vm_deadline_and_output_limits_disclose_the_bound` | 既有約束；硬程序 deadline／memory 未證。 |
| V06 | 不支援 SQLite build 不執行 query | `test_incompatible_sqlite_build_is_an_environment_error_and_does_not_execute` | 原函式回傳 `passed=False`；正式接入必須另映射工程／無法驗收，不能算模型答錯。 |
| V07 | 隱藏結果不改公開 feedback | `test_hidden_resource_errors_do_not_change_public_feedback_or_disclose_fixture_content` | 合成 sentinel 檢查；不能偵測允許字串內所有私人資訊。 |
| V08 | 核心嚴格三態及唯一修正 | `tests/test_engine.py`、`tests/test_diagnostics.py` | 已完成核心契約，不等於 SQL 接入已完成。 |
| V09 | 原始 SQL 結果不是可直接注入診斷 | 本輪離線 probe：default DiagnosticContract 拒絕 SQL 結果；拒絕 nested feedback／hidden 欄位 | 正式 SQL Environment／projection 接入未實作，維持 pending。 |
| V10 | 題目歧義不能靠 oracle 猜 | 既有 final50／hierarchical25 審查，只保留來源與問題類型 | 命名欄位、輸出域、空值／缺資料、投影、排序／tie-break 的具名語義審閱待辦。 |

## 正式接入前的判定／診斷設計待辦

以下是待審要求，**不是本輪已交付的新 adapter**：

1. 每個任務固定公開 schema、資料 hash、結果欄位／型別／順序、輸出域及精確契約版本；核對 oracle 與 prompt 一致。無法唯一決定語義時保留 unknown，不私改原題。
2. 將「依已核定契約確實不符」映射 False；已核定公開檢查通過才映射 True。缺資料、歧義或驗收不可用映射 None 或明確工程中斷；SQLite build 不支援、validator crash 不得以 False 計模型失敗。
3. 明確抽取**公開**結果，不能把整份 SQL return 傳給核心：它含 `hidden_passed`、巢狀 feedback 等不相容欄位。採核定 schema 的 scalar／flat-list codes 與有來源的公開文字；完整來源 metadata 加入後仍須符合 4,096-byte 上限。公開 SQL feedback 4,096 bytes 不等於核心加 provenance 後也能放下。
4. 隱藏結果和跨組答案不進 env.validate 的公開 projection；oracle 只是可信驗收器的一部分，不能在允許 prose 中夾帶隱藏答案。結束後的離線 grading 與選模／修正隔離。
5. 唯一修正沿用同公開契約與診斷；無第三次生成。保存答案後驗收中斷，恢復應重用原回應、不重送；契約／工具／資料／隔離方式改變，更新版本並拒絕舊 run 重用。
6. 分開記錄模型不合格、工程中斷、無法驗收及費用未知。有限公開 fixtures 存在已示範的 false acceptance；正式允許的任務集合、獨立 oracle 審閱及品質主張須具名決定，不能以本次測試 pass 取代。

## 正式核定缺口

owner／reviewer 未指定；首版任務集合及語義未核定；SQL Environment 接入／投影與工程錯誤分類未提供；OS 層隔離／硬資源上限未證；真實模型設定未核定。本包整理完成及所有離線檢查通過，也仍保持 **RG-01 pending**。補證優先順序為：先範圍／語義與責任，再明確接入設計，然後另行授權實作和相應離線／真實證據；本輪不擴大為 adapter 實作。
