# RG-04 完整費用保障缺證矩陣

日期：2026-10-19；狀態：**prepared／正式資格 pending**。以既有本機來源整理，不重新連線供應商、帳戶或公開 API，也不聯絡客服。本表引用的是先前保存的觀察，**不是本日重新確認的價格或保障**。

## 精確候選與證據層級

分類候選為 `typesafe/jev-1.13-20260917`、provider `typesafe`、`https://openrouter.ai/api/alpha/decisions`；不是 `typesafe/jev-router` 託管服務，也不繼承原生 TypeSafe／systemone 的保障。初答與修正候選見[範圍草案](release-scope-draft.md)；歷史 Chat 端點為 `https://openrouter.ai/api/v1/chat/completions`，正式模型／provider／帳戶／期間仍須逐一核定。

證據分為：已完成的本地機制／離線合成測試、歷史公開來源觀察、缺少的供應商有效承諾、獨立審閱與真實核帳。本輪不把前兩者當後兩者。既有兩條機制見[供應商美元上限](provider-bounds.md)與[元件上界 v1／v2](component-bounds.md)；不強求供應商實作 CloudyFan HMAC。

## 八個分類費用元件

| ID／元件 | 歷史已有材料 | 尚缺及完成條件 | 狀態 |
|---|---|---|---|
| F01 input | 既有 endpoint 目錄觀察 max_prompt_tokens 262144、input 標價 0.000000042 USD/token。 | 精確日期版本／帳戶期間下，每個外部 call 的 state、instructions、criteria、序列化／包裝、拒絕前處理及全部內部嘗試的**累計**可計費整數上界；有效最高單價、取整／最低費。目錄 context 不是該保障。 | 部分觀察；未核定。 |
| F02 output | 歷史目錄 max_completion_tokens 235929、completion 標價 0；既有官方指南稱 output 免費。 | 可採全成功／拒絕／失敗／取消／重試及在途的完整零費率保證；否則需強制累計數量及有效單價上界。Decisions 不沿用 Chat max_tokens。 | 零標價觀察；未核定。 |
| F03 reasoning | 歷史指南稱不回傳 reasoning traces。 | 「不另計費且所有路徑零」或獨立有界計量／費率；不回傳 trace 不能推出免費，free output 不能替 reasoning 豁免。 | 缺完整保障。 |
| F04 cache_read | 歷史目錄 supports_implicit_caching=false。 | 精確配置／所有嘗試的零費率保證或獨立累計上界與費率；欄位 false 不能證明全部 cache 費為零。 | 缺完整保障。 |
| F05 cache_write | 同上，尚無獨立寫入保障。 | 同 F04；input 或 output 零價不能令 cache 自動免費。 | 缺完整保障。 |
| F06 request | 有外部 POST 與 response schema。 | 固定請求費、平台／供應商加價、tier／long-context、取整／最低費的完整清單與最大值或零保證，含內部重複／子請求。充值費／稅與 inference 扣款邊界須明列，不任意當逐次費用。 | 缺完整保障。 |
| F07 classification | 歷史資料支持按 input 計費的結構。 | 明確是否已包含全部分類費；若已含則獨立項零費率需有完整保證，不能重複加價；advisor／隱含付費服務須排除。 | 結構觀察；未核定。 |
| F08 failure | 歷史 OpenRouter FAQ／Zero Completion Insurance 有失敗保護。 | Decisions 沒有 finish_reason 時的政策映射；拒絕／錯誤／取消／重試所有通常費目及額外費最大值；無 ID 如何最終核帳。不能把所有非 200、空答案或 timeout 視為零。 | 有公開保護；端點適用與完整量化缺證。 |

完整費用沒有核定數字。本輪**不**把歷史 `262144 × 0.000000042` 的條件試算填成 bound，不從公開零標價生成 v2 guarantee，也不把免費開源模型情境當真實零費率。

## 初答及各修正候選同樣不能省略

