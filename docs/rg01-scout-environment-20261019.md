# Scout 專用 SQL 測試環境準備

2026-10-19。使用者另行授權準備 Scout 專用環境，包含系統套件與權限設定，
並親自在終端完成兩次 sudo 設定。**準備與最小啟動驗證已完成；正式 RG-01 資格仍 pending。**
本次沒有模型 API、供應商聯絡、資源耗盡／攻擊測試、服務部署或推送。

## 已確認的環境與結果

| 項目 | 實際觀察 |
| --- | --- |
| 主機 | Scout，ARM64，kernel `6.12.75+rpt-rpi-2712` |
| 執行版本 | Python 3.13.5、SQLite 3.46.1、bubblewrap 0.12.0（Debian 套件 `0.12.0-1~deb13u1`） |
| 套件 | 安裝固定 wheel 與 lock 相同版本的依賴至獨立 venv；未更新系統 Python 或既有套件 |
| rootfs | 約 66 MiB，root 擁有，獨立唯讀 bind mount；雜湊、內容、mode 與執行器核對通過 |
| worker 限制 | memory.max 256 MiB、swap 0、pids 8、一個 CPU bandwidth；沿用 worker CPU／SQL heap／deadline 與固定 namespace 參數 |
| 真實啟動 | 一般使用者執行 `SqlContract.environment()` 的 preflight，再以 `SUM(n)` 驗收公開 `[1,2]`，預期 `[[3]]`，通過 |
| 啟動與驗收耗時 | 1.132 秒，單次工程 smoke；不是模型延遲基準 |
| 回收 | strict launcher 退出後，專用 worker leaf 為 `populated 0` |
| 既有服務觀察 | 10 個既有容器仍在執行；兩個原有 healthcheck 容器仍 healthy；未觀察到新增失敗的 user unit |

完整摘要與私有原始收據的 SHA256 見[準備證據](evidence/rg01-scout-environment-preparation-20261019.json)。
應用程式來源指紋未變；本次快速離線檢查涵蓋依賴一致性、project-state 與 Ruff。
先前 1,098 tests 是前輪完整離線結果，本次沒有宣稱重新跑過完整 pytest。
容器 inventory 只證明觀察當時的執行／健康旗標，不是其全部應用功能驗證。

## 保留的失敗與修正

1. 初版 root-level cgroup 雖授權目標 `cgroup.procs`，跨 SSH session 移入時仍遭核心
   拒絕；缺少共同上層的 migration 權限。未開放根 cgroup 或全域權限，改用
   systemd 既有的 user delegation，為本次測試建立獨立 transient scope，將
   supervisor 與 worker 分置子群，再只在該 scope 啟用控制器。
2. 初版 rootfs 將 ELF loader `lib/ld-linux-aarch64.so.1` 誤設為 `0444`，
   Python 因 `EACCES` 無法啟動。另建 v2，唯一 bundle 差異是該檔 mode `0555`；
   保留初版檔案、唯讀掛載、policy、空 cgroup 與失敗收據，不回寫成成功。
3. 先在使用者擁有的候選副本、相同 bwrap 隔離參數下核對 SQL；這只算打包測試。
   隨後由使用者執行 v2 管理員腳本，另建 root-owned 唯讀副本，再以真正的
   `LinuxIsolation` artifact preflight／lease／SqlContract 完成最終 smoke。

舊 `user-assets-verification.json` 有欄位名
`not_yet_prepared_environment_refuses_preflight`；該測試刻意提供錯誤 bwrap SHA，
只能證明錯誤 pin 被拒絕，不能證明當時環境尚未完成準備。原收據保留並在摘要更正解讀。

## 下次執行的操作邊界

- 採用 root 管理的 artifact template：
  `/opt/cloudyfan-rg01/rg01-515632f-20261019-546d8058-v2/artifacts.json`。
  rootfs digest 為 `74df498bb1842408c43961e0cafb6a946690949c31302ce1837a688da5fa0f77`。
- 每次由普通使用者在新的、專用 `systemd-run --user --scope --property=Delegate=yes`
  測試 scope 內建立 supervisor／worker 子群；從 template 加入當次的 cgroup 路徑，
  保留有效 policy、cgroup identity、資源值及驗證收據。**不要直接使用初版 policy 的
  root-level leaf，也不要把已結束 scope 的舊路徑當作可用環境。**
- 本次確實執行的完整檢查器與兩版管理員腳本，保存在私有
  `runtime/scout-prep-rg01-515632f-20261019-546d8058/`，Scout user stage 亦有副本；
  摘要列有腳本 SHA256。這些是本次操作記錄，不是已發布的部署工具。
- scope 及其子群由 systemd 隨一次性測試結束回收。未安裝常駐服務或 fstab／開機任務；
  重開機後須重新準備唯讀掛載並重新核對，不能直接沿用本次準備成功的狀態。
- 初版與候選副本均未刪除；若日後清理，另依精確清單、容量、開啟檔案與授權流程處理。

## 尚未取得的資格證據

**RG01-06 尚未完成**：下一階段需要受控的整群／子程序記憶體與 OOM、CPU／deadline、
取消與所有子程序回收、合成宿主 sentinel 讀寫及網路拒絕、已保存答案中斷後的預算 hold
與不重送恢復證據。這些不是本次最小啟動 smoke 的結果。

**RG01-08 尚未完成**：仍需審閱上述證據及實際具名正式核定。
[正式 registry](release-gates.json)沒有修改，沒有將 AI 技術決策寫成使用者簽核。


## 後續取證進展

本文件的未測清單是環境準備完成當時的邊界。後續另行依「下一步」執行的
[RG01-06 限定範圍取證](rg01-scout-target-20261019.md)已完成正常 8/8 與七項 7/7；
現待 RG01-08 正式具名審閱，原始準備／失敗收據與本文件的歷史限制仍保留。
