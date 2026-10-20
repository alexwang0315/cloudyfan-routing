# 首版評分來源與模型對照（POOL-01／02）

原 POOL-01／02 階段（2026-10-20）為本機歸檔整理，當時沒有來源 GET。後續已另完成[免費公開補證](first-pool-public-evidence-20261020.md)：保留本檔歷史輸入，新增獨立的候選包；沒有模型 POST 或供應商聯絡。

## 採用的輸入與仍缺的證據

首版候選領域沿用有界 SQLite 唯讀，公開驗收契約不變。資料起點採 **CodexRadar 模型榜的受控匯入**：既有 `hierarchical50-20261018/radar-snapshot.json` 是 2026-10-17T17:18:14.033119+00:00 的 DOM 擷取，保留 14 筆模型／強度分數；不是 account 積分，也不是目前網站狀態。歸檔 hash、擷取方式與限制保存在 [profile](../examples/first-pool-profile.json) 的 source_choice。

選擇依據是歸檔有明確強度及歷史候選家族，不是這批實驗哪個組別較便宜。LiveBench v1 歸檔雖有 1,633 筆觀測，但強度全部未知，而且歷史候選不具已證明的一對一映射；AA／OpenRouter 存取未驗，沒有為湊齊三池切換來源。

使用既有 import_codexradar 正規化為 [受控匯入檔](../examples/first-pool-codexradar-import.json)。觀測 raw_sha256 指向該正規化檔，source_choice.archive_sha256 另指原 DOM 擷取 JSON；這是兩段來源鏈，不能把正規化 hash 說成官方簽章。日期不刷新；data_until 不當每列 evaluated_at，樣本數未知不補值。

| 角色 | 固定候選設定 | 原生榜分 | 缺證 |
|---|---|---:|---|
| 開源首答 | GPT-OSS-120B／low／BaseTen FP4 歷史 profile | 未列分數，也不要求入三池 | 不可變權重／量化與設定證據、獨立 SQL 能力及當前可用性 |
| 低 | GPT-6 Sol 20260922／low／OpenAI | 35.4 | 榜單家族到日期版 API 的映射、SQL 能力、可用性 |
| 中 | GPT-6.1 Sol 20260929／high／OpenAI | 99.9 | 同上 |
| 高 | GPT-6 Astra 20260903／xhigh／OpenAI | 116.2 | 同上；歷史擷取另註明覆蓋須核對 |

日期版來自既有 experimental profiles，不是榜單已證明測量的 API 身份。能力卡逐項列 SQL filter、join、aggregate、window、set operations、resource limits 為 **unproven**；舊五類跨領域校準不能充當 SQL 能力證據。沒有讀正式題答案／隱藏成績來調卡。

## 政策與資格

- 原生邊界沿已確認規則：低 <80、中 80～100、高 >100；不是新擬合，不套用其他榜單。該 metric 對 SQL 領域的適用性仍待核定。
- 來源／catalog 最長 48 小時、snapshot 24 小時、min_samples=1、評測最長 30 天，是首版**待核定政策輸入**，不是先前已批准的營運承諾。未知樣本／日期不降低門檻放行。
- registry 的 8,192 context_tokens 是待證明的要求值，不是供應商容量觀察；所有實際候選 approved=false，catalog_checked_at 保留歷史觀察時點，不表示當時已通過 SQL 資格。
- configuration_evidence 將映射與非 holdout 能力證據綁到精確執行設定；能力證據另須覆蓋全部宣告操作與 context。這是可信管理輸入的結構檢查，無法認證聲明真實性。
- review 保持空，不建立假 reviewer。來源權限／再散布、存取、時效、更新責任、當前候選資格都未核定；prepare-first-pool 必須交付 blocked，而非正式模型池。

將來要補的是能引用的精確映射、獨立能力／時效與來源使用證據，不是重新審核 S01～S08。新輸入產生新 pack，不能把本檔日期改新便沿用核定。

## 後續公開補證

[2026-10-20 補證](first-pool-public-evidence-20261020.md)已確認公開端點的日期版標籤、指定 effort／供應商及宣告 context，並取得同三個固定候選的新 64/64 題覆蓋分數。新包為 [first-pool-profile-public-20261020.json](../examples/first-pool-profile-public-20261020.json)，舊包／時間／14 筆分數不變。受測日期版與 API 的身份鏈、六項 SQL 操作、完整評測日期與來源使用／owner／review 仍缺證；兩包均 blocked。不是把本檔的缺證全部改成已核定。

## v2的後續資格依據

使用者已明示修訂：榜單僅作家族／強度的分群參考，固定API設定的可用性改由[獨立SQL測試](sql-model-qualification-v2.md)決定。此前「需榜單提供日期版／全體評測日期」是v1的歷史阻擋，不再當作v2的API能力證據要求；未知值仍不補造。真實API身份、SQL結果、來源使用／owner／review及費用保障仍未完成。
