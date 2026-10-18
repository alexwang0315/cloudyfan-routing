# Routing Core 正式放行索引

更新日期：2026-10-19。範圍限於[主規格 #1](https://github.com/alexwang0315/cloudyfan-routing/issues/1) 的 routing core 與 T01～T12 直接配套；不涵蓋其他部門、商業定價或硬體採購。

## 狀態與使用方式

- **`implementation_complete`**：指定版本的程式與離線契約檢查完成。主規格 #1 交付有 529 項離線測試、Ruff、離線 demo 與審查紀錄，詳見 [verification.md](verification.md) 與[實作索引](implementation-spec1.md)。這是該次交付的歷史驗證數，不代表之後每個版本已重測。
- **`production_qualified`**：針對明確的領域、模型設定、來源、客戶端及部署環境，所有適用放行項均有有效證據與具名核定。目前狀態只讀取 [release-gates.json](release-gates.json)，不由本表或 Issue 狀態推定。
- GitHub Issue 結案、CI 綠燈、fixture 通過、資格包中的 `production_ready`，都不能單獨將整個服務標為 `production_qualified`。資格包的核定僅涵蓋它綁定的模型池輸入。
- 下表整理既有缺口，不新增執行授權。`RG-*` 是本索引的追蹤識別碼；`G-01`／`G-02`／`G-03` 沿用主規格驗收 ID，不能與離線 ticket 是否結案混為一談。

正式放行需先明定目標範圍。僅供內嵌 Core 的交付不得聲稱 Ollama 相容或多使用者有界服務；不適用項需具名記錄理由，不能直接刪除。具名負責人與核定者以 [release-gates.json](release-gates.json) 為唯一狀態來源；下表專業角色是待指派責任，不是已存在的團隊或承諾。

## 放行項與證據缺口

| ID／適用範圍 | 現有可追溯證據 | 尚缺的正式證據與完成條件 | 負責人／執行前提 |
|---|---|---|---|
| **RG-01：公開驗收與修正**，所有執行模式 | [公開診斷契約](public-diagnostics.md)、[HTTP 契約](http-task-api.md)；`Core.run` 注入驗收器測試已驗證版本、失敗／未知、修正及不重送邊界。 | 應用領域的契約語義、歧義審查、驗收器正反例與工具 sandbox 審核；公開訊息不得洩漏隱藏答案。更換驗收實作須新契約版本，完成獨立核對後才可套用到該領域。 | 責任角色（應用／驗收負責者）；先核定任務與資料使用範圍。離線通過不授權真實資料處理。 |
| **RG-02：模型、強度與量化資格**，每個領域及實際候選 | [資格包](qualification.md) 已測精確設定、缺項排除及固定輸入核定；目前交付僅合成包，沒有核定真實模型或門檻。 | 具名核定 domain、source、thresholds、coverage、expiry、access 與每個 config；固定版本／強度／供應商／能力／上下文／估計依據。本地配置另須量化、引擎與權重身分；不能繼承基礎模型分數當成量化品質。須通過當下有效的 `qualified_snapshot` 路徑。 | 責任角色（領域及模型資格審查者）；實際校準或模型請求需另有計畫、成本及執行授權，且不得依 holdout 調門檻。 |
| **RG-03：來源權限、時效與更新營運**，啟用的每個評分來源 | [評分整合](score-integration.md) 記錄 LiveBench 公開唯讀歷史證據；[更新操作](score-update-operations.md) 已離線測預覽、競態、原子切換及固定舊任務。 | 核對實際帳戶存取、資料使用／再散布條款、主來源版本與原生尺度；AA／OpenRouter authenticated access、CodexRadar 遠端能力仍未驗證。指定時效、更新執行帳號／頻率、檔案權限、失敗處理及核定者；實際驗證過期阻擋與更新 audit，不能只以新下載時間延長資料效期。 | 責任角色（來源／更新營運者）；來源存取及背景排程需相應授權。受控匯入可保留為明確模式，不冒稱已驗證遠端 API。 |
| **RG-04：完整成本與可強制上限**，任何受控模型／工具執行 | [預算 admission](budget-admission.md) 已離線測完整預留、精確金額、併發、歷史與未知停止；[供應商 adapter](provider-bounds.md) 與獨立[元件上界](component-bounds.md) 已測 admission／派送／撤銷／核帳。未附真實供應商封頂或元件資格，普通 key 額度不放行。 | 精確請求／模型／供應商／工具的可強制上限，涵蓋分類、advisor、生成、唯一修正及失敗。可採合格供應商美元封頂，或獨立核定的完整數量／有效費率上界；v2 完整零費率元件可省略用量上限，須有全路徑保證與來源、獨立審查，不由零標價推定。元件證據含拒絕輸入、cache、tier、價格變動、在途及失敗保障。核對帳戶配置、歷史與未知費用；估價與 token 參數本身仍不足。 | 責任角色（帳務及供應商整合負責者）；有效付費授權、完整預留與停止規則仍必要。真實資格與 reviewer 尚待核定；本地零模型費不能推論主機免費。 |
| **G-01：local_only 與工具外送隔離**，宣稱本地限定的部署 | [執行位置政策](execution-policy.md) 與 [local backend](local-backend.md) 已用注入後端／MockTransport 檢查生成、分類與工具位置；公開通過跳過分類，失敗不偷渡雲端。 | 核對實際 engine、代理、工具、驗收器、憑證與 egress；在目標環境證明同一 origin 限制，包含失敗／修正路徑。可信位置聲明不是 OS 網路 sandbox。實際 LocalBackend 目前不支援 tools，不得引用 test-only backend 充當支援證據。 | 責任角色（隱私及執行環境負責者）；先核定資料、網路觀察及隔離測試範圍，實機操作另需授權。 |
| **RG-05：身份、保留及應用隔離**，HTTP／Ollama／Queue 接入 | [HTTP 安全邊界](http-task-api.md) 已離線測擁有者、結果／費用投影、allowlist、私有 workspace 與安全錯誤，並加入[本地 API token 身份](identity.md)與[保留整合](retention.md)：scope、到期／撤銷、帳號停用、TTL、退休標記與明確批准清除。 | 尚缺部署身份／TLS、secret 管理與角色核定、契約 factory／工具 sandbox、儲存／備份／記錄存取控制，以及實際保留政策與維護 fence 核定。結果不得洩漏其他使用者資料、私人 prompt、原始收據或共享帳務。 | 責任角色（身份／資料保護及應用營運者）；先指定私有測試資料與授權範圍，保留或刪除作業遵守各自核定，不由本索引觸發。 |
| **G-02：真實客戶端互通**，宣稱 Ollama 文字子集或指定產品接入 | 已以[官方 Python 同步 SDK 0.6.3](ollama-sdk.md)對接 WSGI／模擬上游，固定 `cloudyfan-ollama-text-v2`；認證、文字回應、錯誤、重連不重送與不支援功能拒絕已測。SDK 會忽略擴充 metadata，完整帳務以同 run GET 讀取。 | 由放行責任人審閱精確 SDK／協定／HTTP 擴充範圍與收據；不把單一同步 SDK 證據擴及 Async／JS／產品 client、真實 TCP／TLS／代理或完整 Ollama。正式 registry 仍 pending，未核定部署 scope。 | 責任角色（客戶端整合者）；本輪授權只含固定 SDK 與 mock 上游。真實模型須另過 RG-02／RG-04，實際接入／部署另依授權。 |
| **G-03：有界排隊與多使用者營運**，宣稱有界服務的部署 | [排程契約](bounded-scheduler.md) 已離線測容量、配額、身份、取消、重啟與固定負載；`production_slo_validated` 仍為 false。 | 固定目標負載／並行／deadline 與事前通過條件，在實際部署量測等待、完整延遲、隔離、錯誤與中斷處理。只能經 `QueueHTTPApp` 宣稱有界；同步 `HTTPApp`／`OllamaApp` 不得成為旁路。驗證 worker 停止、未知費用保留及人工恢復流程；不能把 running cancel 當停止付費或退款。 | 責任角色（服務／排程營運者）；先核定部署與負載測試、時鐘及恢復責任。多主機排程或自動 crash recovery 不在現有資格內。 |
| **RG-06：本地硬體與推論運行**，每個宣稱可用的硬體配置 | [本地量測矩陣](local-benchmark.md) 提供 fake clock／成功／OOM／中斷流程；所有真實硬體矩陣項仍 pending。 | 固定權重 digest、量化、engine build、硬體／OS／RAM 後，逐項驗證載入、warm inference、模型切換、記憶體／OOM／中斷、宣稱並行與長時間穩定性；TTFT 需 first-token probe，功耗需量測，不能由非串流延遲或 TDP 推估。模型品質另連 RG-02。 | 責任角色（本地推論／硬體驗證者）；模型授權、下載、啟動、故障測試、停止／恢復方案須先獲授權。Mac Studio 128G 只是候選條件，尚非容量證據。 |
| **RG-07：品質、成本與延遲對外主張**，任何新增實驗／產品成效宣稱 | [實驗預檢](experiment-preflight.md) 與[比較報告](comparison-reports.md) 已離線驗證凍結、配對、全流程成本、未知／失敗與品質門檻；合成範例不是實測降本。 | 用已核定設定、獨立題目與相同契約保存可審查實測證據；分類、工具、修正、失敗及未配對／校準成本保留。品質未達標僅能說支出差異。假設零模型費與事後分析分列，完整營運成本缺項維持未知。 | 責任角色（實驗及成效審查者）；新實驗須另有核定計畫及成本授權。`open_source_fixed_low` 目前僅 plan 定義，不因此取得執行綁定或授權。 |

## 證據更新與核定

1. 每次審查固定 commit、鎖檔／執行環境、目標領域、契約版本、模型設定／snapshot／pack ID，以及客戶端或部署版本。沿用[評分資料層的來源原生尺度與固定快照契約](score-integration.md)，不以新的 current 改寫既有任務。
2. 每項另附可讀的驗證紀錄：Gate ID、實際命令或流程、測試資料類型（合成／真實）、日期、結果、適用範圍、證據位置／hash、具名執行者與核定者、授權參照及失效條件。未指定責任人或缺少證據時維持 `pending`；失敗與未知另記，不能省略。
3. 本索引只連到去敏報告與證據位置。原始 prompt、答案、收據、帳戶資料、憑證及具個資的測試資料留在受控私有儲存；不得為完成本表上傳原始資料。
4. 輸入、模型強度／量化、來源／條款、契約、驗證器、auth、工具、queue 或部署環境改變時，重新檢查受影響項。舊證據保留為歷史，不直接覆寫或繼承；模型池依資格包的輸入 hash 與效期重新核定。
5. 針對明確發布範圍列出所有適用項、已核定項及具名的不適用理由，完成後才記錄該範圍的 `production_qualified`。此動作本身不啟動服務、不授權支出，也不擴大到其他客戶端、硬體或任務領域。

本次索引整理沒有真實 API、模型下載、硬體操作、部署或原始碼推送。統一驗證入口與跨接入契約測試的結果，應以本輪實際驗證紀錄更新；不預先將正在進行的工作列為通過。

## 機器可讀索引與離線檢查

`docs/release-gates.json` 是正式放行狀態及具名責任的單一來源；本文件保留各項適用條件與驗證要求。歷史驗證數、Issue 結案與實作完成紀錄均不回寫正式資格。執行：

```sh
.venv/bin/python scripts/release_gates.py
```

有效但仍有 `pending` 的記錄會以退出碼 0 表示**索引結構有效**，並輸出 `production_ready: false`。索引錯誤、缺漏或過期則退出 1。此檢查沒有網路、模型呼叫、部署或授權操作。

Schema v1：頂層僅含 `schema_version: 1`、`scope: {id, description}` 與 `gates`。完整保留 G-01～G-03、RG-01～RG-07，各 ID 恰好一次。scope 的描述必須由核定者確認涵蓋確切目標領域、模型設定、客戶端與部署環境；目前 prospective scope 不是已核定的發布範圍。

每項僅含 `id`、`status`、`scope_id`、`owner`、`reviewer`、`rationale` 與 `evidence`：

- `pending`：可先具名分派 owner／reviewer，未指定以 null 表示；evidence 保持空陣列、rationale 為 null。失敗、未知與候選證據保存在歷史紀錄，不藉加入未通過紀錄改變資格。
- `qualified`：必須有具名 owner／reviewer 與至少一筆有效的 real 證據；rationale 為 null。fixture、synthetic、Issue 結案與測試通過不能作為資格證據種類。
- `not_applicable`：不可刪除該 Gate；必須有具名 owner／reviewer、明確 scoped rationale，及有效 scope_decision 核定紀錄。

每筆 evidence 僅含 `kind`、`method`、`result`、`scope_id`、`path`、`sha256`、`observed_at`、`expires_at`、`authorization_ref`。qualified 的 kind 為 `real`、result 為 `passed`、method 為 `observed_execution` 或 `manual_review`；不適用核定的 kind 為 `scope_decision`、result 為 `approved`、method 為 `manual_review`。scope_id 必須等於該項及頂層 scope.id。核定紀錄須在去敏內容中列明具名執行／核定者與精確範圍，不能僅寫「通過」。

path 為本 repo 內的相對檔案路徑，不允許 `..`、絕對路徑或逃出 repo 的 symlink；sha256 為檔案內容的 64 位小寫 hex。時戳須包含時區且符合 `observed_at <= now < expires_at`。authorization_ref 指向既有的限定授權，填入字串不會建立新授權。只引用去敏證據，不上傳私人 prompt、答案或原始收據。

Python API：`validate_gates(data, root, *, now=None)` 回傳錯誤列表，now 可注入含時區 datetime 作固定時間測試；不修改輸入或檔案。只有對**同一份資料、同一 root 及有效當下時間**取得空錯誤列表後，才可呼叫 `production_ready(data)` 聚合狀態。所有項目須為 qualified 或有核定的不適用，才得到 true。

**限制：這是結構與檔案完整性檢查，不能驗證人員身分、授權真偽、證據內容真實性或領域適用性。** 將 fixture 謊稱 real 仍可能騙過結構檢查；hash 不證明實測，具名欄位不等同簽章。人工審查本表要求仍是必要條件，checker 不得用作付費執行、安全存取或部署的授權邊界。不得把 `production_ready: true` 自動轉換成服務啟動或成效宣稱。
