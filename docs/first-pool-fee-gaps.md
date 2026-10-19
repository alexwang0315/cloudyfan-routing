# 首版候選費用保障（POOL-04）

2026-10-20，使用既有 profile 與歷史證據整理。機器可讀矩陣由 prepare-first-pool 輸出的 fee-gaps.json 重現；它是缺證報告，不是 provider verifier 或執行授權。

矩陣綁定 profile_sha256、逐候選 configuration_sha256、模型版本／強度／供應商／端點／明確量化。未提供的權重、量化、帳戶期間保持未知；不從 provider 名稱猜測。每個候選都列入，候選數量不限制為三個；intended_tier 只是尚未取得資格的角色註記，不代替實際分群。

| 角色 | API | 優先缺證 |
|---|---|---|
| 開源首答與低／中／高修正 | 固定 Chat Completions；無工具／外掛／fallback | 精確版本／provider／帳戶期間的輸入、輸出、reasoning 累計計費上界與有效最高費率 |
| 分類（最多兩次） | 固定 Jev Decisions 日期版；非 hosted Jev Router | 全部內部嘗試、state／instructions／criteria／包裝等累計 input；其他費目有界或全路徑零費率保障 |

全部角色均須涵蓋八個元件：input、output、reasoning、cache_read、cache_write、request、classification、failure；七類路徑：成功、拒絕、失敗、取消、斷線、上游 retry／child、變價及在途。逐格保持 pending，無有效保障不填數字或零。既有本地機制、歷史公開聲明、核帳與供應商承諾分層依 [RG-04 矩陣](rg04-evidence-gaps.md)，未合併為一份保障。

tool-free、每階段一個生成請求的符號預留為：

`開源首答 + 2 × 分類 + max(低分修正, 中分修正, 高分修正)`

缺證時完整美元上界為 null，不從平均實付推算。一般工具往返依 BudgetedCore 的 max_turns／工具上界計算，不套用上面四-call 切面。

## 已有材料不等於費用保障

- 完整預留、元件上界／零費率驗證器、有效期間／撤銷檢查及 unknown stop 已實作並離線測試；缺少真實承諾時仍拒絕派送。
- 429 歸因採 openrouter-fixed-text-upstream429-v2，僅用其精確適用條件；actual receipt 優先，未知不歸零。這不是所有 429 免費、完整供應商封頂或重送授權。
- 最新 221-call 批次 USD5.805611568 已核帳、批內未知零，是 [既有批次](fresh50-recovery-20261020.md) 的歷史事實，不清除更早未知費用，也不是本輪新帳戶查詢。
- 來源價格、免費模型假設、token 參數或 key 額度不自行成為元件保障；已包含的分類／平台費不能重複加算。

## 後續補證順序（本輪不執行）

1. Jev 的累計 input 與八元件封閉清單，確認全路徑的有效數量／費率或 binding zero guarantee。
2. 首答與每個可達修正設定的同等保障，綁定帳戶／有效期間與 in-flight 範圍。
3. 無 ID／取消／失敗的結算終局性、舊未知費用盤點，再做獨立審查。

這是未來最小詢證／驗證清單，不對外提交、不查帳、不做 canary。費用缺證不阻擋合成模型池與 MockTransport 的離線整合；正式派送仍必須通過 RG-04。
