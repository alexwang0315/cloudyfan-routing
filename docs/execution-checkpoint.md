# Monitor 本機接入執行檢查點

2026-10-22：完整方案已獲使用者確認並授權實作。手機端介面製作與測試已依最新指示停止；接續範圍為桌機、本機接入及驗收收尾。規格 [Issue #20](https://github.com/alexwang0315/cloudyfan-routing/issues/20) 已同步排除手機，ready-for-agent 標籤及本文已回讀確認。

## 已完成並取得證據

- Monitor 最新完整套件檢查：76 passed，Ruff、JavaScript 語法與離線 sdist／wheel 通過。原始檢查：`runtime/checks/925f741a42ce/checks.json`。
- 其中7項為真正loopback HTTP：跨程序單名額／429、長任務觀測與管理、重送／明示接續、socket斷線、30天到期與重建、共享未知費停止、程序中斷後不自動重跑。7份實際socket／mock呼叫紀錄在上述目錄的 `http-evidence/`。
- 真正 CategoryCore 透過 TaskService／BudgetedCore／RunBoundCore 的兩client接線已測；凍結workload由伺服器補入，重建／重送不新增呼叫。測試資料與費用均為合成。
- 受影響核心回歸：71 passed，紀錄在 core 的 `runtime/monitor-observation/green.log`；非阻塞retention與只讀結果仍驗原結果、原收據及身分。
- 桌機1280×800的8項操作通過：`output/playwright/main-thread-20261022/desktop-report.json`及同目錄截圖／ARIA。手機未新增製作或測試。預期的來源故障產生1筆HTTP503 console resource訊息，沒有JavaScript page error。
- 真實來源GET：獨立一次15次嘗試皆HTTP／schema成功，約30.62秒；23類、143個具名位置，24個端點未查，仍為部分更新。未發布／啟用，證據在 `runtime/main-thread-acceptance-20261022/source-get/`。不重跑此來源窗口。
- 已修正原授權回呼被覆蓋、停用client抹除歷史統計／歸屬、resume版本保存起點及真正CategoryCore缺workload的接線問題。

## 重開機後狀態與待收尾

- 本機於2026-10-22 13:34重開機；原18767／18768服務已不存在，不能沿用舊PID或port當作仍運行。
- 核心兩輪大型檢查在重開機前未結束：一般檢查至93%並已出現失敗；排除既有 `* 2.py` 的檢查至86%並有3個SQL相關失敗。兩者均屬中斷／未通過，不能當全綠。原始log及未完成checks.json保留，不刪副本或改SQL來掩蓋。
- 指定compound ORDER單案例另重跑1 passed；三個SQL測試的呼叫鏈未進入本次retention／result修改，但尚無完整trace，不能斷言失敗根因。
- 重開機後重新核對當前core來源與共享狀態；目前其他工作scope為routing-v2-s50-integration，保留其in_progress及付費授權限制。只完成自己的MONITOR兩項work-plan，不改其他scope。
- 重開機後已完成：目前版本的六個受影響核心模組119 passed，兩個模擬示範／建置通過（core runtime/checks/2c9f1398b3ce46dbacc4c8d4d62224b5）；Monitor再次76 passed及建置成功（runtime/checks/925f741a42ce）。18767預覽已恢復，Playwright確認原12件合成任務／12通過／0未知及0 page error。
- 驗收文件及機讀索引已完成，見main-thread-integration-acceptance.md與docs/evidence/monitor-main-thread-20261022.json。整個dirty core工作區的全面驗收另列未完成；不將指定範圍通過改稱全套通過。

## 邊界

不呼叫真實模型、不重跑來源GET、不改主線帳本或未知費限制、不部署／推送／commit／刪除資料。原始內容清理只提供preview，實際刪除仍須逐項批准。

## 最後收尾查核

目前來源指紋與機讀證據一致；39個文件連結有效，61個交付／來源／桌機證據檔案未包含來源憑證。18767目前程序與實際桌機載入均重新確認，不沿用重啟前PID。沒有新增手機測試、外部模型請求、來源GET、commit、部署、推送或刪除。
