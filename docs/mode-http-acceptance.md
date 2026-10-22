# 四模式與任務固定綁定驗收（Issue #22）

本單補強 [#20](https://github.com/alexwang0315/cloudyfan-routing/issues/20) 已核定的真實本機 HTTP 與桌機驗收，工作單為 [#22](https://github.com/alexwang0315/cloudyfan-routing/issues/22)。既有正式程式沒有為本單重寫；新增情境沿用 `TaskService`、`RegisteredContract`、`BudgetedCore`、`RunBoundCore` 與原 `Core`。

這是隔離合成資料的接入證據：模型上游是 `MockTransport`，所有費用都是合成費用。不是正式模型資格、實付帳務、正式部署或一般化的正確率／節省率證據。未新增來源 GET、付費請求或手機介面與測試。

## 可重現的驗收範圍

`tests/test_http_mode_acceptance.py` 包含 8 個真實 HTTP 案例，伺服器監聽隨機的 `127.0.0.1` 埠，客戶端沒有使用 `WSGITransport`。沿用既有 loopback socket allowlist；其他 socket 連線會使測試失敗。

| 行為 | 實際 runner 與檢查 |
| --- | --- |
| 四模式首答 | CloudyFan MRC 使用真正 `CategoryCore`，首個模型為 `example/open_source`。高池優先、便宜優先、開源優先使用既有 `Core` 合法模式，首個模型分別為 `example/high`、`example/low`、`example/open_source`。本 fixture 首答通過，各為 1 次生成、0 次分類；既有路由回歸另保留失敗升級與分類次數上限。 |
| 兩客戶端 | 相同公開 run ID 在 studio、agent 下成功形成兩個不同內部任務，真正 CategoryCore 依各任務組裝；Core manifest 保留伺服器提供的 `arithmetic-small` workload 與固定類別矩陣。自行指定 workload、category binding 或 contract ID 皆先拒絕，沒有模型呼叫。 |
| 設定切換與重建 | 全域模式、客戶端覆寫及已註冊池切換只影響後續任務。重建完整應用後，舊結果、重送、已完成任務的 resume 都保留原內容／模式／類別／池／契約與期限；舊 manifest、job 原始位元組不變，沒有額外模型呼叫。 |
| 中斷與原期限 | 公開驗收器短暫中斷後，普通重送維持中斷；切換設定並重建後，明示 resume 仍使用原 CategoryCore。已取得的模型收據不重送；到原建立後 30 天，結果／重送／resume 均回 410，歷史費用及通過統計保留。 |
| 缺少契約 | 已知類別沒有對應模式註冊時回 409；移除舊契約後，舊結果／重送／resume 先拒絕，模型計數不變。新池即使保留相容的當前 CategoryCore 契約，也不替代舊任務的原契約 ID。 |
| 共用預算 | 兩客戶端及四模式沿用同一 BudgetStore。設定切換與重建後歷史費用保留；不按新客戶端或新應用另建乾淨帳本。 |

第二池是預先註冊的另一個合成視圖；CategoryCore 維持原核定描述，使用不同註冊 ID。候選視圖保留 276 個缺額，沒有為矩陣填入假的正式資格。每個活動池只有 1 類 × 4 模式已註冊，其餘 88 個類別模式組合明確顯示缺少契約。

## 執行方式與機器證據

在本套件目錄執行，`CLOUDYFAN_CORE_ROOT` 指向既有 Core 套件，使用該套件 `.venv`。以下命令不下載套件，也不讀真實供應商憑證：

```sh
CLOUDYFAN_CORE_ROOT=/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core \
PYTHONPATH=src:/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core/src \
CLOUDYFAN_MONITOR_EVIDENCE_DIR=runtime/mode-http-evidence \
/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core/.venv/bin/python \
  -m pytest -q -p no:cacheprovider \
  tests/test_http_mode_acceptance.py tests/test_category_host_integration.py \
  tests/test_integration_composition.py tests/test_monitor.py

CLOUDYFAN_CORE_ROOT=/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core \
  node scripts/qa_mode_desktop.cjs
```

第一個命令同時執行本單情境與原有相關回歸。既有 `scripts/check` 收集整個 `tests` 目錄，因此這 8 個案例也會自動納入套件 gate；環境變數指定的證據目錄保留各案例實際 HTTP 狀態／埠、socket 允許／阻擋紀錄、模型與分類計數，以及不含憑證或提示的觀測結果。本單 fixture 自行匯出真正 CategoryCore 流量，沒有沿用另一個初始 demo bundle 的零呼叫計數。

桌機腳本啟動獨立合成服務與本機已安裝 Chrome，固定 1280 × 800，操作真正控制台的四模式、個別覆寫、23 × 4 契約矩陣及池啟用確認框。任務經真正 HTTP 建立，結果 provenance 與 MockTransport 記錄共同驗證首個模型。再重建服務並讀取舊任務、只讀重送及已完成任務 resume；7 件任務只發出 7 次合成生成，共用帳本為 0.07 美元。

桌機輸出在 `runtime/mode-desktop/<run-id>/`：`desktop-report.json`、`http-receipt.json` 與 4 張截圖。Python 服務限制出站 socket；瀏覽器路由只允許該次服務 origin。合成憑證只在父子程序私有 pipe 中傳遞，報告不保存憑證、提示、答案或原始模型 payload。腳本正常結束時會停止服務並關閉瀏覽器。

## 本次結果與限制

本單相關四個測試模組共 **41 passed**，其中 8 個案例走真正 loopback HTTP，保留 79 次 HTTP 回應／socket 連線與 14 次合成模型呼叫，其他 socket 嘗試為 0。合入 #21 的整合分支後，完整 Monitor gate **88 passed**，Ruff、頁面 JavaScript 語法與離線 sdist／wheel 建置通過，Core 原始碼指紋前後一致；記錄在 `runtime/checks/8c8628034007/checks.json`。桌機腳本另通過語法檢查，桌機 **8 項檢查通過**，有 4 張操作畫面；7 件合成任務、7 次合成模型呼叫，頁面錯誤、console error 與被阻擋的瀏覽器請求皆為 0。

TDD 採逐一加入情境、立即執行：首輪因測試本身誤用 CategoryCore 的 routing version 及含斜線的參數 ID，造成期待值與證據檔名錯誤；重建情境另發現合成 fixture 曾以當前池誤當原始池。修正均位於測試 fixture／證據收集，沒有暴露或修改正式路由演算法缺陷。其餘新增情境第一次執行即通過，沒有製造假的正式程式 red-green 記錄。

桌機重跑也保留一次 harness 失敗：候選資料尚未完成非同步畫面更新時過早按下池啟用，伺服器依版本保護回 409；紀錄保留於 `runtime/mode-desktop/7fc97459-ccd/`。腳本改為等候確認動作完成，以及畫面實際顯示預期候選版本，並直接記錄非 200 回應，避免將版本衝突誤報為瀏覽器逾時。HTTP 證據檔亦改為加鎖及原子替換，避免輪詢與任務回應同時匯出時產生半份 JSON。修正後 `runtime/mode-desktop/fbfc4f1f-ddf/desktop-report.json` 再次 8 項通過，來源指紋與最終腳本一致。

本結果不代表整個 Core 測試套件通過。先前整體 Core 中斷與 SQL 檢查的判定，由後續 [#23](https://github.com/alexwang0315/cloudyfan-routing/issues/23) 彙整；本單沒有改動 Core 研究樹。所有 runtime／截圖／合成收據均排除於本地提交之外，保留供最終整合驗收查核。
