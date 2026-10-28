# 建立時任務特徵

這份觀測保存任務接受當時的合法文字輸入量測與明示要求。它提供操作員查閱，不供選模器改派，也不建立任務難度、正確率或節費成效。

## 主機設定

既有任務 HTTP 仍只接受 `run_id`、`category`、`prompt`。政策內容、來源與 port 只在可信主機程式內設定；操作員可透過下列受原身份與 session 保護的控制，採用已登記版本。沒有公開擷取或 seal 端點。

`assemble_monitor` 預設安裝唯讀的安全紀錄 reader，新任務不擷取。明示啟用完整首版欄位：

```python
from cloudyfan_jev.task_features import POLICY

app = assemble_monitor(
    store, service, registry, identities,
    operator_token=operator_token,
    creation_features={**POLICY, "feature_version": "local-deterministic-v2"},
    # 其餘原有主機設定保持原值。
)
```

較低層的 `TaskService.creation_features` 配置包含 `policy_bundle`、真實 `binding_port`，可選 `compute_clock` 與嚴格布林值 `capture`。`compute_clock` 預設為 `time.monotonic`，是外部時間依賴，不是序列化政策的一部分。`capture=False` 只安裝 reader；未指定時，既有兩欄明示配置會啟用擷取。設定為 `None` 會停用新擷取，保留原 reader。重新開啟主機時，Monitor 也會重新接上原 Store reader。

主機配置先複製採用的已知政策；首次綁定把版本與摘要固定在原交易內。資料使用政策目前為 `creation-feature-data-v1`。政策、主機或版本改變都不會重算舊任務。

## 操作員採用、停用與回退

桌面「建立時觀測規則」頁面與 `GET /monitor/observation-policy` 顯示目前 revision、設定來源、是否擷取及可用版本。首次尚未由操作員設定時，`configuration_source=host` 沿用可信主機原配置；此時 `active_version=null` 不代表已停用，須看 `capture_enabled`。

| 採用版本 | 新任務特徵 | 新任務建議 |
| --- | --- | --- |
| `features-v1` | 原 `local-deterministic-v1` 三項 | 未提供 |
| `features-v2` | `local-deterministic-v2` 白名單 | 未提供 |
| `features-v2-advice-v1` | `local-deterministic-v2` 白名單 | 原 `route-recommendation-rules-v1`；缺精確校準仍為未知 |
| `null` | 明示停用新擷取 | 未提供 |

操作流程是先讀取目前狀態，在頁面確認後以 `POST /monitor/observation-policy/activate` 提交且只提交 `revision`、`version`。不能指定任意政策、檔案或校準來源。舊 revision 回覆 `409 configuration_changed_reload`，須重新讀取再作決定；不自動重送覆蓋另一位操作員的選擇。無效、撤銷、客戶及只有任務讀取權限的憑證不能操作此控制。

新任務在原 binding 交易內採用持久版本與指紋，並將同一份 prepared recorder 固定到原接受／建立範圍。即使另一個操作員在建立途中停用，已接受任務仍完成其原一次擷取；相同任務重送、接續、GET 與重開都不取得補算資格。已發布的 recorder 不再被另一版本清空或改寫。版本與建議資料只在主機啟動準備，控制與輪詢不重讀校準來源。

維運回退使用同一控制：重新讀取 revision，再明示選擇 `features-v1`／`features-v2`，或以 `null` 停用。這只改後續新 binding，保留既有特徵、建議、規則版本、原費用及存取期限。重開主機會還原持久選擇，同時接回只讀 reader；停用不隱藏舊紀錄。這不是原路由、模型或費率的回退入口。

控制列與初始化標記不一致、版本／指紋錯誤或資料庫不可讀時，控制回覆固定 `503 observation_policy_unavailable`。新合法任務仍走原接受與派送，輔助觀測明示不可確認，不退回舊主機配置重新啟用擷取。保留失敗證據後由原資料庫維運程序處理，不以目前規則重算舊任務；這不宣稱偵測整份資料庫及所有對照同時回滾。

