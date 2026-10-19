# 全新 50 題四組實測 — 2026-10-19

🟢 **本批可採信的結果：便宜先答與全大模型均 50/50 通過公開及隱藏驗收；便宜先答 API 支出低 82.71%，完成流程中位延遲低 58.23%。** 這是本批 SQL 樣本結果，不能外推所有工作。

🔴 **本批不能宣稱 CloudyFan 等品質降本，也沒有驗證 Jev 的選模增益。** CloudyFan 只有 10/50 取得並通過答案，另 40 次是 BaseTen 上游 429；開源先答取得 20/50，另 30 次是同類拒絕。未知答案不是答錯，也不是成功。CloudyFan 10 筆收到的答案都首答通過，分類器實際呼叫 0 次，沒有發生真實 Jev 二元選擇。

## 執行範圍與凍結條件

- 50 道全新 SQL 任務，10 個操作家族各 5 題；作者分層 easy 15、medium 20、hard 15。層級不是經模型表現校準的難度，五題家族存在相關性。
- 四組各自獨立請求：CloudyFan、固定 high、low 先答＋必要 high 修正、open_source 先答＋必要 high 修正。各組沒有共用已付費答案。
- 原 Core 規則未改：CloudyFan 開源先答→公開驗收→必要時 low/mid→若選 mid 才 mid/high→唯一修正。其他三組公開驗收未過後唯一 high 修正。供應商拒絕目前會終止該流程，**不會觸發模型驗收失敗的升級路徑**。
- 相同系統提示、SQL 公開契約、token 上限 8192、最多兩次生成、固定版本／供應商／effort／能力卡。無工具／外掛／provider fallback，無自動付費重試，concurrency=1。
- OSS 是 openai/gpt-oss-120b / low / baseten/fp4；low 是 openai/gpt-6-sol-20260922 / low；mid 是 openai/gpt-6.1-sol-20260929 / high；high 是 openai/gpt-6-astra-20260903 / xhigh，後三者固定 OpenAI。Jev 固定 typesafe/jev-1.13-20260917，這批未觸發。
- 固定種子 2026101950、隨機題序及旋轉組序，每組排程位置各 12/13 次。這只平衡位置，沒有平衡所有前序組合，也不能消除共享上游容量變動；兩個開源組的可用性差異不能直接歸因於路由演算法。
- 每題 2 組公開＋5 組隱藏資料。事前 Python oracle 與 SQLite 350 次交叉核對、core executor 350 次 reference 核對、50 個反例全部拒絕。200 個流程都有終態後才執行隱藏驗收；隱藏答案與結果未進入分類或修正。
- Core commit 為 1a2de3938f2042cde2e46ab6be6d3b1f74642571；凍結 plan 與 source hash 見下方證據。沿用既有設定，沒有用本批答案重新校準。

## 中斷、接續與帳務證據

