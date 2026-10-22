# Monitor 本機接入執行檢查點

2026-10-22：完整方案已獲使用者確認並授權實作。手機端介面製作與測試已依最新指示停止；接續範圍為桌機、本機接入及驗收收尾。規格 [Issue #20](https://github.com/alexwang0315/cloudyfan-routing/issues/20) 已同步排除手機，ready-for-agent 標籤及本文已回讀確認。

## Issue #20 implement-spec 最新檢查點

- #21 身分與 #22 四模式已合入並結案；#23／父規格的必要檢查已在核定範圍通過；審查修正已合入e5fbd6e並同步，53份交付檔案一致、13份產品指紋不變，待整合者辦理工作單結案。
- 最新 Monitor 全 gate **89 passed**，Ruff、產品／3支桌機QA／共用fixture工具語法及離線建置通過；`runtime/checks/209c861141b0/checks.json` 固定完整產品、測試、QA及核心來源指紋。
- 真正loopback HTTP共20個案例、182筆回應；三組桌機8＋4＋8共20項通過。預期模擬503保留1筆console resource error，JavaScript page error皆0。
- 審查維護性意見已處理：共用JSON-line訊息、逾時與子程序清理，stderr統一安全標記。新增一次87／1的Monitor失敗及其409錯誤碼未知狀態保持不變；只對明確task_busy作有限結果等候，受影響HTTP模組8項及最後89全檢查通過，未改產品。
- 使用者核准單一合成fixture修正及隔離現行交付範圍；核心原命令在穩定438份輸入下 **1921 passed**，兩個模擬示範、依賴／records／Ruff與離線建置均通過。原17份633項快照保留，原71失敗／2483通過另列，不改稱原工作區全綠。
- 三個歷史SQL案例當前逐一與核准現行整套均通過；原中斷日誌缺traceback，歷史根因仍未知。RG-04與單一fixture修正各有直接人類核准收據；沒有擴改研究、付費範圍或檢查器。
- 沿用唯一一次來源GET的15次嘗試與15份已核對原始檔，24個未查端點維持部分更新；沒有重跑／發布／啟用真實來源。
- [目前驗收報告](issue20-final-acceptance.md)、[兩軸審查處理](issue20-code-review.md)、[機讀索引](evidence/monitor-issue20-final-20261022.json)。18767於15:08即時Chrome通過12件合成任務／12通過／0未知；最後同步已核對13份產品指紋相同，原18767再次GET回應200。三組新QA服務已正常退出。
- 本輪僅依implement-spec建立隔離本機提交，runtime／output不提交；沒有推送、部署、付費呼叫、手機工作或資料刪除。

以下保留較早的主線接入紀錄；其76測試／119指定核心結果與18767狀態屬各自執行時點，最新完成判定以上述段落及驗收索引為準。

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
