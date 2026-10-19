# RG01-08 既有證據技術審閱（未簽核）

2026-10-19。依使用者授權，對本機提交 `fd2c5911dfe07e6f50da1803f5ca33ab92354250`
的既有材料進行技術審閱。**在下列有限範圍內，未發現新增技術阻擋；建議提請有條件
核定。這不是正式核定、簽名、部署許可或新的模型實驗。**

正式 registry 原檔、scope、owner／reviewer、evidence 與 pending 狀態全部保留。
技術審閱階段沒有 SSH、Scout 探針、模型 API、供應商聯絡、部署、推送或本機提交。
後續依使用者授權進行本機提交檢查；見[提交檢查紀錄](evidence/rg01-review-precommit-20261019.json)，不改變正式資格。

## 建議可提請核定的範圍

1. **有限公開 SQL 驗收**：`sql-review-catalog-v2-bounded` 的 S01～S08、固定公開
   schema／fixtures／expected 與既定題意，經 `SqlContract`／`SqlEnvironment` 接入
   tool-free `Core.run`。它只判斷公開樣本是否符合，不證明查詢對所有新資料正確。
2. **已採用的有界資料與輸出契約**：唯一非 NULL signed int64 ID，含 0／負值；
   NULL／無主引用保留；amount integer／NULL 且絕對值至多 10^9，每表每 fixture
   至多 1000 列；參考合計／前綴絕對值至多 10^12。保留既定 NULL／排序／同額
   最小 ID／S08 BINARY 語義。輸出最多 100 列／32768 cell bytes 等限制另行生效；
   1000 列輸入不等於可輸出 1000 列，大型公開資料也可能超出預設 context。
3. **固定受限執行設定**：既有 Scout ARM64／kernel `6.12.75+rpt-rpi-2712`、
   Python 3.13.5、SQLite 3.46.1、bubblewrap 0.12.0 與收據所列 rootfs／bwrap／
   實作 SHA256、namespace、唯讀掛載及 cgroup 限制。這是指定建置的觀察證據，
   不是本次重新確認 Scout 目前仍可啟動。
4. **限定的流程保證**：同設定下 S01～S08 正常流程、完成結果重用；已保存回答後
   對真正 immutable SQL worker 注入 SIGSTOP、由 production deadline 結束後，
   恢復同答案、保留原收據及帳務控制，不再 POST 一次。模型 I/O 全為 MockTransport。

上述範圍以[已採用決策](rg01-first-scope-decisions-20261019.md)、
[實作包](rg01-implementation-20261019.md)及[原始收據索引](evidence/rg01-linux-target-runtime.json)
為準；不重新要求選 S01～S08、金額域、NULL／排序規則或 Linux 目標。

## 逐項審閱結論

| 清單項目 | 本次技術審閱結果 | 正式核定時須保留的界線 |
| --- | --- | --- |
| 題意與資料域 | 決策、Python oracle、參考 SQL、資料域與輸出檢查一致；超域 fixture／不合規 expected 在派送前拒絕，模型結果不符 expected 則在驗收時拒絕。 | 核定固定有限契約，不接受任意資料庫路徑、不暗中刪列／截斷或正規化資料。 |
| expected 與覆蓋 | 本機記憶體 SQLite 重新交叉核對原 40 組手列／Python expected、新版 53 個題目／樣本配對；14 個原 mutant、13 個新增 wrong-query probe 均在新版樣本中被拒絕。 | 53 是配對數，不是獨立題數；新增反例為事後材料，不是 holdout。 |
| 隔離與限制 | 既有 8/8 正常流程及 7/7 取證、腳本／policy／答案雜湊、前後 inventory 與回收紀錄相符；原失敗未覆寫。 | 前六項為受信任 Python 探針，只有最後一項直接測真 SQL worker 超時恢復；不是完整 sandbox／逃逸證書。 |
| 診斷與唯一修正 | 固定 SQL adapter 僅投影公開診斷碼；已知模型錯誤與工程異常分開；既有離線回歸涵蓋修正上限、未知不可算成功及版本改動拒絕重用。 | 外部驗收器仍須自行提供可信公開 expected 與 provenance；不能把任意注入驗收器視為已合格。 |
| 答案、重送及預算 | 真 worker 中斷後 response hash 不變、合計 1 次 Mock POST；active reservation 0.482→0，保留 0.02 合成 provisional hold。 | 不能說所有 hold 已清零、真實費用已核帳，或因此取得 RG-04 費用資格。 |
| 責任與時效 | 技術材料可供審查，正式 owner／reviewer／核定 evidence 尚未填寫。 | 需真實具名審閱、接受範圍／殘餘風險、指定有效期間與撤回／重驗責任。AI 不代簽。 |

