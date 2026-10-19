# RG01-06 Scout 實環境取證

2026-10-19。承接使用者在專用 Scout 環境準備後的「下一步」指示，執行固定版本、
有界、逐項的合成隔離取證。無模型 API、供應商聯絡、常駐服務部署、原始碼推送或
正式人員代簽。**RG01-06 限定範圍技術取證完成；RG01-08 正式審閱仍待核定。**

## 實際結果

正常流程 **S01～S08 全部通過**，共 8 次 Mock POST；完成後重用既有結果，沒有重送。
7 項實環境取證全部通過，96 個從 Scout 取回的檔案逐一核對 SHA256。
前後 10 個容器 inventory 相同，原有失敗 user unit 沒有新增；一次性 scope 已回收，
rootfs 仍唯讀。這些是執行狀態觀察，不是 Scout 各應用的全面健康驗證。

| 項目 | 關鍵證據 |
| --- | --- |
| 整群記憶體 | `oom_group_kill +1`、`oom_kill +4`，父／子配置觸發整群 OOM，leaf 清空 |
| CPU | 忙迴圈受限終止；整群 CPU 3.842884 秒、38 次 bandwidth throttling，leaf 清空 |
| 外部 deadline | 等待程序在 3.014 秒內結束並清理，leaf 清空 |
| 宿主檔案 | sentinel 讀寫 ENOENT；rootfs 寫入 EROFS；host sentinel 不變、cgroup 控制檔不可見 |
| 網路／namespace | host listener 正控制成立；sandbox 無法連上；六種 namespace 不同、有效 capabilities 為零 |
| 取消 | ready 後注入取消，約 0.512 秒完成，leaf 清空 |
| 真 SQL 中斷恢復 | SIGSTOP 實際 immutable worker → `SQL_VALIDATION_TIMEOUT`；恢復通過，response hash 不變，Mock POST 只有 1 次 |

CPU 3.842884 秒是兩個忙迴圈程序的合計，不是單程序超過 2 秒的證明。
deadline 數字包含回收開銷，不是精確 3.000 秒的服務承諾。沒有另外聲稱跑過
fork-exhaustion 測試；pids.max=8 的配置與每次 lease 檢查仍有核對。

真 SQL 中斷時的合成已知成本為 0.01，active run reservation 0.482 保留；
恢復後 active reservation 為 0，但仍保留 0.02 的 provisional billing hold。
**不能說所有預留清零或真實核帳完成**；所有費用數字只驗證合成帳務行為，真實 API
呼叫與支出都是 0，不提供 RG-04 供應商費用資格。

[實環境證據摘要](evidence/rg01-linux-target-runtime.json)與
[離線證據摘要](evidence/rg01-target-offline-20261019.json)保留來源指紋、原始收據雜湊
與失敗歸因。最終 `./scripts/check --integration`：**1,102 passed，127.53 秒**；
Ruff、兩條 mock high-route demo、sdist／wheel 均通過。應用程式 `src/` 沒有變更；
此次新增取證探針、CLI opt-in 與四項回歸。獨立 AI 技術複查無剩餘阻擋，不等於正式簽核。

## 取證邊界

- OS envelope 探針使用相同 rootfs、bwrap pin、namespace、唯讀掛載與 cgroup lease，
  只把最後的 Python worker 程式換成版本固定的受信任探針。它們不是宣稱任意模型 SQL
  能執行 Python，也不是透過 SQL 攻破再測試 escape。
- 正常八類流程與中斷恢復使用未修改的實際 SQL worker、SqlContract 與 Core.run；
  所有模型請求經 httpx.MockTransport，費用數字為合成帳務。
- worker leaf 固定 memory.max 256 MiB、swap 0、pids 8、單 CPU bandwidth；探針沿用
  trampoline 的單程序 2 CPU 秒限制。supervisor 位於另一子群，外層 scope 另限
  768 MiB／32 程序／單 CPU／240 秒。無修改根 cgroup 權限或既有服務。
- 檔案測試只用本次建立的公開合成 sentinel；網路只用本次短暫建立的 host localhost
  listener，先做正控制。未讀取真憑證，未連外部端點。
- CLI 不帶 `--exercise --adversarial` 不執行資源探針；需提供 policy、授權引用與新的
  output。既有 output 不寫入。任一失敗保留該列，後續項目標 not_run。

## 測試矩陣

| 項目 | 需要的觀察 |
| --- | --- |
| 正常流程 | S01～S08 首答通過、完整結果重用、每題只發一個 mock POST、active run reservation 釋放 |
| 整群記憶體與子程序 | 子程序先配置 160 MiB、父程序再配置 160 MiB；worker 整群 OOM 事件、退出與 leaf 清空 |
| CPU | 兩個受信任忙迴圈繼承 CPU limit；核心終止、CPU 使用量與 bandwidth throttling、回收 |
| 外部 deadline | 相同 envelope 的父／子等待超時後回收；另由實際 SQL worker 中斷驗證 production deadline |
| 宿主檔案 | sentinel 讀寫不可見、rootfs 寫入 EROFS、無 cgroup 控制檔；host sentinel 不變 |
| 網路／namespace | host listener 正控制成立；sandbox 連不到該 listener；六種 namespace 與 host 不同、有效 capabilities 為零 |
| 取消與回收 | 先觀察父／子 ready marker，再由測試主動注入 KeyboardInterrupt；整群回收；提前外部 Ctrl-C 不能算通過 |
| 保存答案後中斷 | mock 回答已持久保存後，SIGSTOP 真正 SQL worker，production deadline 停止；保留合成成本與預留；重驗同答案成功、response hash 不變且無第二次 POST |

## 可重現入口與原始證據

既有 `scripts/qualify_sql_worker.py` 增加 opt-in `--adversarial`，固定探針在
`scripts/sql_target_exercises.py`。通常的離線 pytest 不會啟動 Linux 資源探針。
普通檢視及不完整取證仍 exit 2；即使技術證據完整也維持正式資格 pending、exit 2，
不能以退出碼自行核定 RG-01。

每個 Scout run 使用新目錄、scope 與 cgroup identity；原始輸出、逐列 JSON、有效
policy、normal-flow／resume 回答與合成預算庫留在私有 runtime，摘要只列核對值與
收據 SHA256。當次 scope 退出後，舊 cgroup 路徑不能再作為有效 policy。

## 已修正且保留的工程問題

- 預先 AI 審閱發現提前外部 Ctrl-C 可能誤記為取消成功，已補準備完成條件與回歸；
  只有確實觀察 ready marker 後的指定取消能通過。未降低正式核定門檻。
- 首輪 Scout 正常流程的 mock provider 名稱與 demo profile 不一致，被核心身份檢查
  正確攔截；當輪資源探針皆 not_run。修正 fixture 與 portable 全八類重用回歸，使用
  獨立 run 重執行；原模型／供應商身份限制不變，原失敗資料不刪除。
- 本機 full gate 首次因 pydantic_core 原生檔案缺失而無法收集；只從同版本本機快取
  補回與 installed RECORD SHA256／大小相符的檔案，保留 iCloud placeholder、未下載
  或改 lock／版本；原失敗收據保留。

## 正式核定仍分開

本輪有限探針不等於完整 sandbox 安全證書、任意 SQL 母體正確率、實際供應商費用
保證或部署後隔離承諾。正式 RG-01 仍須 RG01-08 審閱固定 build、全部成功／失敗
紀錄、有限任務域及其反例覆蓋並具名核定；正式 registry 不由取證腳本自動更新。
