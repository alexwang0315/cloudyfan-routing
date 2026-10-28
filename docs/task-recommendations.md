# 建立時路由建議

操作員可在「任務與升級」的呼叫明細查看建立時保存、尚未採用的路由建議。建議不改任務類別、候選資格、選模、原生成／分類次數或費率；原 Budget 帳本仍決定實際費用。此頁沒有採用或改派操作。

## 明示啟用與停用

主機組裝的 `assemble_monitor(..., creation_features=..., creation_recommendations=...)` 接受受控設定。特徵政策與建議政策必須各自明示；只有建議設定不能替原本停用的特徵開啟擷取。`creation_features` 是原固定特徵 policy bundle，Monitor 自己接真 Store binding port；例如選擇 `local-deterministic-v2`、`creation-feature-data-v1` 及其固定 bounds。建議政策可由公開的 `cloudyfan_jev.task_recommendations.enabled_policy()` 取得，規則／資料政策分別是 `route-recommendation-rules-v1`／`creation-recommendation-data-v1`。

如需精確成本比較，另在建議設定加入 server-only `calibration_sources={"data_kind": "observed_calibration" 或 "offline_fixture", "public_root": 明示的絕對公開來源目錄}`。來源仍須對應原凍結 plan 的全部條件、configuration／execution／validation 版本、partition、原始 SHA、效期、分類及完整後續成本；傳入 `trusted=true` 等旗標沒有核准效果。未知來源種類只保存固定未支援原因，不讀來源或回顯該種類及目錄。未知 policy／不合法設定不能被提升為已支援。

主機可用 `service.creation_recommendations = None` 停止新建議，或用原 `service.creation_features = None` 停止新特徵擷取。冷啟動未明示 producer policy 時，Monitor 只接 reader；不從當前停用設定推定舊任務曾停用。停用後仍可讀原先驗證並保存的觀測。這些都是受控主機設定，沒有新增 HTTP request／runner descriptor 欄位或設定頁面。

## 建議與未知的意思

| 建議種類 | 解讀 |
| --- | --- |
| 沿原路由觀察 | 缺證維持未知；完整條件下也可顯示淨節省估計為正或區間不確定，兩者均不改派。 |
| 可比較略過分類 | 只有完整、精確且適用的成本校準，在所有必要條件下淨節省上緣均不超過原安全餘量，才給比較建議；尚未略過分類。 |
| 需補能力與上下文證據 | 分開顯示精確 input tokens 未知，以及明示上下文需求不是本次用量；不把字元數換成 tokens、困難度或模型通過機率。 |

分類淨節省為直接完整成本減去分類及其完整條件後續成本。固定比例餘量沿原政策使用直接成本**下緣**，而非任務實付金額。未滿足完整條件、效期、執行契約或來源驗證，不能說成已證實不值得分類。來源分布不是答案通過率。

原 `fixed_high`／`low_first`／`open_source_first` 沒有此分類流程，分類比較標為不適用。`not_provided` 表示舊任務沒有當時建議；`unavailable` 表示保存證據無法確認；`unsupported` 表示已知政策下有未支援的資料種類／範圍。未知與不適用各有固定原因，不以零成本或後來的答案補推。

操作員可看已封存的來源種類、來源指紋、條件數及有號 USD 淨節省區間。客戶只讀自己任務的安全種類、原因與版本／時間／摘要，不包含完整比較 basis；其他客戶無權讀取。所有字串原因採固定白名單。頁面的原因和來源 details 支援 Enter／Space，並沿原輪詢保留展開與焦點。

## 保存與適用時間

建立時只用原合法輸入、明示需求及受控凍結契約，不讀任務結果、答案、其他 arm、後來 Health 或執行後安全 cache。受控 Queue 路徑同時要求首次提交成功的 Monitor binding、新 accepting 與新 Core job；原同步路徑沿其首次建立界面。只由首次建立者在原鎖及任何模型呼叫之前嘗試一次保存及獨立 seal。重送、GET、重開與 recovery 不補算、不重簽；接受中斷而缺可信錨點時回無法確認。

