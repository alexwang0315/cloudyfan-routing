# Issue #20 兩軸審查與處理

原 [標準審查](../runtime/final-acceptance/review-standards.md) 與 [規格審查](../runtime/final-acceptance/review-spec.md) 保留不改寫。以下摘錄是審查當時的結論；後續動作另列。

## 標準軸

> **Standards：硬性違規 0；判斷性意見 1（低，重複的桌機 fixture 控制邏輯）。**

已處理這項可選維護性意見。兩支桌機腳本改用 [共用 fixture 工具](../scripts/qa_fixture_ipc.cjs) 的 JSON-line 訊息、有限等候及子程序收尾；逾時會移除 waiter，收到訊息會清除 timer，無效 JSON／輸出關閉會回安全錯誤。清理只針對自己啟動的 child；stderr 統一只保存安全標記，沒有保存原文。情境操作、驗收斷言及公開投影留在各入口。

兩個受影響桌機各 8 項重新通過，child 均正常退出、沒有 cleanup failure。完整 Monitor gate 也檢查共用工具語法與來源指紋。異常輸入及強制終止分支沒有另做故障注入；本輪實際 host 操作驗證的是正常收尾。

## 規格軸

> 規格軸：1 個待驗收門檻；0 個新增產品缺陷；0 個範圍擴張。

當時待取得的核心門檻，之後依直接人類核准的範圍完成：單一 [fixture patch](evidence/core-project-state-fixture.patch)、隔離的全部現行 1,921 項測試、原整合命令與全部 8 步通過，438 份輸入前後一致。[安全摘要](evidence/core-canonical-acceptance-20261022.json) 保留精確 archive 雜湊；原工作區 71 失敗／2,483 通過及另列的 17 份歷史快照不改稱全綠。

審查後第一輪 Monitor 重跑曾 87 通過／1 失敗：斷線任務結果返回 409，原錯誤本文未保存，不能回推確切原因。新增的真實 HTTP 併讀案例實際取得 `task_busy`，在相同內容重送及再次讀取時維持 1 次模型呼叫；這證明既有非阻塞機制，不證明原失敗必定有相同錯誤碼。

斷線測試現在只對明確 `409/task_busy` 的結果讀取有限等候，其他 409 立即失敗。受影響模組 8 項及完整 Monitor **89 項**通過；沒有修改產品或核心。原失敗、診斷、最後通過及實際錯誤碼均保留於 [驗收索引](evidence/monitor-issue20-final-20261022.json)。新增一個行為案例後共 20 個 HTTP 案例、182 筆回應，桌機為 8＋4＋8 項通過。

本輪同一 implementer 完成全部修正，審查候選已合入 `e5fbd6e7cb06e0512d78f56d74c17e1012bc68c2`；[同步與產品指紋核對](../runtime/final-acceptance/final-integration/product-sync-verification.json) 已通過，待整合者依工作單流程結案。完整證據、原失敗與限制見 [目前驗收報告](issue20-final-acceptance.md)。

最終複審：**標準軸硬性違規 0、剩餘判斷性意見 0；規格軸新增缺陷 0、範圍擴張 0，原核心必要門檻已按核定範圍補齊。** 原文見 [標準最終報告](../runtime/final-acceptance/final-integration/standards-final.md) 與 [規格最終報告](../runtime/final-acceptance/final-integration/spec-final.md)。
