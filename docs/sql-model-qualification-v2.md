# 排行榜分群參考＋獨立 SQL 模型資格 v2

2026-10-20 使用者已授權修訂規則及離線準備，未授權任何 API、供應商聯絡、部署或推送。目標是讓實際 API 設定用自己的 SQL 測試取得資格，不再要求榜單提供它本來沒有的日期版對照。規則／工具已實作與離線驗證，不表示四個真實模型已合格。

## 新舊規則分開

| 證據 | v1（保留不改） | v2（本次明示新增） |
|---|---|---|
| 排行榜 | 精確對照測量設定／評測日期 | 同一來源、版本、原生尺度的家族／強度分群參考；不宣稱 API 日期版等效 |
| 榜單未知評測日期 | 原政策要求時仍阻擋 | 原值保持 null；不作 SQL 能力資格的日期。取得時效仍檢查 |
| SQL 能力 | 原能力證據契約 | 固定 API 版本／強度／供應商的獨立 50 題資格；有自己的測試日期及到期時間 |
| 公開卡 | 舊 schema 的投影 | 明示 family_effort_prior；合格時只提供操作範圍／資格摘要，不投影逐題答案或保留題判分 |
| 核定／費用 | 原 input-bound review 與費用檢查 | 保留，沒有豁免正式核定、RG-04 或 unknown stop |

根領域決策見 `CloudyFan/docs/adr/0007-tier-prior-independent-sql-qualification.md`；新 profile schema 為 `cloudyfan-first-pool-profile-v2`，policy 明示 `score_role: family_effort_prior`。v1 不能夾帶此模式，舊快照／資料包不重新命名或覆寫。來源／catalog 仍最長48小時、snapshot24小時；原80／100邊界不變。v2 的 `max_evaluation_age_days: null` 是對榜單證據角色的明示修訂，不是把未知日期改成今天。

## 固定版本表與目前狀態

[版本表](../examples/sql-model-versions-v1.json)與[待驗候選包](../examples/first-pool-profile-sql-v2.json)另存。低／中／高仍是 GPT-6 Sol 20260922／low、GPT-6.1 Sol 20260929／high、GPT-6 Astra 20260903／xhigh，指定 OpenAI；開源仍 GPT-OSS-120B／low／baseten/fp4。閉源只允許日期版回傳，不因 family alias 相同就視為精確版本已確認。

API 端點固定 Chat Completions、供應商 only、allow_fallbacks=false、require_parameters=true，無工具、外掛或串流。Jev 分類設定保留但不參與本批模型資格試驗。所有真實候選 approved=false，api_identity=pending，sql_report 空，review 空；prepare-first-pool 預期 blocked。

開源資格只適用所記錄的 API 模型／供應商／fp4 服務設定及測試時段；無法見到的底層權重 revision／engine 仍未知，不稱不可變權重或本地 Mac Studio 資格。版本表的 context 是公開宣告容量，測試只證明這批有限大小工作負載；不因此宣稱已實測8K／1M完整上下文。

## 全新50題與判分

- 六類 SQL 任務：filter8、join8、aggregate8、window8、set8、bounded correlated/resource10；每題皆受既有執行資源限制。easy15／medium20／hard15 是事前工作量分層，尚非模型實測難度。
- 每題兩個公開案例、四個獨立生成的保留案例。固定種子涵蓋空表、NULL、重複值、孤兒關聯、沒有子資料的父列；Python 規則產生期望值，另一份 SQL 控制解經既有 SQLite worker 核對。題庫沒有讀正式50題答案來設計規則。
- 50題是六個有限題型家族的新任務實例，不是50個互不相關領域；每題多個測資也不當作獨立模型樣本。部分資源題共享語意、資料實例不同。不能據此推論所有SQL的可靠性或商業降本。
- 模型只取得公開 task，沒有工具讀取考官資料。修正只用凍結 SQL 公開診斷；公開通過後不因保留題失敗再補答。最多兩次同模型生成，無升級、無自動網路重試。沒有重審S01～S08或修改RG-01任何模組／資料。
- 資格門檻事前固定：50題最後一次答案皆通過公開＋保留判分，並記錄首答通過；六個操作的所有指定案例均須有通過證據。失敗不挑掉、不調門檻、不換模型湊池。任一層缺合格者不啟用完整三池。
- 工程中斷、身份不符、缺資料及未知費用另列，通過欄為null而非答錯。已派送的所有已知費用仍計入。全部有完整證據前不給資格。
- 資格從本批最早一次實際請求起算30天。suite／grader／實際SQL執行契約、API版本／強度／供應商與輸入證據皆綁定；變更即重新判分／核定。不因榜單重抓而延長SQL資格；快照效期取所有來源／catalog／SQL資格／snapshot時效中最早者。

grade_model 的 known_api_usd 只加總該角色50題紀錄中的首答／修正；四次身份確認費用須由原始Client收據另列並加入整批總帳，不能因不在50題報告內而遺漏。

此批100%門檻只表示「這50題在有限兩次流程通過」，不是零錯誤率保證，也不是四組路由成效或Jev增益實驗。API 能力合格與正式 gate 核定仍分開。

## 離線工具與原始資料隔離