| 候選角色 | 端點／版本來源 | 必須補什麼 | 狀態 |
|---|---|---|---|
| 開源首答 | 歷史 profile 的 GPT-OSS-120B／BaseTen FP4、Chat | 不可變版本／量化、精確 endpoint/provider；F01～F08 各自有界或完整零費率保證。 | 尚無本 scope 正式費用證據。 |
| 低分唯一修正 | 歷史 GPT-6 Sol 日期版 low、Chat | 同上，含 output／reasoning 強制累計上限與費率、取消／失敗核帳。 | 尚無本 scope 正式費用證據。 |
| 中分唯一修正 | 歷史 GPT-6.1 Sol 日期版 high、Chat | 同上；不能沿用低分版本或不同強度的證書。 | 尚無本 scope 正式費用證據。 |
| 高分唯一修正 | 歷史 GPT-6 Astra 日期版 xhigh、Chat | 同上；不能以報價或 max_tokens 參數單獨證明完整金額上界。 | 尚無本 scope 正式費用證據。 |

## 全路徑與流程預留

| ID／路徑 | 本地機制已有證據 | 正式供應商／帳務缺口 |
|---|---|---|
| P01 成功及答案不合格 | 所有 paid calls 留帳，修正不免單。 | 回應費是否完整涵蓋供應商／平台全部費目與內部嘗試。 |
| P02 400／413／422／429／529／5xx／拒絕 | HTTP failure fee 保存；缺帳維持未知，不重送。 | 各端點的保護適用、最大通常費＋額外費、零費條件及最終收據。 |
| P03 timeout／取消／斷線 | intent 保留、unknown stop、風險預留保留。 | 上游何時停止、之後最大可計費餘量、無 ID 終局核帳。 |
| P04 內部 retry／重複／child | 本機無自動 paid retry、關閉 fallback。 | 這不限制內部次數；需累計上界或每外部 call 最多計費一次的適用保障。 |
| P05 漲價／到期／撤銷／在途 | 派送 lease、review 效期、持久撤銷與 breach stop。 | 已派送請求在保障到期／撤銷後仍適用的費率／數量承諾。 |
| P06 完整流程預留 | BudgetedCore 的共享預算、精確算術、階段／工具完整預留。 | 所有可達初答／分類／唯一修正／工具都須合格。tool-free SQL 候選最壞為初答＋兩次分類＋最大可達修正；一般多 turn／工具設定依既有 budget 契約預留，不能照抄四 call。 |
| P07 核帳／中斷／歷史成本 | 同事件費用替換不加總、unknown 不歸零、失敗與中斷保留。 | 各端點可查 request/generation、無 ID 處理、結算期限與修訂／退款範圍；凍結前完整歷史風險清單及帳戶邊界。 |

歷史最新批次的 487/487 核帳、未知為零只屬該批次；其他歷史報告曾有 404 待核帳，本輪沒有查帳，因此**不宣稱歷史未知已清除，也不重新認定目前仍只有兩筆未知**。未經帳戶／帳本逐項核對，不填目前可用餘額或總風險數字。本輪不讀私人帳本、憑證或原始回答。

## 優先補證與可執行停止條件

1. **Q01（P0）**：精確版本／provider／端點／帳戶期間綁定及 Jev input 全嘗試累計硬上界；先對照既有文件，未證不填值。
2. **Q02（P0）**：八元件封閉清單與有效費率；有完整全路徑零費率承諾才用 v2，其他元件仍需可強制數量。保障須涵蓋在途、變價、取消／失敗／重試。
3. **Q03（P0）**：開源首答及低／中／高每個可達修正設定各自合格，不能只證分類器。精確 configuration、credential binding 與實際派送設定由可信管理者離線建立，憑證不放本文。
4. **Q04（P1）**：帳務終局性及歷史未知風險盤點，包含無 ID 路徑；供應商文件與私有收據分開保存。本輪只列所需證據。
5. **Q05（P1）**：具名獨立 reviewer 審查來源／效期／withdrawal registry 綁定，再另行提出具完整預留及授權的最小真實執行／核帳計畫。本輪不送信、不簽假核定、不執行。

缺任一可收費元件或可達候選的保障，就維持 RG-04 pending。少量成功樣本無法取代普遍保障；不因缺證關掉預算檢查、放寬 unknown stop、改 endpoint 或靜默 fallback。

來源為外層 `docs/jev-decisions-bounds-audit-2026-10-19.md`、`docs/jev-decisions-supplier-guarantees-2026-10-19.md`、`artifacts/jev-decisions-bounds-20261019/public-evidence.json` 及本套件契約／離線證據；其位置與 SHA256 保留於[準備證據](evidence/release-evidence-preparation-20261019.json)。歷史研究的「v2 尚未實作」屬當時紀錄，現況以本套件 [v2 契約](component-bounds.md)為準；本輪不回寫歷史報告。