明示停用的新 binding 保存獨立且有摘要對照的「未擷取」標記，讀取才可明確回覆 `not_provided`。摘要在同一原交易內使用 SQLite 實際保存的 binding 表示，支援合法整數／浮點時鐘而不改原建立時間。缺紀錄、錯摘要或不完整 seal 仍是 `unavailable`。未提供操作員 observer 的 runner 明示未提供；不遷移舊紀錄，也不從當前停用推測舊任務狀態。

## 欄位、單位與未知

HTTP envelope 為 `creation-task-features-v1`。`local-deterministic-v1` 保存原三項量測；`local-deterministic-v2` 保存下列最多十六項。reader 保留 v1 原內容，畫面明示其他欄位未提供，不補進 v2 欄位。

| 欄位 | 取得方式與界線 |
| --- | --- |
| UTF-8 bytes、Unicode codepoints | 直接量測原 prompt。codepoints 不是使用者可見字形數。 |
| input tokens | 缺少精確 tokenizer、序列化及模型版本，值為 `null`、類型為 `unknown`。 |
| canonical 類別、模式 | 原可信 binding 的明示值；候選 pool 與契約對應保留在 binding。 |
| 訊息數、參考區塊數 | 原請求沒有對應結構，維持未知，不填一輪或零文件。 |
| 必需工具能力 | 只採用原凍結 `requirements.capabilities` 的 `tools` 要求。工具允許名單本身不足以宣告必需工具。 |
| 輸出 schema | 現有受控契約未明示此要求，維持未知。提示中的 JSON 指令與公開驗收契約都不建立 schema 要求。 |
| 輸入型態、模態要求 | 接受的表示為文字；未提供的模態要求仍未知。額外模態請求欄位沿原 API 拒絕。 |
| 上下文需求 | 只讀原凍結 `context_tokens`；與實測 tokens 分開。 |
| 執行上限 | 原 `max_context_bytes`、`max_tokens`、`max_tool_calls`、`max_turns`，不改寫為任務需求。 |
| 資料位置政策 | 只公開原 `ExecutionPolicy.mode`，不公開 origin、工具位置或本地路徑。provider 允許名單不是地理位置政策。 |
| 執行期限、整體費用要求 | 未明示時維持未知。排隊等待期限、每次呼叫上界、客戶費率與實付各有原本意義。 |

每項都有 `kind`、`value`、`unit`、`source`、有界原因代碼。首版只產生有依據的明示值、確定性量測與未知，不為填滿其他類型而產生啟發式評分。原 API 不支援的輸入欄位仍回覆原安全錯誤。

## 首次建立、獨立對照與失敗

受控 Queue 的 producer 必須同時擁有本次原 Mon binding INSERT、原 accepting INSERT，以及 Core 新 job/state 寫入。binding 只有原交易成功提交後才交出首次結果；短期資格在原 submit/create 呼叫離開時重設。既有 accepting 的恢復、GET、輪詢與相同請求重送都不會得到新資格。原同步路徑仍沿原合法接收／執行流程。

Core 在原新任務鎖內寫入獨占建立錨點、raw 目錄外的安全紀錄，再完成一次 Mon adoption CAS seal 嘗試，才返回 queued／允許原付費流程。sidecar 自己的 hash 不足以當證據；讀取也要比對原 binding、adoption、請求／契約摘要及獨立 seal。有效存取期間仍需 Core 錨點。

輔助 SQLite busy、計算超時、檔案錯誤或封存失敗會顯示 `unavailable`，原合法任務照常執行。partial 檔案保留，讀取與恢復不補寫。若 SQLite 錯誤已回滾整個原 binding 交易，就不是合法提交的接受；主服務在排程前回覆 `503 monitor_store_unavailable`。沒有原綁定時不能回覆成功排入。

這是可信主機程式內的呼叫紀律與獨立持久對照，不宣稱隔離同程序惡意程式。Store 不依賴 Core 的私有 owner ContextVar，也沒有公開 seal HTTP。

## 處理與保存界線

