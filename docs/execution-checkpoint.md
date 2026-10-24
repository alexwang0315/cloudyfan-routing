# Monitor 本機接入執行檢查點

## 2026-10-24：依PR草稿選擇性本機提交

使用者授權本機Git提交；本輪包含客戶內部美元帳本／任務費率版本／明示OpenRouter唯讀核帳、Ollama Gateway、四池與Jev追蹤、官方SDK50題合成案例及相關測試／文件。既有桌機字級style.css、app.js六個樣式屬性及兩張歷史截圖留在工作區；新QA改用本輪data-task-run識別，避免依賴那些樣式。核心配套本機提交為`471c87197ba387db179ee1f102a3882075756012`（12檔，包含四個來源、五個測試與三份選擇性文件）。

選擇性index匯出的Monitor完整235項／12步檢查通過，含Ruff、產品與QA JavaScript、離線sdist／wheel；前後輸入指紋不變。以精確index靜態檔案覆蓋同一既有50題後端的桌機5組唯讀檢查通過，0 JavaScript錯誤／請求失敗／網外請求，沒有再送50題。核心提交子集另200項受影響回歸、記錄檢查、Ruff、兩個Mock示範與離線建置通過。原鎖版核心整合入口因certifi2026.7.22缺離線快取而在測試前停止；上述另用目前複製的certifi2026.10.19環境，不改提交依賴鎖檔、不稱原鎖版整套通過。測試內6處同值合成憑證改引用明示synthetic常數後，密鑰掃描通過並重跑完整235項。既有密鑰掃描與hooks照常執行。原Monitor235通過／核心2,055通過與50項SQL失敗屬原2026-10-23快照，沒有改寫成目前完整dirty工作區全綠。原R7指標仍stale。真實MRC主線接線與實際客戶逐筆扣款仍待完成，模型／Jev／50題價格均為合成。

原有未提交工作與私有憑證／研究／runtime保留；不讀.env，不做模型／供應商請求、推送、部署、手機或刪除。提交／來源／保留核對收據在CloudyFan `.scratch/cloudyfan-monitor-local-commits-20261024/`，本Monitor提交的SHA以本機Git紀錄及收據為準。

本頁保存 Issue20 各輪執行時的結果，以下「最新」限於該輪紀錄。改善後的目前驗收狀態請執行 `python3 scripts/acceptance.py status`；操作方式見 [驗收工具說明](tooling.md)。

## 2026-10-23：Ollama客戶50題桌機驗收

- 實際批次 `sim-20261023T084051-5408a8`：官方SDK0.6.3透過真loopback HTTP送50題，40次200／10次422；40通過、5失敗、5無法確認。110次作答（開源50／低15／中25／高20）及60次Jev，共170筆合成用量，未知費用0；成本0.1155 USD、170筆客戶扣款0.139128 USD、餘額0.860872 USD、待核0。
- 桌機 `output/playwright/ollama50-20261023-r3/report.json` **5組唯讀驗收通過**，涵蓋全部六種路徑、四池狀態、兩階段Jev／同分保守選池、輪詢與換頁保留展開、客戶儀表板、用量及帳務；0 JavaScript錯誤／0網外請求／0請求失敗，讀取前後50任務／170事件／171帳務筆數不變。原先啟動與中間進度2項屬獨立較早證據，不將R2整體失敗改稱通過。
- 首輪22題後401與第二批快照30秒逾時保留。實作修正有公開紅綠證據：SQLite正常伴隨檔消失仍可驗身分、同registry短交易串行、真無效憑證401／工程503、host-only observe逐件只驗一次原Core結果與一次預算報表、相同身分檢查不重包、UI同時只讀一份快照且舊登入回應不回填。受影響核心195通過／1既有lsof平台跳過；該平台單項後續實際重跑1通過。root的Monitor原證據、契約、期限及觀測回歸23通過；這些指定結果不替代全套。
- 限定範圍獨立複審通過，剩餘問題0：41／41來源檔匹配、11項回歸與5項UI邊界探針通過。原Q23確切原因未知；10.10秒只代表當次50題快照量測。原失敗、SDK報告、帳務、桌機及審查在 `runtime/ollama50-evidence-20261023-final/` 保存，索引同時保留既有263筆歷史證據。
- 本機桌機網址 `http://127.0.0.1:18773/`，資料 `runtime/ollama50-20261023-r2/data/`，原18767保留。完整操作及證據邊界見[50題文件](ollama50-simulation.md)。真實模型／供應商請求0；不提交、不做手機、推送、部署、刪除或重啟付費研究。
- 最終Monitor完整凍結gate **235項／12步通過**，Ruff、全部產品／QA JS、全套Monitor測試及sdist／wheel成功，Core與Monitor前後指紋不變；收據 `runtime/ollama50-evidence-20261023-final/full-verification/monitor/runtime/checks/7429cd7f773e/checks.json`。
- 核心完整收集2,105項，**2,055通過／50失敗／2警告**，整套gate未通過，沒有finalize或把目前交付指標改成verified。失敗均為SQL題庫／研究suite或凍結計畫不一致；現行suite `c829540e…`與表內 `8a3127cd…`不同，固定import到凍結來源後仍失敗。複製依賴環境另有舊editable路徑，已保存隔離注意事項，不能單憑此宣稱全部50項的根因。原始完整log、來源與nodeids封存於 `runtime/ollama50-evidence-20261023-final/full-verification/`。核心兩個既有Mock示範各5次呼叫另行通過，sdist／wheel離線建置另行成功，不替代失敗整套。
- 固定17份核准歷史副本排除，沒有增加排除或修改SQL／檢查器。較早完整檢查因後續修正輸入而明示中斷130；兩個新目錄因離線快取不足而失敗，之後重用與目前鎖檔完全相符的已安裝環境，仍由原`--locked --offline`檢查重新驗證並重綁凍結project，無網路下載。原R7收據與263筆歷史索引保留，核心全工作區維持未完全驗證。

