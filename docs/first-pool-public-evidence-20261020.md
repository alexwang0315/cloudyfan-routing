# RG-02／RG-03 公開補證（2026-10-20）

本輪只讀既有資料及免費公開來源，沒有模型 POST、帳戶／金鑰操作、供應商聯絡、部署或推送。原始公開 GET 回應、失敗與 SHA256 留在忽略目錄 `runtime/pool-public-evidence-20261020/`；版本控制只保存去敏[證據矩陣](evidence/first-pool-public-20261020.json)、三筆受控匯入及新候選包。既有 profile、14 筆歷史分數、SQL／RG-01、正式 registry 與付費實驗結果不變。

## 這次實際補齊了什麼

| 證據層 | 本次確認 | 仍不代表 |
|---|---|---|
| API 目錄身份 | 三個閉源家族的 canonical_slug 與原日期版一致；指定 OpenAI 標籤及端點名稱一致。 | 日期版請求一定可用／不可變、帳戶有權使用，或榜單真的測了此日期版。 |
| 公開能力宣告 | 四個生成候選皆宣告文字輸入／輸出及指定 reasoning effort；指定供應商端點容量均高於 8,192 要求。 | 獨立 SQL 正確性、資源限制遵守率、端點即時容量或可靠性。 |
| 開源身份 | 官方 OpenAI 權重庫固定 revision、Apache-2.0、MXFP4 配置及 131,072 位置上限可追溯。 | BaseTen 的 `fp4` 等同該 revision／MXFP4，或已核定 Mac Studio／本地 engine。 |
| 評分來源存取 | 找到網站明示的公開 score 介面；同三個既定候選成功讀取，全部覆蓋 64/64。 | 穩定 SLA、可商用再散布授權、SQL 專用評測或 API harness 等效性。 |
| 分數／來源時效 | 保存新取得時間、score/catalog/題池／統計政策版本、每題樣本數與最新判分時間；重算總分相符。 | 每個樣本的執行日期，或把最新判分日期當作所有樣本的 evaluated_at。 |
| 資格接入 | 新證據綁到四個精確配置 digest，進既有 prepare-first-pool；不再缺 configuration_evidence 綁定。 | mapping／SQL 能力已核准、正式 current 已更新。新包仍 blocked。 |

## 精確模型／強度／供應商與容量

數字是本次公開目錄的**宣告值**，不是實測的可用記憶體或吞吐。8192 仍是原需求，不放寬。

| 角色 | 原固定 model_version | effort | 精確 provider tag | 宣告 context／最大輸出 |
|---|---|---|---|---|
| 低 | `openai/gpt-6-sol-20260922` | low | `openai` | 1,050,000／128,000 |
| 中 | `openai/gpt-6.1-sol-20260929` | high | `openai` | 1,050,000／128,000 |
| 高 | `openai/gpt-6-astra-20260903` | xhigh | `openai` | 1,050,000／128,000 |
| 開源首答 | `openai/gpt-oss-120b` | low | `baseten/fp4` | **128,072**／115,264 |

開源模型的總目錄是 131,072，指定 BaseTen 端點是 128,072；沒有以較大的總目錄數字替代實際供應商宣告。OpenAI 官方三個模型頁另列相同家族、強度與容量，但 Snapshots 只列家族別名，未獨立確認 OpenRouter 日期後綴的不可變性。Jev 分類器端點另確認目錄列 `typesafe/jev-1.13-20260917`／`typesafe`；不是對 Decisions 累計輸入或費用上限的保證。

官方權重庫 revision `b5c939de8f754692c1647ca79fbf85e8c1e70f8a` 的 README／config 已用固定 revision URL 重讀並與 main 回應核對一致，只取文字與 metadata，沒有下載權重。這是 upstream 工件身份，provider weights digest／engine 仍未知。沒有把 `fp4` 字串當成部署工件證據。

## 新評分資料：保留真實來源差異

