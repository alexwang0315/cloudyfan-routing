# 建立時任務特徵

建立時任務特徵保存接受當時合法可知的描述，只供觀察。它不參與選模，不新增模型、embedding、工具、來源或核帳請求，也不建立任務難度、正確率或實際節費結論。

## 單位與來源

受理文字的 UTF-8 位元組數、Unicode 碼點數與 token 數不同；碼點也不一定等於畫面上的一個字形。沒有綁定 tokenizer、序列化及模型版本的方法時，token 保持未知。原請求沒有訊息／參考區塊結構，不能填成一輪訊息或零份文件。

原類別、模式、候選pool與契約來自原綁定。工具需求只依真正的受控需求，允許工具清單不是需求，公開驗收契約也不是輸出schema。上下文需求、原運行上限、估值及實測各有單位與用途；供應商允許清單不是地理位置政策，排隊等待期限不是執行期限，單次費用上界、共享預算或客戶費率不是全任務費用要求。提示中的要求不會自行創造上述權限。

原答案、隱藏grader、另一策略答案、最終費用、上游實際用量及後來健康資料不成為建立時特徵。首版不為填滿類型而做關鍵字難度或品質推論。

## 主機配置與原請求

Core任務請求仍為 `run_id`、`contract_id`、`prompt`；Monitor入口仍為 `run_id`、`category`、`prompt`。新設定只供可信主機程式使用，不是HTTP請求欄位，也沒有公開擷取或seal端點。

`TaskService.creation_features` 接受 `policy_bundle` 與真實 `binding_port`，可選 `compute_clock` 及嚴格布林值 `capture`。port只有取得原採用對照與一次seal的主機metadata能力，不持有模型／工具執行或來源刷新能力；Core不import Monitor。

只有接上真正首次binding與原acceptance流程的受控主機能啟用。Monitor `assemble_monitor` 已提供該接線，預設只安裝reader；明示政策才為新任務擷取。例如：

```python
from cloudyfan_jev.task_features import POLICY

app = assemble_monitor(
    store, service, registry, identities,
    operator_token=operator_token,
    creation_features={**POLICY, "feature_version": "local-deterministic-v2"},
)
```

`capture=False` 只安裝reader；未指定時，既有兩欄明示配置會啟用擷取。設定為 `None` 會停用新擷取而保留已接上的reader。重新開啟Monitor主機也會接回原Store reader。port與計算時鐘不進序列化政策或客戶JSON；`compute_clock` 預設為 `time.monotonic`。

## 一次建立與獨立對照

原Monitor binding交易確認本次是否真正首次建立，提交成功後才交出首次結果。Core必須在原job/state真正新建的分支擷取；受控Queue還須本次原accepting INSERT的資格。相同任務的查詢、重送、接續及恢復不能重新取得資格，也不能用先查不存在或目前仍是accepting代替真正首次建立。

原job/state成立後、原任務鎖釋放及公布queued／付費執行前，嘗試保存Core建立錨點、原文目錄外的安全紀錄與Monitor原採用紀錄中的一次預期摘要。讀取核對原binding、政策、請求、registered及record摘要；有效存取期間還需原Core錨點。紀錄自己的hash不構成充分對照。

SQLite busy、計算逾時、部分寫入或seal失敗令特徵不可確認，原合法任務照常執行。讀取與恢復不補檔、不補簽、不改原紀錄。若SQLite錯誤已回滾原binding外層交易，原任務尚未合法接受，則在排程前回覆儲存失敗，不能回成功再留下未綁定工作。

這不保證跨庫全成全敗，也不隔離同程序惡意程式或偵測所有原資料與對照同時回滾。缺少兩份錨點表示未提供或無法確認，不能從現在停用推定舊任務曾停用。

## 版本、處理及保存界線

Envelope為 `creation-task-features-v1`，資料使用政策為 `creation-feature-data-v1`。`local-deterministic-v1` 保留原三項；`local-deterministic-v2` 擴充明示要求、未知及運行上限等白名單。reader保留各版本原內容，不替v1補出v2欄位；詳細欄位見 [Monitor文件](../../cloudyfan-monitor/docs/task-features.md)。

- 原prompt上限32768 UTF-8 bytes；安全紀錄／特徵投影上限16384 bytes。
- 最多16項、每項最多4原因、每個原因代碼最多80 ASCII字元。
- 新增逐任務純Python序列化、hash、白名單驗證及特徵處理累計計算輸出期限50ms。SQLite／檔案I/O、fsync或OS排程不是硬即時保證，不能據此承諾HTTP延遲或SLO。
- 輔助snapshot／seal為 `timeout=0` 的一次非等待SQLite交易；原binding的鎖等待契約維持。
- 安全紀錄位於 `TaskService.root/.task-features/`，固定根目錄、有界讀取、拒絕symlink／多重hardlink，採0600獨占寫入與fsync；受原raw purge保護。

首版不提供metadata刪除API，保守保留至少365天與未知費用所需證據。原30天客戶存取到期仍拒絕；操作員只查合法保留的安全metadata，不回掃過期原文補特徵／歷史、不延長存取或將費用清零。實際清理仍需精確清單與逐項批准。

## 一般觀測與明示財務動作

一般GET／snapshot對到期任務保留原Budget帳本事件、金額、未知費、預留及原安全公開trace；沒有安全保存的模型／供應商／token／計費來源細節明示不可確認，不為補畫面重開原請求或答案。觀測更新時間不代表重新驗證了歷史完成證據；特徵仍使用原接受時間。

既有已驗證操作員 `POST /monitor/billing/reconcile` 沿原權限使用合法延保財務收據。只有該既有主機呼叫硬編碼啟用 `include_retained_financial_metadata=True`，HTTP body／query不能打開此選項。原費率、缺證停止、未知費保留與冪等扣款維持，沒有新增來源查詢、付款或重新派送權限。

## 驗證與工作單範圍

真實loopback HTTP使用原Core、Budget、Store、身分、受控Queue／Health與MockTransport；同步四模式／類別相容案例使用自己的原fixture，不冒稱具備未接入的Health能力。實際headed桌機保存PNG、ARIA、console／network與自有程序退出紀錄，觀察不新增上游請求。

目前工作單仍須以固定版本的原始完整Coreintegration、Monitorfull／build、獨立審查及合併收據確認。[目前交接](handoff.md)記錄實際狀態；單一focused測試或畫面通過不等於完整套件驗收、供應商資格、真實節費、部署或工作單結案。
