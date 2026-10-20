# 主規格 #1 實作索引（2026-10-18）

範圍：[CloudyFan Routing Core 主規格 #1](https://github.com/alexwang0315/cloudyfan-routing/issues/1) 的 T01～T12。這份索引記錄本機程式與離線驗收；GitHub Issue 狀態以 tracker 為準。T13～T21 屬其他部門，不在本套件實作範圍。

🔵 **下表是主規格初次交付的歷史快照，不是目前未完成清單。** 後續已完成供應商費用驗證器、完整元件上界與 RG-04 v2、RG-05 Token 身份／資料保留整合，以及固定 Ollama SDK／text-v2 互通。最新版本 `f1dd57c` 的完整離線證據為 944 項通過；請以[放行索引的三欄狀態](release-readiness.md)與[歷史驗證紀錄](verification.md)判斷現況，不沿用下表 T04／T07／T08 當時的缺口。這些後續交付仍不等於真實供應商、部署或硬體資格已核定。

初次交付路由為「開源首答 → 公開驗收 → 低／中 → 必要時中／高 → 唯一修正」。2026-10-20後續經確認的新任務改採[順序式路由](sequential-routing.md)，每次選模後實際作答驗收，CloudyFan最多三次作答；不覆寫本表的歷史交付或實驗結果。分數、分類器選項分布、非空回答或 HTTP 成功均不能替代公開驗收。

## 可使用能力與驗收邊界

| Ticket | 本輪交付與文件 | 離線驗收／正式缺口 |
|---|---|---|
| [T01 #2](https://github.com/alexwang0315/cloudyfan-routing/issues/2) | [版本化公開診斷](public-diagnostics.md)：欄位、來源、大小與唯一修正；不可安全重播的工具效果停止 | `Core.run` 與注入驗收器證明診斷邊界；驗收器語義正確性與自由文字來源仍由應用負責 |
| [T02 #3](https://github.com/alexwang0315/cloudyfan-routing/issues/3) | [模型池資格包](qualification.md)：單一來源原生尺度、精確設定、核定與阻擋報告 | 合成資格包連接三池與 Core；沒有核定任何真實領域、門檻或模型 |
| [T03 #4](https://github.com/alexwang0315/cloudyfan-routing/issues/4) | [快照更新與操作紀錄](score-update-operations.md)：更新預覽、差異、固定快照與失敗保護 | 合成更新／競態／續跑驗證；沒有安裝背景同步或發送通知 |
| [T04 #5](https://github.com/alexwang0315/cloudyfan-routing/issues/5) | [預算與完整帳務](budget-admission.md)：有效授權、整流程預留、模型及工具事件、未知停止 | SQLite 競態與 MockTransport；沒有供應商實際硬上限驗證器，預設拒絕付費執行 |
| [T05 #6](https://github.com/alexwang0315/cloudyfan-routing/issues/6) | [本地文字後端](local-backend.md)：固定版本／量化／引擎／位置，與雲端帳務分離 | Loopback 協定 fixture，不啟動真實引擎或下載模型；本地零模型費契約不等於主機免費 |
| [T06 #7](https://github.com/alexwang0315/cloudyfan-routing/issues/7) | [執行位置政策](execution-policy.md)：生成、分類與工具前的 local_only 檢查 | 注入後端與出站觀察證明政策路徑；不能證明管理者位置聲明或實體網路隔離 |
| [T07 #8](https://github.com/alexwang0315/cloudyfan-routing/issues/8) | [私有 HTTP 任務入口](http-task-api.md)：註冊契約、提交／狀態／結果／續跑、身份與費用來源 | WSGITransport 與 MockTransport；正式認證、保留政策、工具隔離及部署仍待完成 |
| [T08 #9](https://github.com/alexwang0315/cloudyfan-routing/issues/9) | [Ollama 文字子集](ollama-text-protocol.md)：查詢、單輪非串流文字、明確不支援項目 | 固定 consumer fixture，並非真實 Ollama 客戶端互通；G02 的實際客戶端驗證仍未完成 |
| [T09 #10](https://github.com/alexwang0315/cloudyfan-routing/issues/10) | [有界排隊](bounded-scheduler.md)：容量、並行、配額、取消與使用者隔離 | 合成雙呼叫者與重啟；未聲稱商業 SLO。正式有界入口只可暴露 QueueHTTPApp，不能旁路同步入口 |
| [T10 #11](https://github.com/alexwang0315/cloudyfan-routing/issues/11) | [實驗預檢與凍結](experiment-preflight.md)：歧義審查、資格、校準／holdout 隔離與輸入雜湊 | 合成計畫及執行綁定；人類簽核仍必要，計畫本身不授予付費權限 |
| [T11 #12](https://github.com/alexwang0315/cloudyfan-routing/issues/12) | [比較報告](comparison-reports.md)：品質門檻、配對／未配對、完整成本、CDR、延遲與未知 | 合成唯讀證據；品質不等或未知時僅支出差異，沒有新實測節費率 |
| [T12 #13](https://github.com/alexwang0315/cloudyfan-routing/issues/13) | [本地量測流程](local-benchmark.md)：載入／切換、TTFT、資源與完整成本欄位 | 假時鐘、成功／OOM／中斷；Mac Studio 容量、功耗、並行與真實延遲皆未驗證 |

## 共用契約與修正

`Core.configuration_descriptor()` 提供一份行為設定快照；Core manifest、完成結果讀取、HTTP 註冊、預算與量測層沿用它，再加入各層自己的欄位。它含完整設定供私有凍結使用，不是可直接傳給一般使用者的內容。

HTTP、Ollama 與排隊結果另投影安全來源欄位：每次生成及分類的實際模型／供應商、固定模型版本／強度／本地設定、可取得的評分來源／版本／快照／policy ID。核帳後使用同一份驗證過的收據刷新身份與成本；未知保留 null，不重送原請求、不公開 capability prose、私人 prompt 或共享帳戶資料。

`ReceiptStore` 僅共用檔案、鎖、固定寫入與讀取；`Client` 保留 OpenRouter 專屬核帳，`LocalBackend` 不再繼承雲端 transport。既有模型呼叫、response-before-parse、未知費用停止與續跑邊界保持原來的語義。

## 可採信的交付結論

本輪完成的是可離線驗證的 routing core 與直接配套，不是已部署 SaaS 或新的商業節費證明。詳細命令與測試結果見 [verification.md](verification.md)。真實模型資格、來源帳戶存取與使用條款、供應商可強制費用上限、應用驗收語義、正式身份與保留政策、真實客戶端、硬體容量與服務部署仍各有獨立門檻；沒有以 fixture 通過取代這些證據。

本輪沒有新增付費模型／分類請求、模型下載、硬體操作、服務部署或原始碼上傳；歷史實驗與原始帳本未改動。