原特徵 DTO／v1-v2 政策及 16 KiB 舊紀錄保持原格式。新任務使用 `creation-feature-adoption-v2`、`creation-task-observation-v2` envelope、`creation-routing-input-v2` 與 `creation-routing-context-v3`，共用同一 record SHA、原 Core anchor 和 Monitor 一次 CAS。Context v3 分別保存分類校準與候選時間界線：snapshot／catalog／qualification 與 provenance 使用原半開窗口；QCS source metrics 年齡上界保留含等號語意。所有界線以原 accepted time 判斷。舊 context v1/v2 仍可解讀，不用新規則改舊檔。

原記錄／anchor 的檔案及父目錄 fsync 完成後才 seal。跨兩個儲存並非單一原子交易；檔案、同步或 auxiliary SQLite 失敗可使觀測無法確認，但不應改原合法任務或費用。這些本機故障測試不代表實際斷電驗證。

原原文存取 30 天與安全 metadata 365 天政策不變。到期後客戶沿原 410；操作員被動查詢只用可驗證的安全保存紀錄及原 Budget 金額，不重新打開 job／manifest／intent 或校準來源。未知費阻擋清理時沿原延保規則保存，不能在 365 天自動抹成零或補算。原明示 maintenance preview／財務核帳 POST 仍依原權限與保留財務資料範圍；被動讀取的零原文讀取驗證不涵蓋這些原授權動作。

## 有界工作與驗證入口

每個新任務新增的 Python 計算、序列化、hash、驗證與萃取，共用原 50 ms 純計算 budget。安全 context 上限 64 KiB、候選摘要 12、可達條件組合 128、建議 8 KiB／3 種、每種原因 4、新 envelope 96 KiB；超限不截取成完整證據。OS／SQLite I/O 不包含在這個純計算承諾。

校準來源僅在受控 host bootstrap 讀一次：先檢查所有 required variants 的非空聲明、raw SHA 對應、同路徑 SHA 一致及完整 union，再開檔。最多 16 個 regular、單硬連結檔，每檔 256 KiB、合計 1 MiB；整個 union 實讀最多再加一個必要偵測 byte，成長即拒絕。逐層 dir-fd／NOFOLLOW／NONBLOCK 保持公開根目錄約束，拒絕 symlink、FIFO、device、私人／authoring／evaluation／憑證及不分大小寫 `.env` 路徑。相同批有界 immutable bytes 交原精確 validator，不能先限讀再用舊 helper 無界重讀。GET／poll／reader 重開不建立 context 或重新開來源。

成本 adapter 只支持已核對的原生 `BackupCategoryCore` 精確型別，並透過原公開 execution descriptor 比對實際公開驗收版本；未支援的子類或不相符版本維持未知，不改原 Queue 準備守衛。原 selector helper 未傳 `source_bytes` 的 caller 保持舊語意；本功能只走顯式有界 bytes 分支。

`tests/test_task_recommendations_http.py`、`tests/test_task_recommendations_integrity_http.py` 是真 loopback／真 Core、Store、Identity、Budget、Queue、Health 加全 MockTransport 的針對驗證。少量 public host-library／HTML renderer 類型與邊界測試另外標明，不替代 HTTP／桌機。`scripts/qa_task_recommendations_desktop.cjs` 配合 `qa_task_recommendations_http.py` 以 explicit source manifest 執行真正 headed Chrome；逐條核對 HTTP reference SHA／size／唯一名稱、純讀不改狀態、隱私與 own PID 退出。完整專案 gates 及最後來源收據另行記錄，不由此說明推定通過。

`offline_fixture` 只支持實際全 MockTransport 的合成驗證。測試中的 `observed_calibration` schema 也明示是合成來源，並非真實觀測或已實現節費；來源 SHA 本身也不證明獨立性、模型資格或實際成效。