## 2026-10-23：桌機帳務與尚未完成的主線接線

**主線實際 MRC 尚未接到 Monitor。** 已完成的 Core／TaskService／IdentityStore 接線驗收限於合成環境與介面；`18767` 目前仍用獨立 MockTransport 任務，主線實際 runner、客戶端與共用帳本尚未由同一服務組裝。OpenRouter 真實金鑰用量是獨立唯讀資料，不能當成實際 MRC 任務或客戶計費已運行的證據。接線缺口及下一個必要步驟見[交接紀錄](main-thread-handoff.md)。

- 本輪帳務功能：內部 USD 帳本、版本化全域／客戶倍率、開源模型輸入／輸出每百萬 token 售價、任務接受時固定費率、冪等入帳／扣款、缺證待核與私有原始證據。頁面提供計價明細；預設不扣款，啟用前任務不補扣，原核心成本及派送預算不回寫。
- 最後完整 Monitor gate：**206 passed、10步全數成功**，Ruff、全部產品／QA JavaScript語法、全部 Monitor 測試及離線 sdist／wheel；測試前後 Monitor 與注入 core 指紋不變。原始收據 `runtime/checks/3a7b50d6365d/checks.json`；前一輪197項／10步紀錄 `runtime/checks/21065421343e/checks.json` 保留。未重跑整個核心工作區套件，不改稱核心全面全綠。
- 桌機1280×800：`output/playwright/billing-20261023-r4/billing-browser-report.json` **7項通過、0 JavaScript錯誤**；包含草稿保存、售價設定、入帳回應遺失後完整reload沿原編號重試、原客戶消失與sessionStorage不可用時阻擋、重複核帳不重複扣款、客戶無管理權限。1個requestfailed是刻意丟失已成功入帳的回應，保留明列。
- 原網址與真實帳務顯示：`output/playwright/billing-live-preview-20261023-r2/preview-report.json` 通過；手動讀 `/key` 回應200，原文雜湊可核對，累計／當期用量及重設週期分開，無管理金鑰的帳戶範圍維持未知。合成客戶帳本保持停用與0筆；本輪沒有真實模型請求或客戶扣款。來源只在明示動作讀取，普通頁面與輪詢不查供應商。
- 獨立唯讀[複審收據](../../.scratch/cloudyfan-monitor-billing-20261023/review/review-final.json)為 `PASS_SCOPED_CODE_REVIEW`、剩餘finding0；初版2項P2各由原探針確認修正。串流期限依資料塊檢查，不能宣稱嚴格牆鐘搶佔；完整限制見[帳務文件](billing.md)。
- 依使用者指示複製核心 `.env`，0600、Git忽略、內容核對；金鑰未輸出或提交。核心其他研究變更、既有UI與兩張截圖保留。沒有手機工作、金流收款、推送、部署、commit或刪除；原R7證據保持歷史範圍，不覆寫。

本輪新程式未提交，私有過程證據在 CloudyFan `.scratch/cloudyfan-monitor-billing-20261023/`，本機預覽使用 `runtime/billing-preview-20261023`。完整操作與價格規則見[帳務說明](billing.md)。

## 2026-10-23：本輪工具修正提交

- 四項驗收工具問題與P3共用步驟已完成；核心工具提交為 `e0d570a2ba371b00e6068fb949cb8594f29a1ac4`，Monitor本輪提交包含本節及[主線交接](main-thread-handoff.md)。原工作區的其他研究變更及既有截圖保留未提交。
- 從暫存區匯出的提交內容通過核心快速檢查／13項工具案例、Monitor快速檢查／24項工具案例；實際提交仍執行既有密鑰掃描與快速檢查。
- R7完整凍結驗收的2,058／113項及兩軸零剩餘問題仍限於其原輸入；原收據SHA256為 `abcbc38ea8c96553fb3c796fd26dbdec2c590eccc2b00b3b9d17a9902214df14`。本輪開始時目前工作區因後續核心研究變更而為 `stale`，不把歷史驗收改稱當前整個工作區通過。
- 詳細範圍、原始證據與續接命令見主線交接及 CloudyFan `.scratch/cloudyfan-monitor-local-commits-20261023/`。本輪只做本機提交，沒有推送、部署、模型／來源請求、手機工作或刪除。

2026-10-22：完整方案已獲使用者確認並授權實作。手機端介面製作與測試已依最新指示停止；接續範圍為桌機、本機接入及驗收收尾。規格 [Issue #20](https://github.com/alexwang0315/cloudyfan-routing/issues/20) 已同步排除手機，ready-for-agent 標籤及本文已回讀確認。

## Issue #20 implement-spec 最新檢查點

- #21 身分與 #22 四模式已合入並結案；#23／父規格的必要檢查已在核定範圍通過；審查修正已合入e5fbd6e並同步，53份交付檔案一致、13份產品指紋不變，Issue20/23 已結案；最新工具驗證狀態由 `python3 scripts/acceptance.py status` 查核。
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
