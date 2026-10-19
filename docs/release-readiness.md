# Routing Core 正式放行索引

更新日期：2026-10-19。範圍限於[主規格 #1](https://github.com/alexwang0315/cloudyfan-routing/issues/1) 的 routing core 與 T01～T12 直接配套；不涵蓋其他部門、商業定價或硬體採購。

## 三個狀態分開記錄

🟢 **既有 RG-04 v2 與本輪 RG-01 SQL 接入是已交付的實作子集，正式資格需另核定。** 本輪 [SQL Environment](sql-environment.md)補上公開診斷投影、工程中斷與保留答案續跑，實際離線結果見[v1 歷史證據](evidence/rg01-sql-offline.json)；最新 v2 見[正式審閱包](rg01-review-ready.md)與[本輪證據](evidence/rg01-review-ready-offline.json)。前輪 RG-04 v2 的 `f1dd57c8a7963422c5ffd7e1c27766c7115ebd9b`／944 項完整測試保留為[歷史證據](evidence/rg04-zero-rate-offline.json)，不混成這次測試數；完整檢查歷史見[驗證紀錄](verification.md)。v2 的子程序／CPU／SQLite allocator／外部逾時不等於整個程序 RAM 或 OS security sandbox；嚴格 AS 模式不支持即拒絕。

| 狀態 | 表示什麼 | 目前如何認定 |
|---|---|---|
| **實作完成** | 既定範圍內的程式與接入契約已交付；不包含另列的選配功能。 | 以下逐項列出實際完成的子集，參照[實作索引](implementation-spec1.md)及各功能契約。 |
| **離線驗證完成** | 指定來源版本的 mock／合成資料／固定 SDK 契約已通過，且證據能對上該來源。 | 最新完整驗證依上述本輪證據；SDK、硬體 probe、來源、資格包各有自己的範圍，不能外推成實機成功。 |
| **正式資格待核定** | 仍需首版實際範圍的真實證據與具名核定。 | [release-gates.json](release-gates.json) 十項均為 `pending`，owner／reviewer 均未指定、正式 evidence 為空；這不是「實作未完成」。 |

現有 `project-state` 的 `implementation_complete` 是來源、已完成工作計畫與離線驗證紀錄一致的彙整結果，並非這三欄的新資料格式。本次不修改 checker 或 registry schema，也不將離線證據填成正式資格。`production_qualified` 只依 registry 的有效核定結果，不從 Git 提交、Issue 結案、測試數或單份模型池 `production_ready` 推定。

## 放行項：實作、離線驗證、正式資格

下表「完成」只涵蓋列出的既有子集。實際首版範圍仍須具名核定；「依啟用範圍」不是已批准 `not_applicable`，本次不改任何 gate 狀態。

| 放行項 | 實作完成：已交付範圍 | 離線驗證完成：可追溯證據 | 正式資格 | 首版適用性 |
|---|---|---|---|---|
| **RG-01 公開驗收與修正** | 完成：可注入驗收器、版本化公開診斷、唯一修正；新增 SQL v2 子程序、資源策略、公開診斷投影、工程分類及八類語義審查材料。 | 完成：[診斷／修正](public-diagnostics.md)、[SQL 接入](sql-environment.md)、[跨接入契約](transport-contracts.md)，`Core.run`＋真 SQLite／MockTransport。 | **待核定**：[委託決策](rg01-first-scope-decisions-20261019.md)已定首個資格切面、業務語義、有界數值域與 Linux 目標；新政策與 13 個已知反例已接入並通過新版離線回歸；[Scout 限定範圍取證](rg01-scout-target-20261019.md)正常流程 8/8、七項 7/7 已完成；目前只待 RG01-08 正式具名審閱，正式資格仍 pending。 | 所有路由必需。 |
| **RG-02 模型／強度／量化資格** | 完成：資格包、三模型池、精確設定綁定、未合格候選排除及效期檢查。 | 完成：[合成資格包與核定阻擋](qualification.md)、[評分快照接入](score-integration.md)。 | **待核定**：首版真實候選、門檻與設定。 | 所有實際使用的模型必需；量化只在使用該設定時適用。 |
| **RG-03 評分來源與更新** | 完成：既有來源 adapters／受控匯入、固定快照、更新預覽、原子切換與 audit。 | 完成：[來源與快照](score-integration.md)、[更新操作](score-update-operations.md)；LiveBench v1 有歷史公開唯讀 smoke，非正式模型池。 | **待核定**：實際啟用來源的權限、映射、時效與更新責任。 | 三池所用來源必需；不用同時驗所有來源。 |
| **RG-04 完整成本與可強制上限** | 完成：共享預算、完整預留、核帳／未知停止、供應商 adapter、元件上界及 **v2 完整零費率保證路徑**。 | 完成：[預算](budget-admission.md)、[供應商契約](provider-bounds.md)、[元件契約](component-bounds.md)、[v2 驗證](evidence/rg04-zero-rate-offline.json)。 | **待核定**：真實供應商上界／零費率及完整帳務證據。 | 任何受控模型／工具執行必需，含分類、初答、修正與失敗。 |
| **RG-05 身份、保留與隔離** | 完成：Token 身份、scope、輪替／撤銷、帳號停用、TTL、退休與受控清除整合。 | 完成：[正式身份機制的 mock 接入](identity.md)、[資料保留](retention.md)、HTTP／Ollama／Queue 回歸。 | **待核定**：部署身份／TLS／secret、存取／備份及保留政策。 | 啟用 HTTP／Ollama／Queue 等應用入口時必需；純內嵌範圍須另核定適用性。 |
| **G-01 local_only 與外送隔離** | 完成：生成、分類與工具派送前的執行位置政策檢查。 | 完成：[位置政策](execution-policy.md)、[本地文字 adapter](local-backend.md)，mock 外送／拒絕路徑。 | **待核定**：實際 engine／代理／工具／驗收器的 egress 與位置證據。 | 宣稱 `local_only`／本地外送隔離時必需；使用本地首答不自動等於全程 local_only。 |
| **G-02 指定客戶端互通** | 完成：Ollama 非串流單輪文字子集，與官方 Python 同步 `ollama==0.6.3` 的接入。 | 完成：[SDK／text-v2](ollama-sdk.md)：認證、錯誤、重連不重送、明確拒絕不支援功能；上游為 mock。 | **待核定**：該精確 SDK／協定範圍的審閱。 | 宣稱該 Ollama 接入時必需；不用新增其他 SDK 才能審閱現有範圍。 |
| **G-03 有界排隊與多使用者** | 完成：QueueHTTPApp、容量／配額、身份、取消、重啟與受控 worker 派送。 | 完成：[排程契約](bounded-scheduler.md)，合成負載、競態、未知費用與中斷測試。 | **待核定**：目標負載／並行／延遲、隔離及恢復演練。 | 對外提供有界排隊／多使用者服務時必需；`production_slo_validated` 仍為 false。 |
| **RG-06 本地硬體與推論運行** | 完成：本地文字 adapter、載入／切換／TTFT／資源事件的量測框架。 | 完成：[本地量測](local-benchmark.md)，fake clock／成功／OOM／中斷；未測真實硬體。 | **待核定**：實際權重、量化、engine 與硬體運行證據。 | 真正使用或宣稱支援本地硬體時必需；API 開源模型不取得本地硬體資格。 |
| **RG-07 品質／成本／延遲主張** | 完成：實驗預檢／凍結、既有四組執行綁定、配對報表、完整成本及未知保留。 | 完成：[計畫契約](experiment-preflight.md)、[比較報表](comparison-reports.md)，合成驗收與品質門檻測試。 | **待核定**：對應版本／任務／設定的實測與可主張範圍。 | 有品質、降本、延遲等對外主張或新實驗時必需；舊實驗不自動核定新版。 |

## 首版真正必需的證據與優先順序

2026-10-19 已以既有資料備妥[首版範圍草案](release-scope-draft.md)、[RG-01 驗收器資格準備包](rg01-validator-qualification.md)及[RG-04 缺證矩陣](rg04-evidence-gaps.md)。三份是審查輸入，**不是具名核定或真實供應商證書**；[來源／缺項清單](evidence/release-preparation-inputs-20261019.json)與[本輪離線驗證](evidence/release-evidence-preparation-20261019.json)分開保存。SQL 僅為首版候選領域，未自行停用既有入口、核定不適用項或變更正式 registry。

這是**證據準備順序**，不是新的實作欠項清單，也不授權任何付費、部署或對外聯絡。優先準備既有單機、非串流純文字流程的證據；保留「開源首答 → 公開驗收 → 低／中 → 必要時中／高 → 唯一修正」。本節不自行核定發行範圍、不停用既有介面，也不把本地功能或多使用者需求擅自移出已承諾範圍。

### 共通證據（所有首版實際路由）

| 優先順序 | 必補證據／完成條件 | 可先做的準備與執行邊界 |
|---|---|---|
| **P0-1 範圍及核定責任** | 固定一個首版任務領域、公開驗收契約版本、入口／客戶端、雲端／本地後端、模型設定、來源與資料範圍；逐項指定具名 owner／reviewer。 | 整理發行範圍清單，判定下節哪些條件實際啟用。scope 未核定前不自行把任一 gate 設為不適用。 |
| **P0-2 RG-01 答對的判準** | 實際領域的公開驗收器正反例、歧義審查、資源上限、診斷／修正契約、隱藏資料隔離，及可重現證據。 | 可先用本機 fixture／既有回答做離線審閱。若驗收器或工具會執行 SQL／程式，仍須隔離與資源證據，不因模型只回文字而豁免。更換驗收語義須新版本。 |
| **P0-3 RG-04 完整費用保障** | 對精確 Jev Decisions 版本／provider／endpoint，取得**每個外部 call 全部可計費 input 的累計硬上界**及有效費率；含序列化、拒絕、失敗、取消、內部重試與在途。其餘元件逐一提供有限上界，或獨立核定的全路徑零費率保證；初答／修正端點也須各自合格。 | 整理所缺供應商文件、完整費目與歷史／未知費用盤點；本次不聯絡供應商。v2 不再強求有完整零費率保證元件的 token 上限，但公開零標價、key 額度及付費抽樣不能取代保證。核帳範圍與未知停止必須保留。 |
| **P1-1 RG-02／RG-03 首版來源與模型池** | 對**實際選用的一個主來源／版本／原生 metric**，補使用／再散布權限、存取、強度映射、時效及更新責任；核定開源首答與低／中／高候選的版本／強度／供應商／能力卡、事先門檻與覆蓋，產出有效 qualified_snapshot。 | 可先審閱來源與設定／既有資料、驗證受控匯入和到期阻擋；不強迫接完所有來源、不把未知強度當低分。需要新的校準或模型呼叫時另提計畫與授權，禁止 holdout 調門檻。 |
| **P1-2 實際執行路徑取證** | 在前述資格及授權具備後，對精確設定驗證初答、需要時的分類／唯一修正、實際模型／provider、失敗／未知停止與最終核帳。已有回答僅在題目、設定、契約均一致時重用。 | 本次只列待驗證，不啟動模型請求；先取得可執行上界、完整流程預留及另行核定的最小測試計畫／預算。小樣本帳務觀察不升格為普遍上界或節費率。 |
| **P2 RG-07 對外主張** | 發布品質／節費／延遲主張前，補對應凍結版本與同題契約的完整配對證據，保留分類、修正、重試、失敗、未知與校準成本，列明適用範圍。 | 先整理既有證據的可重用範圍，不預設要再跑一批新實驗或指定題數。品質不等／未知僅報支出差異；假設開源費為零與事後分析分列。若首版不發布該類主張，須具名核定範圍與理由，不能私自略過 RG-07。 |

P0-2 與 P0-3 可並行準備；順序不表示可以跳過較後列出的安全或帳務條件。任何實際服務／模型派送前，還必須完成所有適用的下列接入與環境證據。

### 只有啟用時才必需的首版證據

| 啟用條件 | 必補證據 | 優先順序／已有基礎 |
|---|---|---|
| **HTTP／Ollama／Queue 對外入口：RG-05** | 真實身份／可信 TLS 代理設定、secret 管理、allowlist／scope、停用／撤銷、儲存／備份權限、保留政策與獨占 maintenance fence；確認無跨使用者資料或共享帳務洩露。 | **P1、上線前**。身份與保留機制已完成；需核定實際配置及整合證據，不要求先導入 OIDC／SSO。 |
| **指定 Ollama 客戶端：G-02** | 具名審閱現有同步 `ollama==0.6.3`／text-v2／同 run GET 帳務擴充的範圍及收據；若主張真實部署連線，另補該部署 TCP／TLS／代理的證據。 | **P1，可先做不花模型費的既有證據審閱**。不因 Async／JS／Open WebUI／OpenClaw 尚未驗證而否定已完成的同步 SDK 範圍。 |
| **有界排隊／多使用者服務：G-03** | 固定負載、並行、deadline 與事前標準；實際等待／完整延遲、配額與身份隔離、worker 停止／中斷／人工恢復、未知費用與預留保留。所有有界請求經 QueueHTTPApp，不開同步旁路。 | **P1、承諾服務能力前**。核心排程已完成；自動事故恢復不是首版必備，但人工恢復安全證據不能省略。 |
| **真正本地推論：RG-06** | 固定權重 digest／量化／engine build／硬體／OS／RAM，補載入、實際回答與公開驗收、記憶體／OOM／中斷及限定工作負載穩定性；模型品質另對 RG-02。 | **P1、本地模式上線前**。adapter／量測框架已完成。宣稱切換、並行、TTFT 或功耗時必須補相應量測；未測欄位不得估成已驗證。Mac Studio 128G 仍是候選配置。 |
| **宣稱 local_only／外送隔離：G-01** | 固定 engine、代理、驗收器、工具與憑證的可信位置，量測 egress，涵蓋失敗／分類／修正；本地 loopback 不能證明上游沒外送。 | **P1、隱私承諾前**。政策檢查已完成。現有路由若本地首答失敗後要用雲端 Jev，不能宣稱全程 local_only；不得為完成核定而暗中放寬政策。 |

上述都是條件式**證據義務**，不是新增功能授權；本輪不執行真實硬體、服務啟動、故障測試、清除或付費驗證。

## 選配／後續擴充（不列為首版未完成項目）

這些項目不阻擋目前已完成子集的實作／離線完成狀態；若正式首版範圍另外承諾它們，必須重新納入適用資格與實作計畫。

| 擴充 | 現況／何時才需要 |
|---|---|
| 更多評分來源與格式 | CodexRadar 遠端自動抓取、LiveBench v2，以及未選用的 AA／OpenRouter 通道，不必為採用一個已支援來源的首版全部補完。CodexRadar 可維持經審閱受控匯入。 |
| 自動同步與通知 | 更新 CLI、快照 TTL 與 audit 已完成；背景排程／通知未啟用。首版可用具名人工更新流程並驗證過期阻擋；若承諾自動更新，才補排程、重疊／故障與通知證據。 |
| 額外客戶端／協定 | Async／JS／Open WebUI／OpenClaw、完整 Ollama、多輪、串流、模型工具呼叫與多模態，不混入既有單輪文字 SDK 的缺口。實際選用時另行實作／驗證。 |
| 自動事故恢復與多主機 | 未提供自動 lease 到期／事故重送與多主機 scheduler；目前受控 worker／人工恢復模式可準備自己的資格證據，不能宣稱具備未支援能力。 |
| 額外實驗對照組 | open_source_fixed_low 為 plan-only executor；不是現有四組的實作欠項，也不是取得 Jev 單獨增益的既成證據。新增實驗另有規格、驗收與預算。 |
| 未主張的硬體／量測能力 | 未使用的模型引擎、其他硬體、切換／並行／first-token／功耗擴充，不要求首版一次完成全部矩陣；已承諾的本地功能仍依 RG-06 補證據，不能移到本列免驗。 |
| 額外身份系統 | OIDC／SSO、管理 UI／帳號 HTTP API 不在已完成 Token 身份子集；不要求為該子集的首版資格先實作它們。 |

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

## RG-01 新版落實進度（2026-10-19）

上述委託決策後續已接入新版本的有界 SQL 政策、13 個公開合成反例及嚴格 Linux launcher，見[本批交付與取證邊界](rg01-implementation-20261019.md)。原 SQL v2 與歷史結果保留；本批完整驗證依新的 evidence／收據。Linux 目標攻擊取證及正式具名審閱仍 blocked，其他 gate 不受本批改寫。這是新政策後續實作狀態，不回寫先前決策當時「尚未實作」的歷史紀錄。

Scout 專用環境其後已依另行授權完成準備，且通過真實嚴格 worker 的最小 SQL
啟動／驗收／回收，見[準備證據與操作邊界](rg01-scout-environment-20261019.md)。
目前不缺 Linux 主機或 sudo 準備，仍缺 RG01-06 資源／攻擊／中斷取證與 RG01-08
具名正式核定；環境準備完成不提升本表的正式資格狀態。

後續「下一步」已完成 [RG01-06 限定範圍實環境取證](rg01-scout-target-20261019.md)：
正常流程 8/8、七項取證 7/7；真 SQL worker 中斷後重用保存答案、Mock POST 僅一次。
以上舊段落的 RG01-06 缺證已解除，**目前 RG-01 剩餘的是 RG01-08 正式具名審閱**。
前六項探針不是全面 escape 測試，active reservation 釋放不等於 provisional 費用
已核帳。正式 registry 保持 pending，沒有由技術取證自動升格。