### 本次真正做過的檢查

- 摘要內 **26 筆雜湊引用**、取回 Scout 的 **96 個檔案**及保存答案雜湊再次符合。
  原始成功觀察與初次 provider fixture 身份不符後的 blocked／七項 not_run 皆保留。
- 目前 source digest 與既有完整離線及 target evidence 一致：
  `37954eebe35b43c6182b92bcd67c6e74fa43342e9a4ba8da4320ae3f49492ee5`。
  既有 1,102 項完整驗證是同指紋的歷史執行，本輪沒有重跑整套 pytest。
- 本機 SQLite 3.50.4 以已存在的可信查詢／公開 fixture 重新計算 expected 與反例；
  這不新增題目、不取代 Scout SQLite 3.46.1 的既有實環境紀錄，也不增加隔離證據。
- 另有兩個唯讀 AI 技術審閱，分別核對語義／覆蓋與隔離／控制面；都未發現此有限
  主張內的新阻擋。AI 複查不是具名人員的正式核定。
- 原始紀錄、檢查方法與引用 SHA256 見[技術審閱摘要](evidence/rg01-technical-review-20261019.json)。

## 必須明列的限制與不涵蓋事項

- **有限樣本仍可能誤收**：特製查詢可記憶公開答案或依 fixture 特徵分支。拒絕
  27 個選定錯誤不代表母體誤收率為零、任意 SQL 正確或真實模型成功率。新契約／
  新 fixture 仍需可信 expected 的產生與審閱；若要求對未見資料也保證正確，現有
  有限公開材料不足以支持該主張，不能只靠簽核補足。
- 金額界限保護的是所定參考題意的合計，不保護錯誤 SQL 的任意乘法、ID 運算或
  Cartesian join。工程中斷／無法驗收不算答對，也不因有限回歸通過而消失。
- 取消測試是 WAIT_PROBE ready 後注入 KeyboardInterrupt，經共同 lease 回收；
  **沒有直接實測真 SQL／Core.run 的產品客戶端取消、宿主關機或 supervisor SIGKILL**。
  真 worker 證明的是 SIGSTOP→production timeout→同答案恢復，不能混稱。
- `pids.max=8` 有配置與 lease 核對，沒有另做 fork-exhaustion。CPU 3.842884 秒為
  兩程序合計，不能當成單程序 2 秒上限失效；3.014 秒 deadline 含回收開銷，非
  精確三秒 SLA，也沒有延遲分布或負載可靠性研究。
- 可信 host kernel、管理員、supervisor 與專用 leaf 管理是前提；未證明惡意同 UID
  主機程序、kernel 漏洞、其他租戶或部署後完整安全。既有容器 inventory 相同僅為
  當時狀態觀察，沒有重驗 Scout 全部應用。
- 舊 scope 已結束；後續須建立新 scope／cgroup identity 並重查 pins／limits。
  rootfs 無開機自動重掛載；重開機後不能沿用舊路徑或本紀錄作即時 readiness。
- 不核定真實模型／Jev 選擇精確性、商業降本、供應商費用、部署身份／TLS、
  HTTP／Ollama／Queue 實際部署或整體正式上線；相關 gate 仍各自 pending。

## 仍需你／實際正式審閱者確認

1. **是否接受此有限保障及殘餘風險作為本版本的 RG-01 核定範圍**，並在核定文字中
   排除上述未支持主張。這是接受限制，不是重選已決定的八題或資料域；若要求更大
   的產品取消、未見資料正確性或多租戶安全主張，須另列所需證據。
2. **實際具名 owner／reviewer 與正式審閱結果**：可核定、附條件或拒絕；應引用
   精確 scope ID、提交／契約／artifact hash、成功與失敗證據及授權。不得把本 AI
   技術報告改名成你的簽核。
3. **生效／到期與停用責任**：指定有效時間、誰負責撤回／停用／重驗，以及 kernel、
   runtime、artifact、契約、資料域或資源限制變動的重新審查條件；期限目前未設定。

本次僅完成技術審閱。RG01-08 正式核定工作仍 blocked，RG-01 正式資格仍 pending；
沒有新增正式 evidence、簽名或資格狀態。