- 原文字上限為 32768 UTF-8 bytes；安全紀錄／投影上限為 16384 bytes。
- 最多 16 項、每項最多 4 個原因、每個原因最多 80 個 ASCII 代碼字元。
- 新增的逐任務序列化、hash、白名單驗證與特徵萃取，累計純計算輸出期限為 50ms；受控時鐘回退或異常也不可產生成功特徵。SQLite 與檔案 I/O 分開；此期限不是 OS 硬即時或服務 SLO。
- 輔助 snapshot／seal 使用 `timeout=0` 的一次非等待 SQLite 交易。原 binding 的交易與鎖等待契約保持原值。
- 控制／初始化與未擷取標記查詢先核對 SQLite 型別、已知短版本及固定摘要格式，局部 native 欄位上限至多 4096 bytes，隨即還原連線原上限；損壞的大欄位不先整份載入 Python。這不改原合法 binding 的資料上限，也不是 OS I/O 耗時保證。
- 原 adoption 查詢另以 32768-byte native row 上限容納原 16384-byte adoption，先核對正文與四個摘要的 SQL 型別／長度／NULL 形狀；不把損壞摘要轉成可補簽的空 seal。
- 檔案採固定根目錄、非 symlink、single-link、有界讀取、0600 獨占寫入與 fsync；不覆寫另一份建立紀錄。
- 安全 sidecar 位於 `TaskService.root/.task-features/`，受原 raw purge 保護。首版沒有安全中繼資料刪除入口，保守保留至少 365 天及未知費用所需證據。

客戶原 30 天存取與身份／scope 不變。到期後仍回覆 410；操作員讀原安全中繼資料，不能以目前規則或重新開啟原文補算。缺少兩個錨點表示未提供或無法確認，不能從目前的 `capture=False` 推定舊任務曾停用。

一般 GET／snapshot 對到期任務保留原 Budget 金額、事件、保留款及已保存的公開 trace。未安全保存的模型／供應商／token／計費來源細節標示 `metadata_unavailable`、`original_raw_access_expired`，不為補畫面重開原始請求。`updated` 是原觀測更新時間欄位，不代表已重新驗證歷史完成證據；特徵仍使用原 `captured_at`。

安全的已執行歷史與建立時特徵分開：既有合法 `capture(persist=True)` 在原公開摘要提交後，另以一次 `timeout=0` 輔助交易保存到既有 Monitor `tasks.data` 的內部 envelope，不新增資料表。只保存本任務已驗證的原綁定／版本與每筆事件的公開模型、供應商、用量及來源白名單；金額、核帳狀態、收據、未知費與預留始終以原 Budget 為準。沒有 prompt、答案、URL、路徑、headers 或別的客戶帳務，內部 envelope 也不直接出現在客戶或操作員的公開 task dict。

只有原新建立、實際執行 finally 與 worker transition 明示開啟主機內的 `retain_metadata=True`；一般 capture 預設不取得此寫入資格。既有重送若只更新原公開摘要，也不因此替 cold legacy 回補安全 metadata。這不是 HTTP request／query 或原 frozen runner descriptor 的欄位。

每份安全紀錄至多 64 筆事件、65,536 UTF-8 bytes；token 子項限原支援欄位及 `0..10**12` 整數。原 Budget 查詢只取本任務，使用唯讀、非等待連線，另有固定 32,000 SQLite VM steps 上限；`LIMIT` 並不保證沒有掃描其他不相符的歷史列。讀取超限、不完整或驗證失敗就略過輔助保存；只有原公開摘要先提交後，才嘗試輔助寫入，寫入忙碌或交易回滾不會回滾原摘要，先前安全紀錄保留。這些界線不保證 OS I/O 的耗時上限，也不改建立特徵原 50ms 純計算界線。

輔助 writer 的 pool 讀取同樣使用非等待唯讀連線、原 pool ID／摘要核對及 1 MiB 上限；每次 capture 只沿原一次到期檢查取得的 `expires_at`，不從目前時間重設期限，也不因補 metadata 再走原可寫、等待 10 秒的 pool／expiry 讀取接線。

到期後依原 binding、account、event id、internal run 與原綁定摘要對照已存資料；完整保存者保留原公開 ledger 明細與分類，cold legacy 或損壞紀錄則明示未知。原 Monitor 綁定與 `bound_at` 仍可知；未保存的 contract／routing version 不從目前 descriptor 補造。`snapshot(persist=False)`、detached／query-only 與一般 GET 不寫入或補建這份安全紀錄，也不重新簽署建立特徵。紀錄沿 Monitor 中繼資料至少 365 天與未知費延保政策保存，沒有新增刪除入口。