候選在讀取前已固定為原 low／mid／high 三個設定；沒有掃榜挑更好看的分數，沒有用正式題答案選模。來源仍 CodexRadar、原生 150 分及 `<80 / 80～100 / >100` 邊界。新來源版本另立，舊 14 筆觀測不覆寫。

| 原固定角色／強度 | 新原生分數 | 題目覆蓋 | 總有效判分事件 | 實際納入 last3 的 reward 數 | 最新判分（UTC） |
|---|---:|---:|---:|---:|---|
| GPT-6 Sol／low | 34.181422 | 64/64 | 80 | 80 | 2026-10-20 01:01:26 |
| GPT-6.1 Sol／high | 99.073981 | 64/64 | 112 | **110** | 2026-10-19 08:30:48 |
| GPT-6 Astra／xhigh | 115.035351 | 64/64 | 74 | 74 | 2026-10-19 03:44:07 |

分數為每題最多最近三筆 reward 的平均，再對覆蓋題等權平均乘 150；已離線重算一致。中分的 112 個事件有兩筆不在當前 last3，不能全填成評分樣本；新匯入的 sample_count 採實際使用的 110。80／110／74 也不是獨立題數或同等信賴樣本，獨立題身份只有各 64 個。沒有把它們拿來計算 SQL 正確率。

原頁面的 `/api/v1/radar-bench` 回傳 **410 score_endpoint_retired**，本文與 catalog 明示改讀 `/api/v1/radar-bench-score`；三筆新介面均 200。舊失敗保留，不無聲使用 cache 或替換資料。回應另明示 `same_population_as_prior_policy: false`，新舊分數不當成同一受控樣本的升降趨勢，也不直接比較優劣。網站前端仍在重構，不能據此聲稱已完成穩定遠端 adapter。

每筆來源保留 retrieved_at；latest_graded_at 及 trend 的 graded_at 是判分時間，沒有提供所有納入樣本的執行日期，因此 evaluated_at **仍 null**。既有 `max_evaluation_age_days: 30`、48 小時來源／catalog、24 小時 snapshot 皆未放寬。新包在取得時補齊新來源存取／覆蓋證據；之後仍會自然過期，不是永久「已解除時效阻擋」。診斷來源新資料時應顯示 evaluation_date_unknown，不能填今天繞過。

## SQL 能力及存取權限仍缺什麼

- 四個生成設定的六項操作：filter、join、aggregate、window、set operations、resource limits，仍 **unproven**；公開編碼能力宣告不能升格為獨立 SQL qualification。先前五類跨領域校準不補這個缺口。
- 查過較早 SQL 校準 plan 的設定是 GPT-4.1-mini／無 effort 與 GPT-5.6 Sol／low，並非這次四個設定；不能借用能力。這是有界查核結果，不宣稱搜尋完所有私人歸檔。正式 50 題答案／隱藏成績未用來改能力卡或門檻。
- CodexRadar 新 score 僅有家族、強度及 Codex harness；沒有受測 API 日期版、供應商、權重／量化與執行日期的完整綁定。三個閉源候選的 mapping 仍 pending；OSS 的 provider 工件身份也 pending。
- robots.txt 允許爬取只證明該公開存取指示，不授予商用資料再散布權。模型 Apache-2.0 也不是榜單資料授權；來源／領域適用性、營運更新 owner 及 input-bound review 仍待核定。沒有聯絡來源／供應商。
- LiveBench 官方 GitHub HEAD／main README 及舊固定 commit README 本輪皆 404，保留回應；這不證明刪庫或專案停止。未繞到鏡像、不沿用舊讀取時間，也未更換首版主要來源。這個非首版替代來源不列為新的首版必補功能。

