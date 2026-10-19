# RG-01 新版有界 SQL 與隔離接入

2026-10-19。使用者已透過 `mattpocock/implement` 授權實作 8 張審查稿；原草稿的
「未授權實作」是當時狀態，不覆蓋本次明確指示。技能要求的本機提交在範圍內；
不發布 Issues、不聯絡供應商、不花費 API、不部署、不推送，不取得主機或下載映像。

## 交付與仍受阻擋的工作

| Ticket | 交付 | 狀態與證據邊界 |
| --- | --- | --- |
| RG01-01 | 新版資料域、契約身份與派送前拒絕 | 已接入；最終檢查依本輪離線收據。 |
| RG01-02 | S01～S05 合計／年份／NULL 關聯／無主群／有號排序 | 補入 P01、P02、P03、P04、P05、P11。 |
| RG01-03 | S06／S07 排名／累計／NULL／排序／無主群 | 補入 P06、P07、P08、P12、P13；完整唯一排序鍵的 RANGE 等價控制保留。 |
| RG01-04 | S08 BINARY 大小寫碰撞及空字串 | 補入 P09、P10。 |
| RG01-05 | Linux 嚴格模式與失效拒絕 | 已接入；本機只具備協定／故障注入證據，沒有 Linux 安全核定。 |
| RG01-06 | 固定 Linux 目標實環境取證 | **限定範圍技術取證完成**；[Scout 正常 8/8、七項取證 7/7](rg01-scout-target-20261019.md)，保留 probe／真 worker 邊界與原始中斷紀錄，不自動核定正式 gate。 |
| RG01-07 | 新版全流程離線審閱包 | 本文件、反例覆蓋、回歸、產物檢查、獨立技術 review 與新收據；不等待 06 才能整理。 |
| RG01-08 | 正式具名核定 | **blocked**；06 實證已補齊，待實際正式 reviewer 審閱與具名核定。AI 技術 review 不冒充人類簽核。 |

狀態機讀來源為[本批 ticket 狀態](evidence/rg01-ticket-status.json)，不是正式 gate。
[正式 registry](release-gates.json)保持原樣，RG-01 及其他 gate 仍 pending。
既有 SQL v2、公開診斷、兩次生成上限、答案保存、帳務／預算與續跑並未重建。

## 新版契約與公開資料

`sql_catalog.contract('S01')` 至 `S08` 建立 `sql-review-catalog-v2-bounded`；舊
`sql_review` 的 v1 程式、五組資料與歷史結果保留。新 catalog 共 53 個公開合成
case/sample 配對：原 40 個加 13 個事後診斷反例。這不是新 holdout 或新模型實驗。

`SqlContract(..., domain_policy='rg01-sql-first-scope-policy-v1')` 明確選擇新域；
舊泛用 SQL 契約不會默默被改成新的業務範圍。金額為 integer／NULL、每值絕對值
至多 10^9、每表／訂單最多 1000 筆，因此參考查詢的任意子集合／前綴絕對和至多
10^12，安全落在 int64。任意錯誤 SQL 仍可能自行溢位，未知 SQLite 錯誤維持工程中斷。

ID 各表內唯一且非 NULL，保留完整 signed int64、零／負值；引用允許 NULL／無主值。
日期為有效公曆 YYYY-MM-DD、年份 0001～9999 或 NULL，status 僅 paid／void。
保留 TEXT、空字串、NULL 與 BINARY 比較；不轉時區／貨幣單位、不正規化或刪列。
舊唯讀、100 列輸出／32,768 cell bytes 等限制繼續適用，超域 expected 在派送前拒絕。

policy 實作與限制、公開資料、執行器與隔離描述都參與凍結身份。政策／資料／隔離
改變不能冒用舊 run 或重送已付費答案。完整 1000 列公開 fixture 可能超過 Core 預設
65,536-byte context；邊界測試明確設定較大額度，adapter 不放寬預設或截斷資料。

手核值與 Python oracle／參考 SQL 分別核對；新增反例驗證錯誤首答→公開固定診斷→
既有路由→唯一修正。反例／參考 SQL 僅用於離線 review，不作為選模輸入；公開
fixture／expected 是明確公開契約。隱藏評分不能回饋修正。有限公開資料仍能被特製
查詢記住；指定 13 個漏收消除不代表母體誤收率為零、任意 SQL 正確或真實模型品質。

## Linux 嚴格模式的部署前提

以 `SqlContract(..., isolation=LinuxIsolation(...))` 明確選取；portable 模式與
`require_address_space=True` 的舊獨立政策保留，嚴格 Linux 不可用時不回退。

目標維護者必須事前準備以下內容；本程式不建立／掛載／配置它們：

1. 固定 kernel／machine 與 SHA256 的 root-owned、不可群組／其他使用者寫入的 bwrap。
2. 已掛載唯讀、內容與 mode 已固定 hash 的最小 rootfs；不得有巢狀宿主掛載。
   含完整 `/runtime/bin/python3` 及其唯讀相依、與本版本一致的 `/app/sql_worker.py`、
   `sql_executor.py`，另有空的 `/proc`、`/dev`、`/tmp` 掛載點。Python／SQLite 必須符合
   父程序原有 runtime 契約；過寬或含秘密的 rootfs 不得核定。
3. 專供此 supervisor 使用、無活程序的 cgroup v2 leaf；確保外部管理者不移入任務、
   不改限制或置換 artifacts。程式對 leaf directory 取非阻塞 flock、檢查真實 cgroup2
   掛載與控制值，不用一般檔案假裝已執行 OS 限制。
