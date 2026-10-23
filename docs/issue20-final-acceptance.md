# Issue #20 本機接入驗收索引

2026-10-22：**核定現行交付範圍的必要檢查已通過**：Monitor 89 passed、核心隔離整合 1,921 passed、桌機 20 項通過。審查候選已合入 `e5fbd6e7cb06e0512d78f56d74c17e1012bc68c2` 並同步原 Monitor；53 份交付檔案一致，13 份產品指紋保持不變。父規格及 [#23](https://github.com/alexwang0315/cloudyfan-routing/issues/23) 的工作單已完成結案；此處保留 Issue20 當時的驗收範圍，最新工具驗證依 `python3 scripts/acceptance.py status` 查核。原工作區的 71 失敗／2,483 通過另行保留；沒有將它改列全綠、刪除共享舊副本、改檢查器或放寬 SQL 限制。

本文件對應 [#20](https://github.com/alexwang0315/cloudyfan-routing/issues/20)、[身分驗收 #21](https://github.com/alexwang0315/cloudyfan-routing/issues/21) 與 [四模式驗收 #22](https://github.com/alexwang0315/cloudyfan-routing/issues/22)。兩張前置工作單已結案，其本機提交均已合入整合分支。機讀結果與每份安全證據的 SHA256 見 [最終機讀索引](evidence/monitor-issue20-final-20261022.json)。本索引記錄的「最終」指本輪彙整版本；完成判定限於使用者核定的交付範圍，不表示原工作區所有歷史快照或正式服務均通過。審查兩軸與修正見 [審查處理紀錄](issue20-code-review.md)。

## 已實際驗證的範圍

| 範圍 | 本輪結果及證據 |
| --- | --- |
| Monitor 完整套件 | **89 passed**；Ruff、產品 JavaScript、三個桌機 QA 入口及共用 fixture 工具語法、離線 sdist／wheel 通過。[檢查紀錄](../runtime/checks/209c861141b0/checks.json) 另保存 Monitor 產品、測試、QA 及建置輸入指紋，以及實際注入的核心 Python 來源指紋；兩組在檢查前後一致。 |
| 真實本機 HTTP | 20 個 socket 案例、182 筆 HTTP 回應：原有 7 個並行／斷線／到期／中斷情境，加上 #21 的 4 個身分情境、#22 的 8 個模式／固定契約情境及 1 個完成任務併讀情境。每案保留真正的 loopback 埠、回應、上游 MockTransport 呼叫及被阻擋 socket 紀錄，詳見機讀索引。 |
| 原有桌機流程重跑 | [8 項通過](../runtime/integration-desktop/705aff87-35a/desktop-report.json)：忙碌與到期歷史、全域模式、個別覆寫、23 × 4 契約矩陣與候選缺額、部分來源更新及發布、主來源故障、任務／費用歷史、已接受任務完成。[HTTP／mock 收據](../runtime/integration-desktop/705aff87-35a/http-receipt.json) 觀測到 6 次合成模型呼叫及 6 次模擬來源 GET。 |
| 客戶端身分桌機重跑 | [4 項通過](../runtime/identity-desktop/50778322aa45/report.json)：客戶端不能登入操作員控制台，換憑證後原身分、任務及費用保持一致。合成模型呼叫維持 2 次。[詳細說明](identity-http-acceptance.md)。 |
| 四模式桌機重跑 | [8 項通過](../runtime/mode-desktop/fec34692-9c6/desktop-report.json)：實際切換四模式、驗證首個模型、個別覆寫／繼承、契約矩陣、合成池切換與完整應用重建。7 件任務共 7 次合成生成、共用合成帳本 0.07 美元。[HTTP 收據](../runtime/mode-desktop/fec34692-9c6/http-receipt.json) 與 [詳細說明](mode-http-acceptance.md)。 |
| 指定核心模組舊證據 | 六個受影響模組 **119 passed**，兩個模擬示範及建置通過。保留 [原檢查副本](../runtime/final-acceptance/historical-119-checks.json) 與 [精確範圍](../runtime/final-acceptance/historical-119-scope.json)；這是重開機後的歷史證據，不稱為本輪完整核心通過。 |

三組桌機共 20 項，皆為實際 Chrome、1280 × 800；沒有新增手機介面或手機測試。畫面明確顯示模擬上游。原有流程故意注入主來源 503，因此保留 1 筆預期的 console resource error；三組 JavaScript page error 皆為 0。各次服務使用新資料夾與隨機 loopback 埠，測試結束已正常停止。

整合者於 15:08（Asia/Taipei）另對原 18767 預覽完成 [即時 Chrome 檢查](../runtime/final-preview-smoke/0bee0d3e/report.json)：12 件合成任務、12 通過、0 未知，讀取前後公開統計一致；瀏覽器只送出 GET，沒有 page error 或站外請求。該預覽的產品來源指紋與本輪 Monitor gate 一致；整合者已完成最後同步並核對 13 份產品指紋不變，原 18767 再次實際 GET 回應 200；見 [同步與產品核對](../runtime/final-acceptance/final-integration/product-sync-verification.json)、[檔案同步收據](../runtime/final-acceptance/final-integration/initial-sync-receipt.json) 及 [桌機畫面複核](../runtime/final-acceptance/final-integration/desktop-review.json)。

20 個 HTTP 案例的父程序合計觀測到 26 次模擬呼叫；被終止 worker 另留下 1 次已進入上游的觀測。重建後父程序的 0 次新增呼叫沒有被拿來取代該 worker 的實際呼叫。身分安全投影與模式觀測各自連回同案 socket 收據，不以初始 demo bundle 的計數代表真正 CategoryCore 流量。

## 核准的現行核心完整驗收

使用者已核准 [單一合成 fixture 修正](evidence/core-project-state-fixture.patch)，並以隔離的全部 1,921 個現行測試作為交付驗收範圍；17 份歷史測試快照、633 項測試保留在共享原工作區，另列範圍，不刪除、不套用全域忽略規則。[核准紀錄](../runtime/final-acceptance/approved-canonical-scope.json) 與 [安全摘要](evidence/core-canonical-acceptance-20261022.json) 可回讀。

隔離樹使用原命令 `./scripts/check --integration`，沒有 pytest 選取條件或 `PYTEST_ADDOPTS`。實際結果為 **1,921 passed in 534.19s**，offline locked sync、依賴相容、records、Ruff、兩個各 5 次模擬請求的示範及離線 sdist／wheel 均通過。見 [官方紀錄](../runtime/final-acceptance/core-canonical/canonical-core-gate-20261022T153238/official-checks.json)、[完整日誌](../runtime/final-acceptance/core-canonical/canonical-core-gate-20261022T153238/required-integration.log) 與 [包裝器收據](../runtime/final-acceptance/core-canonical/canonical-core-gate-20261022T153238/receipt.json)。

438 份執行／metadata 輸入前後一致，變更清單為空；來源摘要為 `09311fb4173eadf7f1366f9396f9ad21b770e618e6f4691e14bf65ae29305f98`。精確版本由私有 [凍結輸入 archive](../runtime/final-acceptance/core-canonical/canonical-inputs-20261022T073139Z/inputs.tar.gz) 的 SHA256 `6afb337d58dd6eddf6e65e00efba589042361024a2d26c45c1955ca049bee1a2` 與 [manifest](../runtime/final-acceptance/core-canonical/canonical-inputs-20261022T073139Z/manifest.json) 指定；歷史 base commit 不是這份 dirty overlay 的已驗證 commit。原 17 份快照的雜湊另已核對保持不變。

fixture 修正讓歷史完成工作維持完成，避免測試把所有歷史 ownership 同時重新啟用；當前 phase 仍維持進行中，沒有把舊通過紀錄當成目前驗收。修正後該模組 31 項通過，未改產品派送、檢查器、付費權限或研究內容。這個修正由核心調查者套用，本次 Monitor 提交保存完全相同的可審閱 patch 與安全摘要。

## 保留的原工作區失敗

不加測試篩選的原命令 `./scripts/check --integration`，於 2026-10-22 14:33:39 至 14:52:47（Asia/Taipei）執行。離線 locked sync、依賴相容、project records 與 Ruff 通過；pytest **71 failed, 2483 passed in 1144.99s**，退出碼 1，後續示範與建置未執行。保留 [完整原始日誌](../runtime/final-acceptance/core-full.log)、[官方檢查紀錄](../runtime/final-acceptance/core-full-checks.json) 與 [包裝器收據](../runtime/final-acceptance/core-full-receipt.json)。

- 70 項失敗位於保留的 `* 2.py` 舊快照；原 17 份、633 個測試保持不變，後續依使用者核准另列交付範圍。
- 1 項失敗位於目前的 `test_project_state.py::test_historical_pass_and_closed_work_never_imply_current_or_production_completion`。其合成 fixture 之後依人類明確核准修正，原失敗紀錄沒有改寫。
- 核心可執行來源摘要前後一致；但 `docs/agent-state.json` 在這次約 19 分鐘檢查中有變動。相同來源摘要不代表 metadata 一致，不能把這次結果說成穩定完整狀態的全綠驗收，也不據此歸責其他工作者。

較早的檢查先停在 RG-04 next step 與既有 records checker 的授權前提欄位不一致。使用者明確允許後，整合者只將 `requires_authorization: false` 改為 `true` 再重跑；[核准與修正收據](../runtime/final-acceptance/approved-records-repair.json) 保留原值、雜湊前提與實際變更。此欄位修正沒有給予付費模型、研究派送、預算或正式資格的新授權。

重開機前提到的三個目前版本 SQL 案例，在同一 locked 環境逐一重跑都通過：bounded review 39.74 秒、compound ORDER 1.27 秒、low research 292.57 秒。[收據](../runtime/final-acceptance/core-narrow-receipt.json) 與原始 [bounded](../runtime/final-acceptance/core-narrow-bounded.log)、[compound](../runtime/final-acceptance/core-narrow-compound.log)、[low research](../runtime/final-acceptance/core-narrow-low-research.log) 可查核。原 [一般檢查](../runtime/final-acceptance/historical-pre-reboot-required-integration.log) 與 [另一輪檢查](../runtime/final-acceptance/historical-pre-reboot-canonical-integration.log) 缺少最後 traceback，歷史失敗根因仍未知；現在單獨通過不算修復，也不能只由靜態呼叫鏈宣告與 Monitor 無關。

上述後續隔離驗收已在核准範圍取得通過，命令、438 份穩定輸入、17 份另列快照與原 71 失敗結果分開保存；它沒有覆蓋或改寫原命令的失敗。

## 審查後的 Monitor 重跑與併讀診斷

共用 JSON-line fixture 工具後，原有桌機與四模式桌機各重跑 8 項通過。之後對測試模組增加安全錯誤碼觀測時，只改動未被桌機 host 使用的測試／LocalService 定義；桌機實際匯入的 socket、handler 與 guard fixture 定義逐位元組一致。[輸入重用證明](../runtime/result-read-diagnosis/desktop-input-reuse-proof.json) 保留原整檔 SHA 與新整檔 SHA 的差異，不修改舊報告來偽裝同版。身分桌機另外重跑 4 項。

審查後第一輪完整 Monitor 檢查曾 **87 passed／1 failed**：[失敗紀錄](../runtime/checks/5d28c8b0f924/checks.json) 保留斷線任務狀態已通過、結果卻收到 409 的觀測，原錯誤本文沒有保存，其確切錯誤碼與歷史根因仍未知。新的 [兩案例真實 HTTP 診斷](../runtime/result-read-diagnosis/pytest.log) 使用真正 per-task guard 模擬併讀，實際觀測到 `passed` 狀態、明確 `task_busy` 回應及釋放後正常結果；同內容重送和讀取始終只有 1 次模型呼叫。這證明既有非阻塞契約下的機制，不回推原失敗必定有相同錯誤碼。

斷線測試改為最多 5 秒、只對明確 `409/task_busy` 的結果讀取作有限等候；其他 409 立刻失敗，模型呼叫與只讀重送斷言不放寬。新增的併讀行為案例及 [受影響 HTTP 模組 8 項](../runtime/result-read-affected/pytest.log) 先通過，最後完整 Monitor gate 為 89 passed；沒有為此更改產品或核心程式。原 87／1、診斷與最新完整結果均各自保留。

## 沿用唯一一次真實來源窗口

本輪只讀查核既有 [來源報告](../runtime/final-acceptance/source-get/report.json) 及 [實際嘗試紀錄](../runtime/final-acceptance/source-get/observed-requests.json)，沒有重跑外部 GET。15 份原始公開 metadata 的大小及 SHA256 與實際請求紀錄一致：3 個必要 metadata 加 12 個端點，共 15 次嘗試皆 HTTP／schema 成功，30.62158 秒。**24 個端點未查，所以仍是部分更新**；23 類、143 個具名候選位置與 `exploratory_unvalidated` 政策不等於模型執行資格。這一次真實候選沒有發布或啟用。

桌機故障回歸的來源呼叫全部經 MockTransport；其中合成候選的發布僅影響隔離 fixture，沒有發布上述真實候選或更動執行中的模型池。

## 重跑與證據保存

在 Monitor 套件根目錄，沿用已安裝的核心環境、Playwright 與 Chrome：

```sh
export CLOUDYFAN_CORE_ROOT=/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core
export PYTHONPATH="$PWD/src:$CLOUDYFAN_CORE_ROOT/src"
"$CLOUDYFAN_CORE_ROOT/.venv/bin/python" scripts/check
node scripts/qa_integration_desktop.cjs
"$CLOUDYFAN_CORE_ROOT/.venv/bin/python" scripts/qa_identity_http.py
node scripts/qa_mode_desktop.cjs
```

每個入口建立新的 ignored `runtime/` 證據目錄。核心完整 gate 的重跑另依核心工作區規範與當次驗收決定執行，不是由上述桌機命令暗中啟動。未修改真實服務帳本、未取得實付模型能力或降本證據，未新增來源 GET、手機測試、資料刪除、推送或部署。本輪 implement-spec 僅允許隔離分支的本機提交；私人 runtime、截圖、合成收據與診斷日誌不加入提交。

機讀索引只列安全投影、檢查日誌、公開 metadata 及其雜湊，不收錄 fixture 的資料庫、憑證、原始提示或答案。來源檔案與歷史失敗保留不變；文件連結、證據雜湊與秘密樣式掃描的實際結果另在同一索引列出。這些查核只驗證證據完整性；功能驗收仍以上述實際檢查與核定範圍為準。
