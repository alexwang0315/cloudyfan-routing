# 每類主選＋兩備援：離線實作與使用邊界

2026-10-21。目的：在呼叫前避開已知故障或過慢端點；明確無答案且費用已知時，在同一品質池內換模型；答錯則繼續原本的品質補救。健康資料不判定答案正確。

## 實作與真實資料分開看

新版 `BackupCategoryCore` 明示選用 `qcs-backup-policy-v1`，舊 `Core`、`CategoryCore` 與已凍結研究不自動切換。支援23類各自開源／低／中／高四池，目標每池三個不同模型；不足時回報不足，不借用其他類別或把同模型三個供應商湊成三個模型。來源與適用能力不足的候選不能進可派送清單。

同一模型的不同端點不算三個模型；不同供應商也不證明實體故障獨立。全部經OpenRouter仍共享入口，不能聲稱可避開整個OpenRouter故障。現有本地接入只支援已註冊且設定完全匹配的LocalBackend；未註冊的其他本地模型會被排除，不假設已有三台主機。具名表中的雲端開源提案也不等於Mac Studio 128GB可執行清單；沒有固定量化與本機容量證據，不能列成本地合格候選。

[92池具名表](category-backup-coverage-20261021.md)列出全部276個位置，包含有限SQL研究准入、待驗提案、過期來源及缺額。[JSON](evidence/category-backup-coverage-20261021.json)保留來源列、模型／供應商、報價與時效。原3個SQL研究設定不能直接拿舊單例校準啟用新備援版本；正式資格維持待核定。

## 已實作的行為

- 候選準備：`prepare_backup_pools`依各類品質、能力、供應商、上下文及獨立完整成本／速度資料排除候選。主選依完整成本上界、整件流程速度及穩定ID排序；備援優先明示不同供應商／家族，再按同一成本速度排序。
- 健康資料：`normalize_provider_health`保留status與5分鐘／30分鐘／1日可用率；端點p50／p75／p90／p99延遲保持毫秒，吞吐保持tokens/s。沒有p95便不補造；端點速度不當成整題耗時。
- `HealthStore`保存不可變快照與事件，獨立於凍結候選清單更新。明示TTL、可用率窗口、速度門檻及冷卻政策；未知、過期或degraded不當成健康。連續故障開啟冷卻；等待結束仍需以更新且合格的觀測明示恢復，不自動付費探測。
- 池內不另付分類費；Jev仍是低／中及必要時中／高兩階段。第二次使用最近一次實際模型設定、答案與公開診斷。沒有對應的獨立分類校準或淨節省不足時直接選完整成本最低的合格候選，紀錄略過原因。
- 明確無答案、身分可信且費用已知的429／5xx可同池切換；答錯或無法驗收走品質補救。費用未知、請求逾時或取消狀態不明時停止，不推論零費用，也不立即競速另發一筆。
- **CloudyFan全流程最多3次生成POST，對照最多2次，分類最多2次。** 工具往返中的每個生成POST也消耗總額；不是每池各3次。此明示新版的全域POST計數與舊版本的每次作答內多turn設定分開保留，不沿舊manifest改規則。
- `BudgetedCore`先驗證所有可達模型的可強制單筆上界，再以最高可達費用乘全域次數保守預留，另加可達分類及全域工具費。回報模型收費見`costs`，含外部工具的受控完整實付見`budget.run_total_usd`，不可把兩者相加重複計費。預測、token報價與`cost_tier`不當硬上限。沒有實際強制保障時仍拒絕受控付費派送。
- 保存每次實際設定、健康快照、選擇原因、請求及收據；同池換模型後，核帳使用當次實際profile，不沿用原主選身分。原始期限不因續跑而重設；已有收據離線回放，不重送付費請求。
- 非推理模型必須明示`effort: none`、`reasoning_parameter: omit`才省略欄位；不把不支援參數的候選直接送出。`:free`保留完整路徑，僅在政策明示允許且任務為公開／合成資料時可考慮，不當本地模型或完整免費承諾。