4. 事前設定 `memory.max=268435456`、`memory.swap.max=0`、`memory.oom.group=1`、
   `pids.max=8`、`cpu.max="100000 100000"`、`cgroup.type=domain`，並授予 join／kill
   該專用 leaf 的權限。整群 CPU bandwidth 是一個 CPU，worker 另有原本 2 CPU 秒上限；
   不是宣稱整群累計 CPU 必然恰好 2 秒。kernel memory.max 可有短暫超量，不能當成
   每個瞬間精確 256 MiB 的服務承諾。

固定 launcher 強制 user／IPC／PID／network／UTS／cgroup namespaces；不採 `*-try`。
停用再建立 user namespace、移除 capabilities、清空環境、rootfs 唯讀；只有 sandbox
的 proc／minimal dev 與 8 MiB tmpfs，沒有宿主根目錄、憑證或 cgroup 控制檔掛入。
既有 SQLite allowlist、CPU／allocator／檔案與外部 deadline 繼續使用。

worker protocol 升為 `sql-worker-v2`，回報真實 Python implementation、完整 version
及 cache tag；父程序於每次 preflight／驗收比對，缺漏或不匹配時在 POST 前拒絕。
這是兩軸技術審查發現的 P2 修正；舊 worker 身份不會在新契約中被默默沿用。

可信 trampoline 先進入 leaf 才 exec 固定 bwrap；離開任何執行路徑皆 kill 整個專用
cgroup 並確認 populated=0，原 worker 仍 wait/reap。驗收／取消失效不算模型答錯，
不支付分類或修正；已保存答案與預算 hold 維持，恢復不重送。程式不刪除 leaf、
映像或使用者檔案。這些是實作保障，仍需要目標上的攻擊與資源取證。

設計依據為 [bubblewrap 官方說明與限制](https://github.com/containers/bubblewrap)、
[官方參數文件](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)及
[Linux cgroup v2 文件](https://docs.kernel.org/admin-guide/cgroup-v2.html)；
閱讀文件不等於供應商保證或本環境已驗證。

## RG01-06 取證入口與尚未執行清單

執行 `uv run --locked --offline python scripts/qualify_sql_worker.py` 只輸出本機能力
與待測矩陣，不啟動 worker、不改宿主政策、不連真實 API。未合格以 exit 2 表示，
不是一般檢查失敗或通過。候選 policy JSON 只接受 LinuxIsolation 的明確欄位。

未來已有授權 Linux 環境時，可明確提供 `--policy`、`--exercise`、`--authorization-ref`
與新的私有 `--output` 目錄，測正常 S01～S08 真 worker＋MockTransport／BudgetedCore、
完成結果重用與釋放預留。它只產生 partial 證據，**沒有自動跑下列攻擊**，不能靠
此工具的正常 SQL pass 把 06 結案：

- 全程序／子程序總記憶體超額、OOM 事件與回收。
- CPU 超額、外部 deadline、取消與所有子程序回收。
- 受控宿主 sentinel 的讀寫拒絕、namespace／網路拒絕；不測真憑證或供應商。
- 真 worker 故障發生於答案保存後的預算 hold、恢復與不重送。

本機已有這些流程的 portable 真程序／OS 故障模擬回歸，但不繼承為 Linux 隔離證書。
正式 reviewer 必須審查固定建置、命令、日期、source／artifact hash、成功／失敗／
未測結果、責任與失效條件；不具名、不足證或有已知漏收時，RG-01 保持 pending。
本輪不捏造人名或代簽，不改 RG-02／RG-04／RG-05 等其他資格。

## 本輪最終離線結果

完整 `./scripts/check --integration`：**1,098 passed，127.00 秒**；Ruff、兩條 mock high 路徑、sdist／wheel 通過。另以 `-I -S -B` 從解包 wheel 載入並完成八類參考查詢，13 個反例資產與 worker v2 均可用。

Standards：初審 1 項 P2（Python 身份核對缺口），修正後 0 remaining。Spec：同一缺口 1 項 P2，修正後 0 remaining。兩者為獨立 AI 技術審查，沒有正式人員簽核。

[完整收據摘要](evidence/rg01-tickets-offline-20261019.json)保存命令收據／來源指紋／產物與 review 歸因；[ticket 狀態](evidence/rg01-ticket-status.json)為 6 completed、2 blocked。原正式 registry hash 不變。`project-state` 的全批 completion 仍為 false，是兩項阻擋尚未解除，不是前述六張未實作。

## 後續 Scout 準備狀態

上述「沒有指定 Linux 目標」是本批實作當時的阻擋。使用者其後授權 Scout 專用
環境準備，現已完成固定 ARM64 rootfs／bwrap／專用委派 scope 的實際 SQL 啟動與
回收驗證，見[準備紀錄](rg01-scout-environment-20261019.md)。RG01-06 的 blocker 已更新為
尚缺受控資源、檔案／網路、取消與中斷取證，不再是缺主機；RG01-08 仍缺正式審閱。
初版 cgroup／ELF loader 設定失敗已保留，未拿最小 smoke 取代完整隔離資格。


## 後續取證完成

[Scout 限定取證](rg01-scout-target-20261019.md)已完成 RG01-06 技術交付；本批 ticket 現為七張完成、一張正式審閱受阻。以上較早的「六張完成、缺 06」數量是當時狀態。最終完整離線驗證 1,102 tests，對應新的 source digest／收據；正式 gate 仍 pending。
