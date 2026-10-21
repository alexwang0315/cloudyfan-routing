# SQL主備候選與既有證據對照

2026-10-21。本輪為離線準備，不呼叫模型、不更新來源、不改RG-01、舊資格包、原始答案或正式資格。適用`artificial-analysis:coding_index`作分群參考的有限SQLite唯讀任務，不能代表其餘22類或所有coding任務。

## 目前交付

新增純函式`prepare_sql_backup_candidates`與離線CLI，從既有廣泛盤點、SQL專用低池提案及可選的原研究準備包，產生[機器可讀對照表](../examples/sql-backup-candidate-review-v1.json)。輸出不含正式題答案，不能直接當成模型派送或生產資格。

對照12位置共有12筆準備記錄：**5個精確請求設定已可完整對照、3個有舊SQL研究參考、僅3個符合現行資格考試的單一回應身分格式**。這三種數量不同；所有實際dispatch及production資格仍是0。

| 池／位置 | 候選／供應商 | 精確設定與舊證據 |
|---|---|---|
| 開源主選 | Qwen3.8-27B medium／Near AI fp8 | 從原包核對完整profile、config hash與研究報告ID；保留兩個原回應alias，不能直接進單一回應身分考試 |
| 開源備一 | Lightning `:free`／Nvidia nvfp4 | 版本、強度、端點／回應身分仍缺；不與低池provider_default提案視為同一精確設定 |
| 開源備二 | LFM2.5-2.6B `:free`／Liquid fp8 | 版本、強度、端點／回應身分與SQL證據仍缺 |
| 低主選 | Llama3.1-8B／DeepInfra fp8 | 明示`none`＋`omit`、單一回應身分；完整設定但沒有SQL能力證據或費用保障 |
| 低備一 | Gemma3-4B／Parasail bf16 | 明示`none`＋`omit`、單一回應身分；完整設定但沒有SQL能力證據或費用保障 |
| 低備二 | Lightning `:free`／Nvidia nvfp4 | 原提案為`provider_default`且要求省略reasoning，不能自改為`none`；現有wire契約不接受這個組合 |
| 中主選 | GPT-OSS-120B high／Near AI fp4 | 從原包核對完整profile、config hash與研究報告ID；單一回應身分，但不因此取得新考試或新備援資格 |
| 中備一 | North-Mini-Code `:free`／Cohere | 版本、強度、量化、端點／回應身分與SQL證據仍缺 |
| 中備二 | Gemma4-26B-A4B `:free`／Google AI Studio | 版本、強度、量化、端點／回應身分與SQL證據仍缺 |
| 高主選 | GLM5.3-Flash low／Wafer nvfp4 | 從原包核對完整profile、config hash與研究報告ID；保留兩個原回應alias。舊研究未全對，不代改成全對或正式核定 |
| 高備一 | DeepSeek-V4-Flash-0731／Relace fp4 | 實際版本／強度／端點／回應身分與SQL證據仍缺 |
| 高備二 | DeepSeek-V4-Pro-0813／Baidu fp8 | 實際版本／強度／端點／回應身分與SQL證據仍缺 |

完整API request route保留在JSON中，包含`:free`，不把catalog canonical自動當成實際請求版本。雲端開源不是Mac Studio本地量測；零報價不是完整零費率保證。

## 兩版低池怎麼處理

[廣泛具名盤點](category-backup-coverage-20261021.md)的低池原序是Lightning `:free`、LFM `:free`、Nemotron3-Nano-Omni `:free`，依歷史報價排列；另一份既有SQL專用配置為Llama、Gemma、Lightning。新版準備表**採SQL專用配置作待驗提案**，理由是它明列SQL需求、供應商與請求模板，並非根據正式50題答案挑選。

兩版原序及未採用理由都保留在JSON，不回寫來源、不更新current；廣泛版沒有因此被判定「不能用」。低池需有實際任務成本優勢，原20%只是既有同token報價情境。Gemma在輸入長、輸出短時可能比中池貴；完整修正／升級風險及Jev費仍須另校準，不能因已列低池便強迫支付分類費。

省略reasoning的程式已實作。原配置的`omit_reasoning_transport_contract_not_integrated`只保存在`historical_blockers`，不再當成現行程式缺口。Lightning的provider-default強度仍未驗證，這不是用刪掉舊blocker就能解決的問題。

## 證據如何核對與重用

1. **原包補欄位，不自填。** CLI可明示既有`prepared-category-v8.json`，先核對[原盤點JSON](evidence/category-backup-coverage-20261021.json)所記的原始SHA256；模組再查pack自雜湊、matrix ID、摘要每個非空身分欄位、原config hash、SQL admission與parent report ID。任何衝突拒絕，不把另一provider的endpoint補進來。
2. **保留舊身分邊界。** 原Qwen／GLM有兩個accepted response alias，完整profile照留。若未來縮成`[model_version]`，必須明示新設定、新hash、新考試，不能仍稱原來那份資格。`exam_compatibility`只比較請求身分格式，從不授予考試或派送權。
3. **能力參考不等於自動重用。** 舊研究的config、report、suite、identity-contract與效期列作追溯。原包沒有明示的grader／公開契約hash仍為null，需對照實際證據；效期過了就列過期，不改測試時間。可選metadata index還會核對精確profile/hash、SQL domain及suite／grader／公開契約／artifact binding，匹配也只稱「仍需契約審閱」，不核定。
4. **舊整流程資料不能搬用。** 新主備池、全域次數與故障切換改了完整流程身分，因此cost/speed、Jev配對及前次實際設定的條件校準都仍缺。端點速度不當整題延遲，舊單例forecast不當新版備援forecast。
5. **不完整設定不去重。** 已完整的同一精確設定若佔多池只留一份準備設定及多個slot引用；provider、版本、強度、量化、accepted aliases或reasoning mode不同就不同。缺欄位的同名提案保持獨立，避免共用不適用的能力證據。

## 離線使用

```sh
.venv/bin/python scripts/prepare_sql_backup_candidates.py \
  --coverage docs/evidence/category-backup-coverage-20261021.json \
  --sql-low-proposal examples/sql-low-pool-candidates-v1.json \
  --archived-pack ../artifacts/category-four-pool50-20261021/live-execution-v7/research-v8/prepared-category-v8.json \
  --now 2026-10-21T12:00:00Z \
  --output runtime/sql-candidate-review-NEW.json
```

`--now`必須是明示且帶時區的檢查時間；例中的時間不是刷新來源的授權。SQL低池原提案與原研究包屬既有受控工作區，不隨本次套件交付上傳；輸入路徑必須明示，CLI不搜尋憑證、答案或收據。缺原包時可省略`--archived-pack`，輸出會誠實保留摘要缺欄位，只能對照Llama／Gemma兩個完整設定。既有output不覆寫，應使用新的輸出路徑。

## 已測與仍待完成

專屬離線回歸涵蓋來源不變、12位置、兩版低池、完整設定跨池去重、不同provider／強度／版本／量化／wire不共用、未知提案分離、metadata錯配／缺失／過期、保留free route、原包hash／身分衝突、alias邊界、私有欄位不投影及CLI不覆寫；模型API與來源GET皆0。實際測試數與整合範圍由本輪驗證紀錄另記，不將此表當真實模型成績。

尚未補齊候選能力、當前健康、完整費用保障與新版整流程／Jev校準；沒有更換正式資格門檻或啟用模型池。下一步與[SQL主備包落地清單](sql-backup-pool-readiness-plan-20261021.md)及多候選資格執行器銜接。