既有已驗證操作員 `POST /monitor/billing/reconcile` 保留原財務追溯授權：只有該既有呼叫在主機內硬編碼開啟 `include_retained_financial_metadata=True`，沿原受控財務收據與核帳範圍補齊費用證據。HTTP body／query 不能自行打開此選項。這項明示動作與一般查閱分開；原費率、缺證停止、未知費保留及冪等扣款規則不變，不新增來源查詢或付款權限。

## 操作員與驗收範圍

任務明細的「任務建立時特徵」可展開單位、來源及未知原因；沿用原頁面的鍵盤焦點與輪詢展開狀態。未完成任務也能查閱原建立紀錄，缺紀錄顯示無法確認。畫面不顯示 prompt、答案、隱藏 grader、另一策略答案、文件名稱、私人錯誤或憑證。

同步長任務持有原執行鎖時，查詢以原只讀身份、有限時鐘與存取期限檢查核對已封存投影；不取得派送名額、不補擷取。長任務原本占用的共享名額維持，其他任務依原政策拒絕或排隊。規則頁的小型直接查詢不等待完整歷史刷新；登出、撤銷及較晚返回的舊 session 回應不恢復私有畫面。

新增驗收位於 `tests/test_task_features_http.py`、`tests/test_task_features_integrity_http.py`，使用真實 loopback HTTP、原 Core／Store／身份／Budget，以及受控 Queue／Health 主機。另用原同步四模式合成 fixture 核對既有模式與 canonical 類別的接受／未註冊界線；不宣稱未註冊類別已有模型能力。

`scripts/qa_task_features_desktop.cjs` 啟動自有 headed Chrome 1280×900 與真 HTTP fixture，保存 PNG、ARIA、原網路／console 與程序退出收據。所有模型、來源及對帳上游均為 MockTransport。

實際 on/off 比對保存原 body、headers 與 session ID，不做正規化；隔離主機的契約私人保存路徑差異另列。原路由、分類次數、公開驗收與 Budget 金額仍各依原始證據核對。Focused 測試或畫面通過不等於完整套件驗收、實際模型資格、真實節費、部署或工作單結案。


## #39 本機驗收完成 — 2026-10-28

固定Core6c01bd7e／MonitorEFE8f11b的最終47focused（新增45＋原2）與真實headedChrome156／1280x900桌機3通過；44份含pairfixture的744個HTTP／upstream參照及桌機24response參照逐筆核對無缺口。原兩測試、第一tracer整檔與QA來源未改。

修正後原Monitor完整472tests／26steps及sdist/wheel通過。原Core2069／8、兩個Mock示範與sdist/wheel只在Core429檔精確不變且無Monitor依賴的範圍沿用，不宣稱重跑新Core。兩軸獨立審查各0；正常整合Core087e29c8／Monitor20765124，原guard與quick4／24通過，597來源與已驗收候選一致。此文件收尾是Markdown-only，不是另一輪完整測試。

原Monitorfull01的2failed／460passed與未跑build、各次故障及測試作者oracle失敗完整保留。full02實際checker成功後，外層封存曾errno5／exit1，原失敗路徑及原因未知；原收據未覆寫。另存恢復索引涵蓋26212regular／291symlinkmetadata、3806對native原件／副本，Root均已獨立回讀。詳細時間與SHA在本機CloudyFan的issue39收據；不將舊失敗改標成功。

[工作單39](https://github.com/alexwang0315/cloudyfan-routing/issues/39)已結案。父規格30仍需40／41，整體20張子單目前完成6張；所有模型／供應商上游為MockTransport，沒有來源推送、部署、真實支出或節費／容量／SLO資格主張。

## #41 驗收狀態

新增真 HTTP 案例位於 `tests/test_task_observation_policy_http.py`；桌面流程為 `scripts/qa_observation_policy_desktop.cjs`。完整 Core integration、Monitor check／打包、真 headed 桌面及最終獨立審查尚未收齊，工作單仍在實作。局部 RED／GREEN 與舊版本完整收據各保留其原始範圍，不能代替目前來源的完整驗收。
