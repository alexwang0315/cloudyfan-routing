# CloudyFan Monitor 0.1

CloudyFan routing core 的獨立管理介面：看任務、失敗升級、完整帳本、23 類四池與每個客戶端，並切換新任務的路由模式。沿用既有 Core／BudgetedCore／TaskService，不另外實作一套路由演算法。

## 本機預覽

```sh
cd /Users/alexwang0315/Documents/CloudyFan/cloudyfan-monitor
./scripts/preview --port 18767
```

開啟 `http://127.0.0.1:18767`。預覽只綁定 loopback，使用真實 Core 程式搭配 `httpx.MockTransport`；不讀 OpenRouter 金鑰、不呼叫真實模型、不部署主服務。示範的費用是合成數值，延遲是模擬流程的執行時間，頁面持續顯示「模擬上游・非真實成效」。預覽中的 23 類候選來自既有公開盤點，來源時間和 pending／缺額如實保留；沒有把歷史研究准入轉成目前 production 可用。

需要核心目前的鎖定離線 Python 環境。可用 `CLOUDYFAN_CORE_ROOT` 指定其他已備妥的 core checkout。沒有自動下載依賴或偷偷重跑核心的付費研究。預覽重新啟動沿用獨立的 `runtime/local-integration`；使用真正 IdentityStore 與 30 天政策，舊 `runtime/preview` 保留；以 `python -m cloudyfan_monitor --demo --data ...` 明確指定新目錄可建立另一套獨立示範。

手機端介面製作與測試已依使用者 2026-10-22 最新指示停止，本次新增畫面驗收限於桌機。

「客戶帳務」另提供內部美元帳本、全域／客戶折扣或加成倍率，以及開源服務的輸入／輸出每百萬 token 售價。預設不啟用扣款，設定僅影響新任務；需明示啟用唯讀 OpenRouter 帳務來源時，使用[帳務接線方式](docs/billing.md)，一般預覽仍不讀供應商憑證。客戶售價與原核心成本、派送預算分開。

另有[一個 Ollama 客戶50題模擬](docs/ollama50-simulation.md)：以 `./scripts/preview --simulation50 --port 18773 --data runtime/ollama50-example/data` 建立獨立環境，再從總覽明示啟動。官方 Ollama SDK 經真正本機 HTTP、同一 Monitor Gateway 與 MRC 核心送題；模型與 Jev 回應、成本及客戶美元帳本都是合成資料。此模式提供進度、四池作答時間線、Jev 選池／保守備援、逐筆費用與零成本開源服務的客戶售價，啟動時不讀真實帳務憑證。

## 第一版功能

| 頁面 | 內容 |
| --- | --- |
| 運作總覽 | 首答／最終公開驗收、未知／中斷、升級比例、一次／兩次 Jev、P50／P95、預算與近期客戶端活動 |
| 任務與升級 | 客戶端／模式／文字篩選、凍結模式、逐階作答路徑、分類次數、完整 API 與延遲；展開檢視入口、四池狀態、模型用量與 Jev 每階段選池證據 |
| 帳本 | 核心共享帳本去重、已知／完整總額、未知、預留、剩餘、逐筆 generation／classifier／失敗／中斷費用證據；usage 與核帳不相加 |
| 客戶帳務 | OpenRouter 實際帳務讀取、版本化倍率與開源模型售價、客戶入帳、冪等扣款及待核項目；僅內部美元紀錄 |
| 23 類四池 | 92 池、276 位置、主選／備援、模型／供應商／強度、原生 quality、cost、speed、缺額及來源時間 |
| 客戶端 | 每個已認證 client 的任務／通過／升級／延遲、模式覆寫、最近活動及設定審計 |

四模式：CloudyFan MRC、全大模型優先、全便宜模型優先、全開源模型優先。模式的嘗試與升級語意以注入的既有核心為準：CloudyFan 最多三次作答／兩次分類，便宜與開源先答失敗升高池；固定高池仍遵守既定核心上限。切換是改未來任務的 default，既有任務不改設定或重送。

「連線」顯示的是已認證客戶端的最近活動，不是推測 TCP socket 數；原本沒有經過這個 gateway 的程式不會自動出現在儀表板。公開驗收不等於獨立隱藏正確率。主機、電力、人工未計價時保持未知。未知費用繼續由核心阻擋派送，Monitor 一般讀取不核帳、不放寬預算或發送模型探針；明示啟動的50題合成控制器會在批次結束後分批核對該獨立環境的合成費用。

## 模型池更新分成兩個動作

1. `POST /monitor/pools/refresh`：抓取固定 OpenRouter `models`、`benchmarks`、搜尋 benchmark 與有上限的端點 GET；正規化後準備 23 類候選，回傳差異。失敗保留舊版本，不跟隨 redirect，不把憑證存進證據。預覽使用離線來源。
2. `POST /monitor/pools/publish`：以 revision 檢查套用候選資料。新資料不自動取得路由資格。
3. `POST /monitor/pools/activate`：僅允許主服務已註冊的確切 pool／category／mode／contract 組合；不接受瀏覽器傳入模型或判分器。舊 task 保留舊契約。可回到仍有契約的舊版本。

候選更新按各類原生 quality 門檻、同 token 情境報價及同類 benchmark 時間整理，不把它當成完整流程成本或正式 runtime 資格。真正派送使用主服務註冊的既有 QCS／備援流程。榜單未明示版本、供應商、強度或速度時不猜值。來源衝突、資料過期與缺額留在畫面。`provider_observations` 是供應商公開觀測，不是即時可接單保證。

## 接到真正核心

見 [接入說明](docs/integration.md)。需要明確注入目前主服務的受控 `TaskService`、合法模型池版本與各模式 `RegisteredContract`。本輪沒有把 parent 的研究執行器切到這個入口，也沒有解除模型或費用資格。

## 離線驗證

```sh
./scripts/check
```

檢查使用核心既有 `.venv` 和來源，不修改 core／SQL／原始帳本；執行 Ruff、JavaScript 語法、全部 Monitor 回歸和離線 sdist／wheel 建置，輸出在 `runtime/checks/<id>/`。一般測試禁止 socket；7 項真實本機 HTTP 測試只開放各自專用的 loopback port，模型上游仍是 MockTransport，保留實際 socket 及呼叫紀錄。此為新 Monitor 套件的完整驗證，不冒充主工作區全部測試重跑。

本機 demo server 用於操作驗證；正式 HTTP/TLS、權限與部署環境仍需主服務接入驗收。來源 GET 已另在有界窗口實測：15 次嘗試全部成功並通過 schema，另有 24 個端點未查，明示部分更新；新資料未發布或啟用，不建立模型或費用資格。

2026-10-22 最新本機接入驗證：76 項 Monitor 回歸、Ruff／JavaScript 語法／離線建置通過；包含 7 項真實本機 HTTP 與真正 CategoryCore 接線。桌機 1280×800 的 8 項操作通過。核心整個 dirty 工作區的檢查另列，不冒充全部通過；詳見[接入驗收](docs/main-thread-integration-acceptance.md)、[執行檢查點](docs/execution-checkpoint.md)與[規格 Issue #20](https://github.com/alexwang0315/cloudyfan-routing/issues/20)。[原獨立套件驗證](docs/verification-20261022.md)保留為歷史證據。
