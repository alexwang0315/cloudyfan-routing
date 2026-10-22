# 主線接入驗收清單

本清單由[主線接手文件](main-thread-handoff.md)中使用者已確認的Q1～Q12推導，完整方案已獲使用者確認；本頁保留驗收條件，實測結果另列。2026-10-22 使用者要求停止手機端介面製作與測試，故新增畫面驗收限於桌機。

## 驗收環境與證據邊界

- 使用新的獨立本機fixture資料目錄、真實Core／TaskService程式及模擬上游、虛構模型與合成費用；不載入真實模型憑證、不改主線原始帳本、收據或研究執行狀態。
- 真實本機HTTP測試只開放該測試的loopback服務與port，其他網路一律拒絕；模型仍使用MockTransport。測試須保留實際呼叫紀錄，包含故障路徑。
- 真實來源GET另作獨立步驟，只准固定來源路徑與GET、不跟redirect；驗收設定固定為3個主來源GET加最多12個模型端點GET，包含失敗在內最多15次嘗試，整輪最長60秒、單次最長15秒。不發送模型作答、分類或核帳請求，不自動publish／activate。
- 既有35項Monitor回歸及桌機／手機證據可作基礎；新接入仍需執行受影響檢查及下表情境。既存檢查JSON的零呼叫欄位是常數，須與網路限制、MockTransport及實際紀錄共同查核。
- 來源GET成功只能證明該次來源連線與schema，不建立模型／費用資格，也不解除主線未知費用停止。

## 必須通過的情境

| 項目 | 驗收結果與應保留的證據 | 決策依據 |
| --- | --- | --- |
| 兩客戶端身分及權限 | A、B任務與統計隔離；client不能使用管理入口；單操作員可改各client設定。換憑證後舊憑證失效，原client、任務及統計持續存在。 | Q1、Q3 |
| 同步任務接入 | 真實本機HTTP經Monitor控制器進入TaskService；任務固定client、run、category、pool、mode與contract，未經入口的舊任務不自動接管。 | Q2、既有凍結規則 |
| 四模式與候選視圖 | 保留23類×四欄×三位置的候選視圖及四模式控制；逐類別×模式列出契約註冊狀態，缺契約組合拒派且零模型呼叫。模擬契約不列作真實執行資格。 | Q7、原首版範圍 |
| 設定切換與舊任務 | 全域／client覆寫及pool切換只影響新任務；原任務重連、重啟後仍綁原契約，缺舊契約時明確拒絕。 | 既有凍結規則 |
| 服務容量 | A的模擬長任務佔用槽位時，B的新任務立即429且零上游呼叫；A完成後B不會自行啟動。同一服務不同worker／程序接收A、B時也共用一件容量，不能僅靠進程內鎖通過。 | Q8 |
| 長任務中的可觀測性 | 不釋放模擬上游完成訊號前，任務狀態、監控頁與管理操作已能回應；不被retention鎖阻塞，也不把執行中誤標為工程中斷。 | Q8及運作監控需求 |
| 重送與明示接續 | 完成、執行中及確定中斷的相同client／run重送，均不觸發新模型請求；只有明示resume可依原條件接續。改內容、過期或退休不能另建或復活原任務。 | Q9 |
| 共用帳務與未知費停止 | A產生未知費用後，同付款帳戶的B也停止新派送；帳戶總帳包含未經Monitor入口的歷史支出，按資料能歸屬的部分分列client，未歸屬及未知部分如實保留。 | Q4、既有帳務規則 |
| 任務30天邊界 | 到期前可依原條件讀取／接續；恰到30天及重啟後明確過期／410，重送不重設起算時間，不被投影為一般工程中斷。 | Q5 |
| 歷史統計與保存 | 結果到期後仍保留已確認的歷史費用及統計；原文滿30天只進清理審查，帳務等至少365天，未核帳或有效引用延保。一般紀錄自建立起算，共用版本證據自最後一次任務使用起算；查詢／重送不重設期限。只產生精確清理preview，未經逐項批准不刪除。 | Q6及方案期限口徑 |
| 手動來源更新 | 僅操作員可手動更新；先取得差異再發布候選，執行模型池啟用仍獨立。更新時間、來源及完整度可查，不以頁面每5秒輪詢當作來源刷新。 | Q10 |
| 來源正常與失敗 | 主要來源HTTP／網路／schema故障維持舊版；主要來源成功時，個別端點HTTP、網路timeout、JSON／schema解析失敗及未查項目可形成明示「部分更新」的候選，保留其他成功資料，成功／失敗／未查數目與未知欄位符合紀錄。實際嘗試數包含失敗，來源證據不包含憑證，發布不授予執行資格。 | Q11、既有有界GET |
| 客戶端斷線 | 真實本機socket斷開後，伺服器正常時已接受任務繼續；執行槽位不因客戶端斷線而提早釋出，以原client／run查詢可取得狀態或結果。程序真正中斷及重啟後不自動續跑，原未知費／期限限制維持。不以WSGITransport測試代替。 | Q12、Q8、Q9 |
| 瀏覽器操作 | 桌機實際檢視新契約矩陣、忙碌、到期、歷史統計及來源完整度；保留畫面與操作結果，確認所有資料均標示模擬／待核定的正確範圍。 | 原本機驗收要求及Q5～Q8 |

## 完成判定

完成必須有：受影響檢查通過、上述本機HTTP與瀏覽器證據，以及該次真實來源GET的成功與schema查核。任一項缺少時，明列未通過／未執行，不將既有35項回歸或畫面展示稱為本次主線接入已完成。

