# RG01-08 正式審閱清單（未核定）

本清單是 RG01-06 技術取證完成後的審查入口，**不是簽核紀錄**。已決定的首版
語義與範圍不需重新訪談；reviewer 要確認實作、預期答案、證據與已採用決策一致，
記錄是否接受其有限保障及剩餘風險。正式 registry 仍維持 pending。

| 審閱項目 | 需要確認與留下的結論 | 材料 |
| --- | --- | --- |
| 有限任務與數值域 | S01～S08 唯讀 SQLite、NULL／排序／同額／無主引用語義；amount 絕對值至多 10^9、每表每 fixture 至多 1000 列；超域及超輸出限制在派送前拒絕。不得擴大成任意 SQL 或真實客戶資料庫資格。 | [已採用決策](rg01-first-scope-decisions-20261019.md)、[實作包](rg01-implementation-20261019.md) |
| 預期答案與反例 | 核對獨立計算／Python oracle／參考 SQL；新版 53 組公開 case/sample、14 個原 mutants 與 13 個新增已知錯誤的覆蓋。確認有限公開樣本仍可能被特製答案通過，不能聲稱未知誤收率為零。 | [語義審閱](rg01-semantic-review-20261019.md)、[有界覆蓋](evidence/rg01-bounded-catalog.json) |
| 固定建置與隔離 | 核對 Scout ARM64、kernel／Python／SQLite、bwrap／rootfs SHA256、命令與有效 policy。接受的範圍須區分前六項受信任探針與最後的真 SQL worker；未另測 fork exhaustion、不是完整漏洞／escape 認證。 | [實環境取證](rg01-scout-target-20261019.md)、[原始收據索引](evidence/rg01-linux-target-runtime.json) |
| 錯誤、修正與續跑 | 公開診斷不得洩漏隱藏答案；驗收中斷不是模型答錯，應保留答案與預算，不自動重送。核對真 worker 超時後同答案恢復、response hash 不變、僅一次 Mock POST；active reservation 釋放仍保留 0.02 合成 provisional hold，不是真實核帳。 | [SQL 契約](sql-environment.md)、[取證限制](rg01-scout-target-20261019.md)、[離線驗證](evidence/rg01-target-offline-20261019.json) |
| 責任、有效期間與撤回 | 指定實際具名 owner／reviewer、scope、證據及 hash、審閱結果、核定／到期時間與授權引用；確認誰負責停用／撤回。kernel、runtime、artifact、契約、域或限制改變時重新審查；重開機後需重新掛載及核對，每次 scope 使用新的 cgroup identity，不沿用過期路徑。 | [環境準備](rg01-scout-environment-20261019.md)、[registry 規則](release-readiness.md) |

審閱時必須保留初版 cgroup／loader 失敗、第一次 mock identity 中斷與未執行項，
以及後續修正證據；不能只挑最後成功的一輪。上述 53 組是公開回歸材料，新增反例
屬事後修正，不能當成盲測或真實模型品質結果。

目前尚缺的是實際具名正式審查與核定紀錄；AI 技術複查不代簽。核定若有條件或
拒絕，應寫明條件／缺口；未完成前不改為 qualified。完成 RG01-08 也只解除此有限
範圍的 RG-01 資格問題，不自動核定 RG-02 模型、RG-04 費用、RG-05 部署身份或整體上線。

本次提交僅保存本清單與既有證據，不新增付費、供應商聯絡、部署或推送授權。
