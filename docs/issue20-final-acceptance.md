# Issue #20 本機接入驗收索引

2026-10-22：Monitor 與桌機補強已取得本輪證據；**父規格及 [#23](https://github.com/alexwang0315/cloudyfan-routing/issues/23) 仍未完成**。必要的完整核心整合檢查回報 71 失敗、2,483 通過，不能以 Monitor 通過或較小範圍的核心通過取代。沒有為了得到全綠而刪除舊副本、改變檢查器或放寬 SQL 限制。

本文件對應 [#20](https://github.com/alexwang0315/cloudyfan-routing/issues/20)、[身分驗收 #21](https://github.com/alexwang0315/cloudyfan-routing/issues/21) 與 [四模式驗收 #22](https://github.com/alexwang0315/cloudyfan-routing/issues/22)。兩張前置工作單已結案，其本機提交均已合入整合分支。機讀結果與每份安全證據的 SHA256 見 [最終機讀索引](evidence/monitor-issue20-final-20261022.json)。本索引記錄的「最終」指本輪彙整版本，不代表必要門檻已通過。

## 已實際驗證的範圍

| 範圍 | 本輪結果及證據 |
| --- | --- |
| Monitor 完整套件 | **88 passed**；Ruff、產品 JavaScript 與三個桌機 QA 腳本語法、離線 sdist／wheel 通過。[檢查紀錄](../runtime/checks/782d2e51cec4/checks.json) 另保存 Monitor 產品、測試、QA 及建置輸入指紋，以及實際注入的核心 Python 來源指紋；兩組在檢查前後一致。 |
| 真實本機 HTTP | 19 個 socket 案例、177 筆 HTTP 回應：原有 7 個並行／斷線／到期／中斷情境，加上 #21 的 4 個身分情境、#22 的 8 個模式／固定契約情境。每案保留真正的 loopback 埠、回應、上游 MockTransport 呼叫及被阻擋 socket 紀錄，詳見機讀索引。 |
| 原有桌機流程重跑 | [8 項通過](../runtime/integration-desktop/75d86630-a3d/desktop-report.json)：忙碌與到期歷史、全域模式、個別覆寫、23 × 4 契約矩陣與候選缺額、部分來源更新及發布、主來源故障、任務／費用歷史、已接受任務完成。[HTTP／mock 收據](../runtime/integration-desktop/75d86630-a3d/http-receipt.json) 觀測到 6 次合成模型呼叫及 6 次模擬來源 GET。 |
| 客戶端身分桌機重跑 | [4 項通過](../runtime/identity-desktop/48010ccc8056/report.json)：客戶端不能登入操作員控制台，換憑證後原身分、任務及費用保持一致。合成模型呼叫維持 2 次。[詳細說明](identity-http-acceptance.md)。 |
| 四模式桌機重跑 | [8 項通過](../runtime/mode-desktop/92dae824-3be/desktop-report.json)：實際切換四模式、驗證首個模型、個別覆寫／繼承、契約矩陣、合成池切換與完整應用重建。7 件任務共 7 次合成生成、共用合成帳本 0.07 美元。[HTTP 收據](../runtime/mode-desktop/92dae824-3be/http-receipt.json) 與 [詳細說明](mode-http-acceptance.md)。 |
| 指定核心模組舊證據 | 六個受影響模組 **119 passed**，兩個模擬示範及建置通過。保留 [原檢查副本](../runtime/final-acceptance/historical-119-checks.json) 與 [精確範圍](../runtime/final-acceptance/historical-119-scope.json)；這是重開機後的歷史證據，不稱為本輪完整核心通過。 |

三組桌機共 20 項，皆為實際 Chrome、1280 × 800；沒有新增手機介面或手機測試。畫面明確顯示模擬上游。原有流程故意注入主來源 503，因此保留 1 筆預期的 console resource error；三組 JavaScript page error 皆為 0。各次服務使用新資料夾與隨機 loopback 埠，測試結束已正常停止。

整合者於 15:08（Asia/Taipei）另對原 18767 預覽完成 [即時 Chrome 檢查](../runtime/final-preview-smoke/0bee0d3e/report.json)：12 件合成任務、12 通過、0 未知，讀取前後公開統計一致；瀏覽器只送出 GET，沒有 page error 或站外請求。該預覽的產品來源指紋與本輪 Monitor gate 一致；最後同步只有測試、腳本與文件，整合者仍須核對同步後產品指紋沒有改變，若有改變則重跑預覽。

19 個 HTTP 案例的父程序合計觀測到 25 次模擬呼叫；被終止 worker 另留下 1 次已進入上游的觀測。重建後父程序的 0 次新增呼叫沒有被拿來取代該 worker 的實際呼叫。身分安全投影與模式觀測各自連回同案 socket 收據，不以初始 demo bundle 的計數代表真正 CategoryCore 流量。

## 核心必要門檻尚未通過

不加測試篩選的原命令 `./scripts/check --integration`，於 2026-10-22 14:33:39 至 14:52:47（Asia/Taipei）執行。離線 locked sync、依賴相容、project records 與 Ruff 通過；pytest **71 failed, 2483 passed in 1144.99s**，退出碼 1，後續示範與建置未執行。保留 [完整原始日誌](../runtime/final-acceptance/core-full.log)、[官方檢查紀錄](../runtime/final-acceptance/core-full-checks.json) 與 [包裝器收據](../runtime/final-acceptance/core-full-receipt.json)。

- 70 項失敗位於保留的 `* 2.py` 舊快照；本輪沒有刪除或忽略這 17 份、633 個測試。
- 1 項失敗位於目前的 `test_project_state.py::test_historical_pass_and_closed_work_never_imply_current_or_production_completion`。核心調查者正釐清其合成資料與歷史 ownership 的關係；本單沒有自行改動這個測試或替它取得豁免。
- 核心可執行來源摘要前後一致；但 `docs/agent-state.json` 在這次約 19 分鐘檢查中有變動。相同來源摘要不代表 metadata 一致，不能把這次結果說成穩定完整狀態的全綠驗收，也不據此歸責其他工作者。

較早的檢查先停在 RG-04 next step 與既有 records checker 的授權前提欄位不一致。使用者明確允許後，整合者只將 `requires_authorization: false` 改為 `true` 再重跑；[核准與修正收據](../runtime/final-acceptance/approved-records-repair.json) 保留原值、雜湊前提與實際變更。此欄位修正沒有給予付費模型、研究派送、預算或正式資格的新授權。

重開機前提到的三個目前版本 SQL 案例，在同一 locked 環境逐一重跑都通過：bounded review 39.74 秒、compound ORDER 1.27 秒、low research 292.57 秒。[收據](../runtime/final-acceptance/core-narrow-receipt.json) 與原始 [bounded](../runtime/final-acceptance/core-narrow-bounded.log)、[compound](../runtime/final-acceptance/core-narrow-compound.log)、[low research](../runtime/final-acceptance/core-narrow-low-research.log) 可查核。原 [一般檢查](../runtime/final-acceptance/historical-pre-reboot-required-integration.log) 與 [另一輪檢查](../runtime/final-acceptance/historical-pre-reboot-canonical-integration.log) 缺少最後 traceback，歷史失敗根因仍未知；現在單獨通過不算修復，也不能只由靜態呼叫鏈宣告與 Monitor 無關。

下一步須由整合者依明確核定的驗收範圍處理目前 fixture／舊快照門檻，保留原 71 失敗結果，再於穩定且有指紋的核心狀態完成適用整合檢查。任何新核定的隔離驗收都須另列命令、收集範圍、排除清單與結果，不覆蓋這次原命令的失敗。

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

機讀索引只列安全投影、檢查日誌、公開 metadata 及其雜湊，不收錄 fixture 的資料庫、憑證、原始提示或答案。來源檔案與歷史失敗保留不變；文件連結、證據雜湊與秘密樣式掃描的實際結果另在同一索引列出。它們的通過不取代未通過的核心必要門檻。
