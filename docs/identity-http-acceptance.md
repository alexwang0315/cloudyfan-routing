# 客戶端身分的 HTTP 與桌機驗收

對應 [Issue #21](https://github.com/alexwang0315/cloudyfan-routing/issues/21)，父規格為 [Issue #20](https://github.com/alexwang0315/cloudyfan-routing/issues/20)。2026-10-22 的隔離合成環境驗收通過；沒有發現需要修改產品程式的接入缺陷。

## 已確認的行為

| 實際入口 | 驗收結果 |
| --- | --- |
| 真正 loopback HTTP：憑證輪替 | 原憑證立即回應 401；新憑證仍讀取原身分、任務狀態及結果。原統計與可歸屬費用保持一致，相同任務重送不新增模型請求。 |
| 真正 loopback HTTP：兩客戶端及 scope | 兩客戶端各自建立、查詢任務。對方的狀態、結果及接續入口回應 404；客戶端不能讀取操作員投影或變更設定。缺少讀／寫 scope 的請求回應 401，兩件既有任務合計只有 2 次模擬作答。 |
| 真正 loopback HTTP：停用與原證據 | 停用後結果及接續回應 401，操作員保留原通過紀錄與費用歸屬。原證據完整時重新啟用可讀；分別破壞合成結果、合成收據後重新啟用，結果回應 503，操作員改列工程中斷及未知，不沿用成功快取。已知支出仍保留，沒有額外模型請求。 |
| Chrome 桌機 1280×800 | 實際點選客戶端明細、清除操作員登入、輸入客戶端憑證並確認登入失敗、重新登入及讀取輪替後畫面、切換費用帳本。原身分、歷史統計與費用保持一致；模擬標示可見，沒有頁面 JavaScript 錯誤。 |

憑證輪替使用既有主服務 `IdentityStore`，由測試程序執行；桌機程序透過父子程序管道要求該動作，沒有新增產品管理端點。憑證只經程序記憶體及管道傳遞；截圖前清空登入欄位，報告只保存回應狀態、公開統計及安全投影。

## 證據

- [完整 Monitor gate](../runtime/checks/532c4f3f1303/checks.json)：80 個測試、Ruff、產品 JavaScript 語法及離線 sdist／wheel 通過，核心來源指紋未變。
- 同一 gate 的 `http-evidence/` 含 4 個新增案例各自的實際 socket／HTTP 狀態紀錄，以及 `-identity.json` 公開回應投影。上游為 MockTransport；socket 只允許該案例指定的 loopback 埠，未出現被阻擋的連線。
- [桌機合併報告](../runtime/identity-desktop/f2146e5e883e/report.json) 與 [瀏覽器報告](../runtime/identity-desktop/f2146e5e883e/browser-report.json)：4 項操作通過；輪替前後公開投影相同，模擬呼叫保持 2 次。Python 記錄 7 次獲准 loopback 連線，瀏覽器沒有站外請求或頁面錯誤。
- 畫面：[輪替前](../runtime/identity-desktop/f2146e5e883e/clients-before-rotation.png)、[客戶端登入遭拒](../runtime/identity-desktop/f2146e5e883e/client-login-rejected.png)、[輪替後](../runtime/identity-desktop/f2146e5e883e/clients-after-rotation.png)、[費用歸屬](../runtime/identity-desktop/f2146e5e883e/ledger-after-rotation.png)。各圖另有同名 ARIA 文字。

首次直接執行曾因未設定本工作樹的 `PYTHONPATH` 而無法收集測試。第一個可執行的輪替案例則因測試誤將客戶端摘要與操作員個別統計的不同欄位集合直接比較而失敗；修正該測試假設後通過，沒有修改產品程式，也沒有將這兩次測試設置問題列為產品缺陷。其後新增的隔離與原證據案例，以及第一輪桌機操作均通過。

## 重跑

在本套件根目錄執行，使用核定的核心原始碼與既有離線環境：

```sh
export CLOUDYFAN_CORE_ROOT=/Users/alexwang0315/Documents/CloudyFan/cloudyfan-jev-core
export PYTHONPATH="$PWD/src:$CLOUDYFAN_CORE_ROOT/src"
"$CLOUDYFAN_CORE_ROOT/.venv/bin/python" scripts/check
node --check scripts/qa_identity_desktop.cjs
"$CLOUDYFAN_CORE_ROOT/.venv/bin/python" scripts/qa_identity_http.py
```

HTTP 測試自動納入既有 gate。桌機 runner 使用新的 `runtime/identity-desktop/<id>/`，不接觸主線資料或既有帳本；需已安裝 Chrome 及 Playwright。預設沿用本機既有 Playwright 模組，可用 `CLOUDYFAN_PLAYWRIGHT_MODULE` 指定其他已安裝位置。桌機報告只在所有斷言通過後標記 `verified: true`。

## 適用範圍

本輪是實際本機 HTTP 與桌機 Chrome，模型作答、費用與身分資料均為獨立合成資料。它不證明正式服務部署、真實模型能力或實付費用。手機介面與手機測試依使用者指示排除。沒有來源 GET、付費模型呼叫、部署或資料刪除；核心研究樹沒有修改。核心整體檢查與先前 SQL 失敗的判定由父工作單另行處理。