```sh
# 只建立離線材料；目錄必須不存在且在 ignored runtime 之內
.venv/bin/python scripts/prepare_sql_model_exam.py \
  --output runtime/sql-model-exam-NEW --now <含時區的實際UTC時間>

# 使用既有first-pool CLI檢查新版待驗包；預期exit1/blocked
.venv/bin/cloudyfan-scores prepare-first-pool \
  --profile examples/first-pool-profile-sql-v2.json \
  --output-dir runtime/sql-v2-review-NEW

# 日後只讀已保存的標準化請求／答案，不發送API
.venv/bin/python scripts/grade_sql_model_exam.py \
  --records runtime/EXAM/normalized-low.json --role low \
  --output runtime/EXAM/graded-low.json --now <實際UTC時間>
```

準備工具輸出 public-tasks、assessor-only、suite、versions、api-test-plan、pending-pool-report、preparation。資料目錄0700、檔案0600，拒絕覆寫／寫到追蹤文件區；考官材料／真實答案／原始收據不提交Git。公開task只含原本可公開的範例答案，不含控制SQL或保留案例。

`captured_attempt(entry, case, intent, record)`只讀既有 Client intent／response及已存在核帳；核對URL／payload雜湊，再沿用實付usage解析，不執行GET或POST。generation_total_cost取代response_usage_cost、不重複相加，estimated_cost保持未知。`grade_model`要求完整50題、不重複request ID、精確回傳model／provider、實際payload強度／fallback限制、答案等於保存response、正確公開契約及實際請求日期；輸出摘要另綁完整原始紀錄digest。

準備工具與判分工具不包含付費派送命令。本輪備妥的是離線輸入／考官流程與實施計畫；正式執行時仍須接既有Client／預算及保存答案的受控runner，先檢查授權與預留。資料與審閱是可信操作者輸入，hash不是供應商簽章，也不能驗證人是否說真話。合成測試標記synthetic，正式入口拒絕拿合成報告替代真實證據。

## 真實API測試計畫與預算提案（未授權執行）

2026-10-20 後續指示：先暫停全大模型獨立對照組，集中地端先答、便宜先答與CloudyFan三流程。下列四模型資格考試與US$60保留作未執行提案，不是三流程的新預算或執行授權；三流程的條件估算與仍需分開計價的資格工作見[新預算說明](three-arm-budget-20261020.md)。

1. 唯讀預檢：固定本批所有材料／程式／SQL契約與版本表；核對可用帳務、未決費用、目前模型／價格及來源使用／時效。需要新來源GET時另依當時授權執行，本輪沒有查價。缺可執行的費用限制／明確風險授權時先回報，不能用估價填RG-04。
2. 身份確認：四個生成設定各一次，最多4次，不自動重試。檢查日期版／供應商／強度參數與可追溯用量／費用。alias不符、429、未知費用或中斷先保留並停止；不無聲放寬限制或換模型。
3. 正式資格：固定Q001→Q050，每題四個設定輪替先後順序；200次首答，最多200次公開失敗後同模型修正。每次最多4096輸出token、上下文32768bytes。無Jev分類（0次）、無網路重試。前一呼叫費用／意圖未解決前停止新POST；修正亦須先有預留。
4. 完整收集／封存後才做保留案例判分，報首答／最後通過、模型錯誤／工程／未知、全部API成本；API耗時取原始Client收據另附。離線grade_model本身只彙總SQL／費用，不把HTTP耗時當成端到端服務延遲。原答案符合相同題目、設定及契約才可重用，不重送已付費請求。日後若調能力規則，須新批資格，不能用這批答案選最好看的規則。

以下僅是**舊價格下的token費情境**，並非實付、目前報價或可強制最壞上界。輸入以首答8192、修正16384token作預留試算，輸出各4096；這些輸入數不是tokenizer或供應商的保證。每角色另含一次1024輸入／128輸出的身分確認。

| 角色 | 50次首答 | 最多50次修正 | 身份確認 | token費小計USD |
|---|---:|---:|---:|---:|
| 開源 | 0.143360 | 0.184320 | 0.0001664 | 0.3278464 |
| 低 | 2.867200 | 3.686400 | 0.003328 | 6.556928 |
| 中 | 2.867200 | 3.686400 | 0.003328 | 6.556928 |
| 高 | 14.336000 | 18.432000 | 0.016640 | 32.784640 |
| **合計** | | | | **46.2263424** |

加25%預備為US$57.782928，建議日後另授權**US$60**。最多404次生成；沒有分類費。這不是現在准許花60美元，也不是模型修正率／實際費用預測。快取寫入、內部重試／失敗／取消與另列費目仍有未知保障；實際核帳所有費用照計，不能當零或藏在預備金內。若當時無法建立下一完整題塊的可信預留與可執行限制，或價格／用量情境失效，先提出修訂預算，不超支也不假裝已有供應商硬上限。

首版來源使用／更新責任及input-bound review、RG-04完整費用保障與其他適用gate仍需各自補證；本輪不代簽或啟用正式current。

## 模型資格執行器接入（2026-10-20）

後續已接上[獨立資格執行器](sql-model-runner.md)：既有Client、共享預算、身分確認、固定50題公開補答、保存答案與後置保留判分。上文「仍須接受控runner」是準備階段狀態；本輪完成的是可注入Client的執行接入與mock-only CLI，不是已授權真實派送。完整全批預留404次上界，兩次同模型上限與資格門檻不變。原capture normalizer的JSON雜湊編碼已改為真實Client格式，原收據與失敗保留。真實API身分／SQL成績、來源使用／人員審閱及費用保障仍待核定；不啟用正式current。
