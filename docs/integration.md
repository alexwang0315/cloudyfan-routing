# 與主服務整合

目前狀態（2026-10-23）：受控組裝介面與本機合成接線已實作並測試；主線實際 MRC 執行器仍未透過此組裝啟動，真實任務與共用帳本尚未自動進入 Monitor。`18767` 使用獨立合成預覽；OpenRouter 金鑰用量的真實唯讀查詢不等於實際任務已接通。下列主服務組裝範例仍是接線契約，不能當成已運行服務的證據。

本次使用者已確認 Q1～Q12 及完整方案，並授權本機接入與驗收。規格已發布為 [Issue #20](https://github.com/alexwang0315/cloudyfan-routing/issues/20)；實際執行狀態與證據見[執行檢查點](execution-checkpoint.md)及[驗收清單](main-thread-integration-acceptance.md)。模型測試使用真實 Core／BudgetedCore／TaskService 與模擬上游；真實來源 GET 是獨立步驟，不建立模型或費用資格。

## 受控服務組裝

`assemble_monitor` 消費主服務已建立的 `TaskService`、`IdentityStore`、Monitor `Store` 與精確契約登錄表。它不建立正式資格、不遷移舊任務，也不另開付款帳戶帳本。

```python
from cloudyfan_monitor.integration import assemble_monitor

registry = {
    # 每一組都須由主服務合法建立，缺少組合明示未支援。
    (pool_id, category, mode): registered_contract_id,
}
wsgi_app = assemble_monitor(
    monitor_store, existing_task_service, registry, existing_identity_store,
    operator_token=operator_token,
    source=source_adapter,
    origin="https://your-private-monitor.example",
)
```

主服務必須明確設定版本化的 30 天 `RetentionPolicy`，並提供仍有效的所有舊契約；缺少這個政策時組裝拒絕，不默默替舊任務延長期限。身分有效條件是既有主服務限制與 `IdentityStore.is_active` 同時成立；接入不能覆蓋原停用或租戶限制。每一位 client 沿用固定 principal，換憑證使用既有 IdentityStore 生命周期。

`Store.register_principal` 只登錄穩定身分及顯示名稱，不保存主服務 token。正式接入透過 IdentityStore 驗證 scope、撤銷及有效期；client 的明文 HTTP 僅允許 loopback，正式部署仍須 HTTPS。操作員採獨立管理 token；僅本機合成 demo 提供一次性示範登入。

所有 worker 必須使用同一個受控 Monitor Store 與服務根目錄，並讓同一付款帳戶的所有契約使用原有完整 BudgetStore。服務級 flock 名額限制一次一件模型任務；它不跨主機，不能宣稱提供分散式排程。

## 每個任務的類別核心

`CategoryCore`／`BackupCategoryCore` 在建立時綁定一個內部 run 身分。主服務可以用 `RunBoundCore(template, factory)` 在既有凍結契約下為每個任務組裝 runner；factory 接收實際內部 run_id、已受預算控制的 Client 及 local backend。

每次組裝都檢查 factory 回傳真正 Core、沿用相同受控傳輸及完全相同的 configuration descriptor。已凍結 QCS／備援政策的 workload_id 由伺服器補入，客戶端不能自行傳入或覆寫。主服務仍須供應獨立合格的原矩陣、原 evidence、原 policy、公開驗收與費用限制；這個 adapter 不產生它們。

`CategoryCore` 只接受其 CloudyFan 模式；其他模式必須由主服務分別註冊既有合法 baseline runner。不能把同一個只支援 CloudyFan 的 runner 換 mode 字串就視為四模式接入。

## 同步客戶端介面

```http
POST /v1/tasks
Authorization: Bearer <client-token>
Content-Type: application/json

{"run_id":"unique-1","category":"artificial-analysis:coding_index","prompt":"A task under the registered public contract"}
```

第一版同步執行，並提供：

- `GET /v1/tasks/<id>`：讀目前狀態。
- `GET /v1/tasks/<id>/result`：讀已完成原結果，先檢查身分與存取有效期，再驗證原結果及收據；不刷新共享傳輸。
- `POST /v1/tasks/<id>/resume`：body 僅接受 `{}`，明示接續原任務。
- `GET /v1/monitor`：只讀自己的公開統計與任務。
- `GET /v1/billing`：只讀自己的內部美元餘額、已入帳紀錄與待核項目；[客戶費率與帳務](billing.md)分別管理。

相同 client／run／內容的 POST 只讀取原任務，完成、執行中或中斷皆不派送。內容衝突 409；到期或退休 410。只有新工作或明示 resume 取得全服務名額；滿載立即 429，不建立佇列。查詢及管理不取得執行名額，HTTP 斷線不取消伺服器已接受的任務。程序中斷後不自動續跑，仍受原未知費與預算限制。

任務建立時固定 client、內容指紋、pool、category、mode、contract。設定切換只影響新任務；缺少原契約時拒絕，不遷移成新版本。未經入口的歷史研究不自動接管。

## Ollama 文字入口

主服務可在同一個 `assemble_monitor` 明示傳入 `ollama_models={"cloudyfan/mrc": {"category": category, "contract_id": registered_contract_id}}`。別名固定到精確的既有契約；Ollama 原生解析及回應沿用核心 `OllamaApp`，透過明示的 `submit_task` callback 交給同一 Monitor Gateway，因此保留客戶歸戶、單件容量、原任務重送、30天期限、凍結售價與原預算控制。

提供 `GET /api/tags`、`POST /api/chat` 及 `POST /api/generate` 的單一使用者文字、非串流子集。列模型須有 `models:read`，送題須有 `tasks:write`，讀結果須有 `tasks:read`。Host／Origin 限制及客戶與操作員權限分離照常適用。若現在選定的模式／池無法解析為別名綁定的同一契約，新任務在綁定及派送前回409；已接受任務的相同內容重送仍用原契約。

官方 Python SDK固定 `ollama==0.6.3`，以 `X-CloudyFan-Run-ID` 指定穩定身分、`stream=False`。它會捨棄CloudyFan擴充欄位，完整狀態、選池及成本另從同一任務的結果API取得。公開驗收失敗／無法確認回原生422，仍保留任務、費用與Monitor追蹤。真正無效憑證回401；身分驗證的工程故障回安全503並停止，不能自動重試或放行。

原始核心結果的作答與 Jev 決策會投影到 Monitor 的安全 `trace`：模型池、驗收狀態、模型、供應商、用量、費用、分類分布、保守選池原因及原請求編號。提示、答案原文與原始API回應保留在私有證據，監控快照不帶入。實際的[50題合成案例](ollama50-simulation.md)使用獨立資料集，與主線真實模型接線的資格及帳本分開。

## 管理、帳務與保存

操作員讀 `GET /monitor/snapshot`，修改全域／client 模式使用 `POST /monitor/mode` 與 revision。`capabilities.categories` 明列 23 類乘四模式的契約登錄；已登錄仍須通過原執行前檢查。

總帳以 BudgetStore 路徑去重，保留帳戶全部歷史。只把有原任務歸屬證據的金額分列 client，其餘列未歸屬；未知與預留不填零。停用身分不能抹除已確認的歷史通過紀錄或費用，也不能重新授權讀原文。主機、電力與人工成本維持未知。

結果存取在建立後恰到 30 天即結束，重送與重啟不延長。到期只影響存取，保留已確認公開統計。`POST /monitor/retention/preview` body 為 `{"client":"...","run_id":"..."}`，只產生清理審查：原文滿 30 天可審查，帳務／審計／歷史／版本證據至少 365 天，仍被任務引用或未核帳時延保。共用版本從最後一次實際任務使用起算，查詢及只讀重送不延長。

preview 包含核心精確 inventory、大小、風險、開啟狀態或無法確認原因、保留阻擋與 `deletion_authorized=false`；本次不提供自動刪除，也未執行清除。

## 來源更新

`OpenRouterPoolSource` 只允許固定 OpenRouter metadata 路徑。主服務須明確注入 23 類原生門檻政策與既有來源憑證；候選更新不等於准入。

```python
source_adapter = OpenRouterPoolSource(
    httpx.Client(trust_env=False, timeout=15),
    source_policies["policies"],
    api_key=server_credential,
    endpoint_limit=12,
)
```

主要 GET 為 `/models`、`/benchmarks`、`/benchmarks?task_type=search&include_run_config=true`，另最多 12 個 `/models/<id>/endpoints`。包含失敗最多 15 次嘗試、單次 15 秒、整輪 60 秒，不重試或跟隨 redirect。部署端若允許自訂傳輸，仍需外層期限控制；本次真實 GET 使用訊號期限護欄並保留實際請求紀錄。

主要來源與必要 schema 全部成功才形成候選；個別端點 HTTP、網路、timeout、JSON／schema 失敗或未查可形成明示部分更新。`completeness` 提供 attempted／succeeded／failed／deferred 與明細，URL／雜湊證據不包含憑證。

1. `POST /monitor/pools/refresh` 只準備候選與差異。
2. `POST /monitor/pools/publish` 由操作員檢視後，以 revision 發布候選。
3. `POST /monitor/pools/activate` 僅接受已登錄合法契約的執行版本。

本次真實來源窗口已取得 15 次 GET 成功及 schema 證據，另有 24 個端點未查，故仍是部分更新。使用的是既有 `exploratory_unvalidated` 政策，沒有將門檻稱為獨立能力證明；此次資料未發布或啟用。

## 本機環境與限制

`./scripts/preview` 使用獨立的 `runtime/local-integration`，真實 IdentityStore／30 天政策／Core 搭配 MockTransport、虛構憑證及合成費用。原 `runtime/preview` 保留，需 `--legacy-demo` 才使用舊組裝；舊任務目錄缺少 lifecycle 時拒絕自動遷移。

正式 TLS、跨主機容量、多操作員權限、Queue、完整 OpenAI／Ollama 功能與真實模型／費用資格仍須另外接入及驗收。本機驗收狀態以[清單](main-thread-integration-acceptance.md)及[50題案例](ollama50-simulation.md)各自證據為準，不以開啟頁面或文件整理代替必要證據。
