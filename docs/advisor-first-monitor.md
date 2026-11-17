# Monitor：顧問先答、換池紀錄與經驗共用設定（#80）

依據：`alexwang0315/cloudyfan-routing` #80（總表 #71）、ADR 0034–0036。測試對象是 MRC 整合分支 `claude/mrc-advisor-71` 的 `49bb34c`（含 #72、#73、#75、#77、#78）。

## 白話說明

MRC 新增了三個機制：路徑二（開源顧問先看題目，能答就答，答不了就指定池內模型）、依體檢自動換池、契約經驗庫。這張單讓操作員在 Monitor 上看得到、管得到它們：

- **任務頁**：走路徑二的任務，展開明細會多一段「路徑二：顧問先答」，列出顧問是自答、交出還是未完成，有效建議（依順位）與作廢原因，每次作答的來源、池內設定、驗收結果與費用，以及每筆呼叫的角色（顧問／裁判／作答）。費用分兩個總數：**正式成本**（不計設定中明示開源的顧問／裁判）與**研究全額**（照收據全數列出）。
- **換池紀錄頁**（側欄「換池紀錄」）：目前快照與各池分群、每筆換池紀錄的前後快照、移動與理由、體檢分數與信賴區間、操作者；可「回復上一份快照」與開關「自動換池」。
- **客戶端設定**：路由模式下拉選單多了「路徑一加強版」與「路徑二：顧問先答」，只有主服務註冊了對應契約才可選；「連線客戶端」頁多了每個客戶端的經驗「貢獻」「使用」開關。

回復、總開關、經驗開關與路由模式都只在 `/monitor/` 底下，需操作員 token；客戶端 token 一律 401。每次變更寫進 Monitor 設定紀錄（`auto_pool_rollback`、`auto_pool_auto_switch`、`experience_settings_changed`）。

## 已做

| 項目 | 位置 |
| --- | --- |
| 路徑二任務明細投影：`trace.advisor`（`advisor_mode`、`incomplete_reason`、`end_reason`、`score_snapshot`、`recommendations`、`voided`、顧問呼叫、`costs`），各次作答加 `source`／`setting`／`pool_tier`／`recommendation_rank`，呼叫加 `role` | `src/cloudyfan_monitor/tracing.py` |
| 路由模式：`ROUTING_MODES` ＝ 既有四模式 ＋ `cloudyfan_plus`、`advisor_first`；可否選用仍由註冊契約決定（`mode_available`） | `store.py`、`gateway.py` |
| 換池紀錄 API：`GET /monitor/auto-pool`、`POST /monitor/auto-pool/rollback {snapshot_id}`、`POST /monitor/auto-pool/auto-switch {enabled}` | `auto_pool_panel.py`、`app.py` |
| 經驗開關 API：`GET /monitor/experience`、`POST /monitor/experience/settings {client, contribute?, use?}` | `experience_settings.py`、`app.py` |
| 畫面 | `static/advisor.js`、`static/advisor.css`，掛在 `trace.js`、`app.js`、`index.html` |
| 合成環境：`build_demo(..., advisor_first=True)` 加上開源顧問／裁判設定與路徑二契約；預設行為不變 | `demo.py` |

設計重點：

- 顧問的回答草稿與每組建議的自由文字理由**不進 Monitor**（可能引用題目內容），只顯示設定識別、層級、順位與作廢原因代碼。
- 回復時要帶上畫面看到的 `snapshot_id`；快照已被改過就回 409 `auto_pool_changed_reload`，避免回復到操作員沒看到的版本。MRC 拒絕回復（沒有紀錄或上一份已過期）時回 409 `auto_pool_rollback_unavailable`。
- 經驗開關直接寫 MRC 經驗庫自己的檔案（`ExperienceStore.open` → `set_client_settings` → `save`），每次寫入前重讀檔案；Monitor 不呼叫改寫器。
- 主機要啟用這兩頁，需在組裝 App 後指定 `app.auto_pool = AutoPoolPanel(AutoPoolController(root))`、`app.experience = ExperienceSettings(path)`；沒指定時 API 明示 `configured: false`，POST 回 409。

## 已測（離線，MockTransport）

`tests/test_advisor_monitor.py`（10 項）：用 MRC 真正的 `Core`（路徑二）產生結果後檢查任務明細投影（自答失敗改派、作廢原因、正式／研究費用、私有理由不外流）；顧問未完成；路徑一明細不變；經 Monitor HTTP 把客戶端改成路徑二、建題、任務頁看到顧問決定與費用（自答與兩次改派兩種情境）；加強版未註冊時不能選；換池紀錄內容、回復、過期回復被擋、總開關、客戶端 token 一律 401、設定紀錄；經驗開關寫入後 MRC 重新開檔讀到同樣設定；未設定時明示 `configured: false`。

## 仍未知／未做

- **路徑一加強版（#79）尚未合併**：Monitor 認得 `cloudyfan_plus`，但沒有 MRC 契約可註冊，所以畫面上不能選。
- **MRC 的建立時路由建議規則只認既有四模式**（`task_recommendations.context` 的模式白名單）：主機註冊路徑二契約時，Monitor 的「特徵 v2 ＋ 觀察性建議 v1」版本無法建立，`ObservationPolicy` 改為只提供不含建議的版本。要恢復需 MRC 擴充白名單（本單不改 MRC）。
- 裁判（#74）尚未合併：明細已能顯示 `judge` 角色與裁判費用，但沒有實際裁判呼叫可測。
- 經驗庫是單一 JSON 檔、沒有跨程序鎖；Monitor 與 MRC 同時寫入可能互相覆蓋（MRC 文件已註明）。
- 換池紀錄只顯示 MRC 已寫入的紀錄；體檢與評估本身不從 Monitor 觸發。
- 預覽（`./scripts/preview`）沒有接上自動換池與經驗庫，兩頁會顯示「尚未接上」。畫面另以一次性合成伺服器（`build_demo(advisor_first=True)` 加上合成換池紀錄與經驗庫檔）在桌機瀏覽器操作過：換池紀錄顯示與回復、經驗開關切換、路由模式選單（加強版顯示無已註冊契約）、路徑二任務明細，主控台無錯誤；這不是正式驗收紀錄。
- Monitor CI 指定的 MRC 版本在 GitHub repository 變數 `CLOUDYFAN_CORE_REF`／`CLOUDYFAN_CORE_INPUT_SHA256`；MRC 本機沒有 remote，本單沒有、也不能更新這兩個變數，需在 MRC 推上遠端後另行同步。