## 接入順序

1. 由呼叫端明示23類中的一類與公開需求；不以隱藏答案分類。
2. 以`prepare_backup_pools(matrix, category, requirements, evidence, policy, now=...)`準備凍結清單，檢查`coverage`、`shortages`與`excluded`。CLI為`scripts/prepare_category_backups.py`，只準備、報告，不批准或派送。
3. 建立`HealthStore(root, policy=..., clock=...)`並匯入可追溯的當前觀測。沒有自動排程或背景免費GET掃描；資料更新由呼叫端負責，缺值會被拒絕。
4. 新政策必須使用`qcs-backup-policy-v1`，其`health_policy`與`HealthStore.policy`正規化結果完全相同，並綁定實際工具、驗收、限制及資料政策。
5. 提供獨立的整條備援流程耗時資料，`complete_policy_flow`使用`whole_task_flow_ms`及明示統計量；以`backup_flow_contract_hash`綁定整池清單、故障／次數限制及政策。沒有實際資料就維持未準備，不拿合成測試生成真實校準。
6. 若要付費Jev，每個可用候選配對及前次實際設定必須有對應`stages[stage].variants[contract_hash]`：含直接／分類後續的完整成本、分類費、有效機率區間、耗時與同一獨立人口來源。使用`backup_classifier_contract_hash`及`backup_pool_contract_hash`綁定。不同模型備援、量化或推理參數不能偷用舊單例資料。缺少則不呼叫Jev。
7. 建立`BackupCategoryCore(client, plan, decision, run_id=..., health_store=..., clock=..., limits=...)`，以`BudgetedCore`注入已確認可強制的費用保障，然後才進入`run`。本輪沒有任何真實新設定完成此資格流程。

政策預測有有效期，原始期限與資格時效分開；已付費結果可以離線重播，新派送仍須符合當前資格、校準及健康條件。每筆保存選擇必須與凍結清單及經濟規則一致，不能以改寫續跑檔案新增模型。

## 離線重現與證據

```sh
uv run --locked --offline pytest -q tests/test_backup_health.py tests/test_backup_pools.py tests/test_backup_runtime.py tests/test_backup_budget.py tests/test_generation_request.py
uv run --locked --offline python scripts/demo_category_backups.py
```

示範使用`example/`虛構模型與`httpx.MockTransport`；同一加法任務只驗證接線、切換與回放，不能證明23種真實任務能力。23類均準備四池、每池3個虛構模型，模擬主選503後備援成功；完成後以過期健康資料回讀，新增POST必須為0。合成收費不是真實費用，真實模型API費為0。

[本輪完整驗證](evidence/category-backup-offline-20261021.json)保留初次失敗、審閱重現、修正回歸及標準工作區檢查結果。保留未追蹤的`* 2.py`舊副本；其既有失敗與現行檔案測試分列，不刪除、不加全域忽略，也不宣稱完整工作區gate通過。

原始SQL驗收、已付費答案與收據、50題研究結果不變。未進行真實故障切換實驗，不能宣稱已改善真實延遲、SLA、模型成功率或降本率；未部署、推送或提交。


### 三候選備援本機提交前確認（2026-10-21）

先前1,769項與187項為原實作完成時的工作區紀錄；本次兩項P2修正後，原工作區runtime／budget73項及工作紀錄51項通過（先前全套1771項的1項依賴紀錄失敗已補正，原始診斷保留），獨立匯出的實際提交內容完整integration為1754項通過（無測試忽略），Ruff、Mock及建置通過。原舊副本與其70項失敗保留，不宣稱整個原工作區gate通過。[提交證據](evidence/category-backup-tree-precommit-20261021.json)與[工作區證據](evidence/category-backup-workspace-precommit-20261021.json)分列。正式模型與來源／費用資格仍待核定；本次只授權本機提交，沒有模型API、部署或推送。