補充查到的公開材料另留在矩陣：BaseTen 2025-10-24 工程文章說明當時採原生 MXFP4、TensorRT-LLM／Dynamo 與推測解碼，支持歷史技術脈絡，沒有提供今天此端點的 build／權重 revision 綁定。社群 GPT-6.1 Sol benchmark 是 Codex／ChatGPT Pro 的通用任務資料，未附完整執行 harness，不能補此 SQLite 操作資格；SQL Server tuning 頁亦不是同一個 SQLite 契約。未把泛用編碼、SQL injection 或其他 SQL 方言的評分填成六項已通過。

## 使用與下一步

新包只作審閱輸入，不啟用正式 store、不發模型請求：

```sh
.venv/bin/cloudyfan-scores prepare-first-pool \
  --profile examples/first-pool-profile-public-20261020.json \
  --output-dir runtime/first-pool-public-review-NEXT
```

預期 exit 1／blocked，原因是精確受測配置綁定、SQL 操作資格及來源／日期／review 缺證；不是程式失敗。review 保持空，approved=false。更新任何證據後都需新包與新的有效 input 綁定，不沿用原核定。

優先處理剩下的「榜單受測配置→API 精確配置」鏈、同配置非 holdout SQL 操作證據，以及現有評測日期政策需要的樣本日期。現有材料不能補的項目維持待核定；本輪不以放寬政策、再跑付費實驗或重審 RG-01 解決。RG-04 費用保障獨立存在。

## 可追溯的公開來源

- [OpenRouter 模型目錄](https://openrouter.ai/api/v1/models)；[模型目錄契約](https://openrouter.ai/docs/api/api-reference/models/get-models)。
- 指定端點：[低](https://openrouter.ai/api/v1/models/openai/gpt-6-sol/endpoints)、[中](https://openrouter.ai/api/v1/models/openai/gpt-6.1-sol/endpoints)、[高](https://openrouter.ai/api/v1/models/openai/gpt-6-astra/endpoints)、[開源](https://openrouter.ai/api/v1/models/openai/gpt-oss-120b/endpoints)。
- OpenAI 官方模型頁：[Sol](https://developers.openai.com/api/docs/models/gpt-6-sol)、[6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)。
- [OpenAI GPT-OSS 固定 config](https://huggingface.co/openai/gpt-oss-120b/raw/b5c939de8f754692c1647ca79fbf85e8c1e70f8a/config.json)、[固定 README](https://huggingface.co/openai/gpt-oss-120b/raw/b5c939de8f754692c1647ca79fbf85e8c1e70f8a/README.md)。
- [CodexRadar 模型評測頁](https://deng.codexradar.com/)、[公開 catalog](https://api.codexradar.com/api/v1/library-catalog)、[低分](https://api.codexradar.com/api/v1/radar-bench-score?model=gpt-6-sol&effort=low)、[中分](https://api.codexradar.com/api/v1/radar-bench-score?model=gpt-6.1-sol&effort=high)、[高分](https://api.codexradar.com/api/v1/radar-bench-score?model=gpt-6-astra&effort=xhigh)。

上述動態 URL 之後可能變動；本次精確回應的時間、狀態與 SHA256 以矩陣及本機歸檔為準。本輪離線檢查、source／RG-01／舊資料不變及收據另記在矩陣；1,206 全套結果只引用先前紀錄，不冒稱本次重新跑過。

## 本輪離線結果

141 項既有模型池／來源／資格／更新回歸及 `./scripts/check --quick` 通過。32 份明確歸檔公開 GET（26 個 200、3 個舊介面 410、3 個 LiveBench 404）的 hash、四個配置綁定、三筆分數重算、新舊 report 重播、未知評測日期阻擋及 49 小時後的來源過期拒絕均已檢查。CLI 新包 exit 1／blocked 符合預期。網頁搜尋／頁面發現工具不計入這 32 份歸檔數。

原始碼／測試／建置 digest 與先前 1,206 項完整驗證一致；257 份不在本次文件範圍的既有 tracked 檔案雜湊一致。原始付費試驗未被讀來調卡或重送。當次檢查的本機收據、腳本初始化匯入錯誤與修正見矩陣，沒有隱藏失敗或宣稱新一輪全套測試。