此完成判定僅指本機接入及限定來源查核；真實模型／費用資格、服務部署、付費研究、推送與正式降本率維持既有邊界。本次訪談未授權實際資料刪除，也不重審S01～S08或恢復已終止的低池專測。

## 2026-10-22 實測結果（桌機範圍）

使用者後續明確停止手機端製作與測試，手機不列為本次完成條件。以下均為本機接入證據；不宣稱整個核心工作區或正式模型資格通過。

| 驗收項目 | 本次結果與證據 |
| --- | --- |
| 套件完整檢查 | **76 passed**；Ruff、JavaScript 語法及離線 sdist／wheel 通過。重開機後使用目前核心重跑成功：[checks.json](../runtime/checks/925f741a42ce/checks.json)。 |
| 核心受影響整合檢查 | 指定HTTP、retention、新並行／觀測、identity及scheduler六個模組，共 **119 passed**；兩個模擬示範與離線建置通過。這是明確指定範圍，並非全套：[範圍說明](../../cloudyfan-jev-core/runtime/monitor-integration/post-reboot-scope.json)、[原始檢查](../../cloudyfan-jev-core/runtime/checks/2c9f1398b3ce46dbacc4c8d4d62224b5/checks.json)。 |
| 身分與授權 | 真正IdentityStore換憑證後保留原任務、舊憑證401、scope／停用／loopback限制；原主服務回呼與身分有效性同時成立。停用client仍保留操作員可查的公開歷史及費用歸屬，client結果／resume維持401，恢復後重新驗原證據。 |
| 真正類別核心 | 兩client經HTTP、TaskService、BudgetedCore、RunBoundCore進真正CategoryCore；固定workload由凍結契約補入，client自填被422拒絕。共享合成帳本US$0.02；重建／讀取／重送／完成後resume不新增呼叫，原manifest／job位元組不變。見[test_category_host_integration.py](../tests/test_category_host_integration.py)。 |
| 服務容量及重送 | 7項真正loopback HTTP測試涵蓋跨worker／程序單名額、立即429、不暗中排隊、同內容重送只讀、改內容拒絕、顯式resume、socket斷線後繼續及SIGTERM後不自動重跑。見[test_local_http_integration.py](../tests/test_local_http_integration.py)及上述套件檢查的7份http-evidence JSON。 |
| 到期、保存及帳務 | 到期恰30天／410、重建不延長、執行中到期、歷史pass與已知費保留、至少365天、resume更新版本最後使用時點、有效引用／未知費延保及只讀preview均有回歸；沒有實際刪除。未知費跨client停機，帳戶歷史及未歸屬金額不被忽略。 |
| 桌機操作 | **8項操作通過**，1280×800：長任務觀測、全域／個別模式及還原、23類×四模式矩陣（4個合成註冊、88個缺額）、12個候選缺額、部分來源差異／發布／不自動啟用、主來源失敗保留、到期／歷史／帳務及釋放後原任務完成。6次mock模型呼叫保持6次，頁面／管理未新增派送。見[桌機report](../output/playwright/main-thread-20261022/desktop-report.json)。 |
| 桌機異常呈現 | 模擬來源先有5次GET嘗試，4成功／1失敗／1端點未查；發布只改候選，路由版本保留。第二輪主來源503保留候選；1筆預期HTTP503 console resource訊息，0筆JavaScript page error。這些來源呼叫全部是MockTransport。 |
| 重開機後預覽 | 原程序確實不存在後，重新啟動18767，保留原12件合成任務、12件通過、0未知；桌機重新載入成功、無page error。見[重啟report](../output/playwright/main-thread-20261022/post-reboot-report.json)。 |
| 真實來源GET | 獨立一次窗口15次GET皆HTTP及schema成功，約30.62秒；23類、143個具名位置，24個模型端點未查，故明示部分更新。既有政策為exploratory_unvalidated；未發布或啟用、不建立資格。15份原回應雜湊已重新核對。見[來源report](../runtime/main-thread-acceptance-20261022/source-get/report.json)。 |

[機讀證據索引](evidence/monitor-main-thread-20261022.json)含目前Monitor來源指紋、各檢查、網路證據與範圍。桌機畫面：[候選部分更新](../output/playwright/main-thread-20261022/desktop-source-partial-published.png)、[到期與執行中](../output/playwright/main-thread-20261022/desktop-tasks-expired-and-busy.png)、[重開機後總覽](../output/playwright/main-thread-20261022/desktop-post-reboot-preview.png)。

## 核心整個工作區仍未完成的檢查

重開機前兩輪大型檢查都未結束，而且已出現失敗；一般檢查至93%，排除既有「 2.py」副本的檢查至86%。它們保留為中斷／未通過，沒有補寫成功、刪除副本或修改SQL來湊全綠：[一般log](../../cloudyfan-jev-core/runtime/monitor-integration/required-integration.log)、[排除副本log](../../cloudyfan-jev-core/runtime/monitor-integration/canonical-integration.log)。原checks.json尚無finished_at，因此沒有可靠的最終全套通過數／失敗數。

排除副本的中斷紀錄可對應三項SQL測試：catalog mutants、compound ORDER expression、rotating fifty capture。呼叫鏈查核未進入本次retention／TaskService.result修改；compound單案例另重跑1 passed。完整失敗trace未取得，根因仍未確認，不概括宣稱全是舊副本或環境問題。這些全面檢查與其他routing-v2-s50-integration工作分列，未覆寫其狀態、SQL規則或原研究帳本。

本次桌機與本機接入的指定驗收已取得通過證據；**核心全工作區整體驗收尚未完成**。沒有正式部署、供應商能力／費用資格核定或真實降本率主張。