第一個 CloudyFan 請求收到上游 429，原執行器因沒有費用收據而暫停，當時已完成兩筆成功作答。保留原停止紀錄與付費答案，沒有重送。其後擷取 [OpenRouter 官方定價政策](https://openrouter.ai/pricing)：報錯嘗試不收模型 token 費，已執行的輔助服務則可能收費；本輪固定純文字、無任何輔助服務。

執行補充 v2/v3 將符合明確範圍的 upstream429 列為供應商拒絕、政策零費，原題目、來源、模型、effort、選模、修正與驗收規則不變。v2 在新增 POST 前遇到本機重入鎖定，原版保留；v3 修正該實驗接續鎖並通過新 MockTransport 派送／未知費用阻擋測試。未解費用、其他 HTTP/transport/identity 問題的停止規則沒有放寬。接續不是單一未中斷執行器，版本均可追溯。

202 次付費端點請求中，132 筆取得 generation_total_cost 收據，70 筆 upstream429 依上述政策列零費，**不是 202 筆 generation 收據**。最後逐筆唯讀重查 70 筆仍為 GET404，原錯誤保留；零費依據是官方政策及實際純文字請求範圍，不是 404。若後續取得真實費用收據，收據優先，不能與政策數字重複相加。

本輪總額 **US$2.8850731**，全部歸屬 200 個流程，沒有未配對費用或未知費用。派送瞬間 key 用量少入帳 US$0.114122，最終唯讀快照已補足，key delta 為 US$2.885073100，與帳本差 0。新付費校準為 0；先前獨立 smoke US$0.060291624 不屬本批，不重複加入。這不表示任何歷史未知費用已清除。開源 API 費照實計入，未假設本機電力／主機或人工免費；這裡的費用比較限 API。

## 可採信的解讀與優先缺口

1. 本批便宜模型 50 題全數首答通過，無需分類或升級。相對固定大模型，完整 API 費從 US$2.3646875 降到 US$0.4089475，少 82.71%；逐題平均 82.54%、中位 82.25%、最小 79.95%、最大 86.84%，無變貴題。這支持本批可直接便宜先答，尚未證明它對未知任務也必然可靠。
2. 開源先答兩題 F19、F43 首答未通過公開驗收，使用唯一 high 修正後均通過隱藏驗收；修正費 US$0.0997225 已計入。CloudyFan 因收到的 10 筆答案均首答通過，未觸發分類；不能從較低支出推論 Jev 增益。
3. CloudyFan 的 99.83%、開源先答的 95.45% 都只是含未服務流程的支出差異，不能稱等品質節省。每成功任務成本也不能證明尚未取得答案的題目能以相同成本補完。
4. 正式落地應先補「已確認無答案的上游拒絕如何進入受限升級」，同時保留未知費用停止、原始證據及總嘗試上限。這是本輪暴露的工程缺口；本次沒有中途改規則、補跑或實作此功能，也不需要再審 S01～S08。
5. 下表工作流程延遲僅統計有完成結果的流程；CloudyFan n=10、high n=50、low n=50、開源 n=20。快速拒絕時間與 API 全流程時間另列原始統計，不能當成成功服務的低延遲。快取值照實保存，不宣稱快取隔離。有限隱藏 fixtures 不等於任意 SQL 正確性，這也不是跨領域商業資格驗證。

## 離線檢查與檔案

- 原四組 MockTransport smoke：路徑／唯一修正／接續不重送／未知費用阻擋通過；接續政策 10 項檢查通過；報表合成測試通過，均無真 API。
- 本次 `./scripts/check --quick` 通過 locked offline sync、相依性、project-state 與 Ruff。產品 runtime source 未改；本次未另重跑先前 1104 項全套測試，不冒稱新的全套驗證。
- [去識別驗證紀錄](evidence/fresh50-four-arm-20261019.json)、[逐題比較 CSV](evidence/fresh50-four-arm-per-task-20261019.csv)。原始答案、提示、request/generation 證據、凍結計畫與所有版本保存在 Git 忽略的 runtime/fresh50-four-arm-20261019/，不納入原始碼。
- 未重審 S01～S08、未部署、未推送、未新增正式資格或簽核；本輪文件未提交 Git。

---

# CloudyFan 全新 50 題四組配對測試

成功標準：首答／最終答案同時通過公開驗收及後置隱藏驗收。分類、修正與失敗請求均計費；未知費用不視為零。

| 組別 | 題數 | 首答通過 | 最終通過 | 失敗／工程中斷／供應商拒絕／無法驗收 | 完整 API USD | 每成功任務 USD | 工作流程秒 平均／中位／P95 | API 秒 平均／中位／P95 |
|---|---:|---:|---:|---|---:|---:|---|---|
| cloudyfan | 50 | 10/50（未知 40） | 10/50 | 0 / 0 / 40 / 0 | 0.003952600 | 0.000395260 | 2.092 / 2.194 / 2.864 | 0.723 / 0.513 / 1.829 |
| fixed_high | 50 | 50/50（未知 0） | 50/50 | 0 / 0 / 0 / 0 | 2.364687500 | 0.047293750 | 6.441 / 5.781 / 10.387 | 5.877 / 5.259 / 9.781 |
| low_first | 50 | 50/50（未知 0） | 50/50 | 0 / 0 / 0 / 0 | 0.408947500 | 0.008178950 | 2.578 / 2.415 / 4.208 | 2.013 / 1.800 / 3.526 |
| open_source_first | 50 | 18/50（未知 30） | 20/50 | 0 / 0 / 30 / 0 | 0.107485500 | 0.005374275 | 2.720 / 2.080 / 8.612 | 1.192 / 0.685 / 2.981 |

## 相對全大模型的完整配對支出差異

| 組別 | 全批比例 CDR | 逐題平均 | 中位 | 最小 | 最大 | 變貴比例 | 可用稱謂 |
|---|---:|---:|---:|---:|---:|---:|---|
| cloudyfan | 99.83% | 99.84% | 100.00% | 99.04% | 100.00% | 0.00% | spending difference only |
| fixed_high | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | equal-quality sample savings |
| low_first | 82.71% | 82.54% | 82.25% | 79.95% | 86.84% | 0.00% | equal-quality sample savings |
| open_source_first | 95.45% | 95.34% | 100.00% | -13.40% | 100.00% | 4.00% | spending difference only |

## 費用、分類與修正

| 組別 | 呼叫 | 分類呼叫 | 修正題 | 修正後通過 | 分類費 USD | 首答費 USD | 修正費 USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| cloudyfan | 50 | 0 | 0 | 0 | 0.000000000 | 0.003952600 | 0.000000000 |
| fixed_high | 50 | 0 | 0 | 0 | 0.000000000 | 2.364687500 | 0.000000000 |
| low_first | 50 | 0 | 0 | 0 | 0.000000000 | 0.408947500 | 0.000000000 |
| open_source_first | 52 | 0 | 2 | 2 | 0.000000000 | 0.007763000 | 0.099722500 |

## stratum 分層結果

| 分層 | 組別 | 題數 | 首答／最終通過 | API USD | 流程中位秒 |
|---|---|---:|---|---:|---:|
| easy | cloudyfan | 15 | 3 / 3 | 0.001066000 | 1.262 |
| easy | fixed_high | 15 | 15 / 15 | 0.644062500 | 5.082 |
| easy | low_first | 15 | 15 / 15 | 0.118242500 | 2.362 |
| easy | open_source_first | 15 | 6 / 6 | 0.002091700 | 1.621 |
| hard | cloudyfan | 15 | 4 / 4 | 0.001615600 | 2.157 |
| hard | fixed_high | 15 | 15 / 15 | 0.811862500 | 7.737 |
| hard | low_first | 15 | 15 / 15 | 0.128052500 | 2.512 |
| hard | open_source_first | 15 | 4 / 5 | 0.052310600 | 2.806 |
| medium | cloudyfan | 20 | 3 / 3 | 0.001271000 | 2.488 |
| medium | fixed_high | 20 | 20 / 20 | 0.908762500 | 5.529 |
| medium | low_first | 20 | 20 / 20 | 0.162652500 | 2.415 |
| medium | open_source_first | 20 | 8 / 9 | 0.053083200 | 2.226 |

## category 分層結果

| 分層 | 組別 | 題數 | 首答／最終通過 | API USD | 流程中位秒 |
|---|---|---:|---|---:|---:|
| conditional_aggregates | cloudyfan | 5 | 1 / 1 | 0.000401200 | 2.159 |
| conditional_aggregates | fixed_high | 5 | 5 / 5 | 0.216262500 | 5.112 |
| conditional_aggregates | low_first | 5 | 5 / 5 | 0.039842500 | 2.327 |
| conditional_aggregates | open_source_first | 5 | 1 / 1 | 0.000352700 | 1.819 |
| group_conditions | cloudyfan | 5 | 1 / 1 | 0.000357900 | 2.229 |
| group_conditions | fixed_high | 5 | 5 / 5 | 0.219787500 | 5.712 |
| group_conditions | low_first | 5 | 5 / 5 | 0.039467500 | 2.114 |
| group_conditions | open_source_first | 5 | 1 / 2 | 0.050361400 | 5.276 |
| join_existence | cloudyfan | 5 | 0 / 0 | 0.000000000 | 未知 |
| join_existence | fixed_high | 5 | 5 / 5 | 0.219087500 | 5.488 |
| join_existence | low_first | 5 | 5 / 5 | 0.039487500 | 2.386 |
| join_existence | open_source_first | 5 | 3 / 3 | 0.001079400 | 1.731 |
| rank_selection | cloudyfan | 5 | 0 / 0 | 0.000000000 | 未知 |
| rank_selection | fixed_high | 5 | 5 / 5 | 0.227225000 | 5.558 |
| rank_selection | low_first | 5 | 5 / 5 | 0.040275000 | 2.405 |
| rank_selection | open_source_first | 5 | 3 / 3 | 0.001095100 | 1.783 |
| relational_division | cloudyfan | 5 | 1 / 1 | 0.000408200 | 1.914 |
| relational_division | fixed_high | 5 | 5 / 5 | 0.262725000 | 7.432 |
| relational_division | low_first | 5 | 5 / 5 | 0.041295000 | 2.326 |
| relational_division | open_source_first | 5 | 1 / 2 | 0.050951200 | 5.230 |
| row_filters | cloudyfan | 5 | 2 / 2 | 0.000664800 | 1.187 |
| row_filters | fixed_high | 5 | 5 / 5 | 0.208712500 | 4.220 |
| row_filters | low_first | 5 | 5 / 5 | 0.038912500 | 2.117 |
| row_filters | open_source_first | 5 | 2 / 2 | 0.000659600 | 1.179 |
| sequence_relations | cloudyfan | 5 | 0 / 0 | 0.000000000 | 未知 |
| sequence_relations | fixed_high | 5 | 5 / 5 | 0.267075000 | 7.737 |
| sequence_relations | low_first | 5 | 5 / 5 | 0.042915000 | 2.867 |
| sequence_relations | open_source_first | 5 | 2 / 2 | 0.000862400 | 2.735 |
| set_operations | cloudyfan | 5 | 1 / 1 | 0.000458800 | 2.864 |
| set_operations | fixed_high | 5 | 5 / 5 | 0.225512500 | 5.499 |
| set_operations | low_first | 5 | 5 / 5 | 0.040052500 | 2.425 |
| set_operations | open_source_first | 5 | 2 / 2 | 0.000775900 | 2.263 |
| temporal_composition | cloudyfan | 5 | 3 / 3 | 0.001207400 | 2.400 |
| temporal_composition | fixed_high | 5 | 5 / 5 | 0.282062500 | 8.871 |
| temporal_composition | low_first | 5 | 5 / 5 | 0.043842500 | 2.655 |
| temporal_composition | open_source_first | 5 | 1 / 1 | 0.000497000 | 2.806 |
| window_accumulation | cloudyfan | 5 | 1 / 1 | 0.000454300 | 2.488 |
| window_accumulation | fixed_high | 5 | 5 / 5 | 0.236237500 | 5.168 |
| window_accumulation | low_first | 5 | 5 / 5 | 0.042857500 | 2.477 |
| window_accumulation | open_source_first | 5 | 2 / 2 | 0.000850800 | 2.703 |

## 證據及限制

- 本輪全部 API：USD 2.885073100；未知費用呼叫 0；未配對呼叫 0。
- 新校準：USD 0。先前獨立 smoke：USD 0.060291624，未納入本輪總額。
- Key usage delta：USD 2.885073100；與本輪 ledger 差 USD 0.000000000。採用 key-final-recheck.json；狀態 delayed_posting_resolved。派送當下 delta USD 2.770951100，當下與 ledger 差 USD -0.114122000；後續入帳增加 USD 0.114122000。原始快照保留。
- 未跨組重用模型答案；每題各組獨立請求。排程位置各 12 或 13 次；未宣稱快取隔離。
- provider_error_no_charge_policy 為供應商明文失敗零費用證據，與 generation_total_cost 核帳收據分開計數，不冒稱已核帳 generation receipt。
- 各組模型層級、實際快取 token counters、缺值計數、未完成時間及逐題資料均保存在 analysis.json / per-task.csv。
- cloudyfan 費用來源：{"generation_total_cost": 10, "provider_error_no_charge_policy": 40}
- cloudyfan cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 40, "reported_calls": 10, "sum_reported": 0}, "prompt_tokens_details.cached_tokens": {"missing_calls": 40, "reported_calls": 10, "sum_reported": 1408}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- fixed_high 費用來源：{"generation_total_cost": 50}
- fixed_high cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 150143}, "prompt_tokens_details.cached_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 0}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- low_first 費用來源：{"generation_total_cost": 50}
- low_first cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 150143}, "prompt_tokens_details.cached_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 0}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- open_source_first 費用來源：{"generation_total_cost": 22, "provider_error_no_charge_policy": 30}
- open_source_first cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 30, "reported_calls": 22, "sum_reported": 6197}, "prompt_tokens_details.cached_tokens": {"missing_calls": 30, "reported_calls": 22, "sum_reported": 12256}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- Immediate snapshot lag was resolved by the saved final read-only snapshot: final delta equals ledger exactly; original immediate snapshot retained.
- One frozen SQL workload; related five-task families; author-assigned difficulty, not independent domains.
- Hidden fixture success is finite coverage, not universal SQL correctness.
- Public-only repair; hidden grades never caused repair or routing.
- All actual open-source API fees counted; no zero-cost local-host assumption.
- First/final primary success requires joint public and hidden pass; unknown is neither failure nor success.
- API POST times exclude reconciliation GET; engine workflow wall includes calls/public verification and inline reconciliation, excludes later batch reconciliation/hidden grading.
- Completed resumed workflows with missing original wall are excluded from wall distribution and counted as missing; interruptions timed separately.
- Independent requests and balanced 12/13 arm positions do not establish cache isolation.
- No new paid calibration. Earlier smoke cost is disclosed separately, never added twice.
- No deployment, push or formal qualification from this comparison.
