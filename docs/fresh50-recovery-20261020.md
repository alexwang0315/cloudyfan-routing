# 修正後核心的全新 50 題比較 — 2026-10-20

本輪已完成 50 題 × 4 組、200 個獨立流程與後置隱藏驗收。規則、能力卡、模型／強度、公開提示及修正上限全程凍結；沒有依隱藏結果重跑或挑題。

**決策結論：CloudyFan 支出最低，但最終 48/50，尚未達成本輪「答對再降本」標準。便宜先答達 50/50，與全大模型相同，在本樣本降低完整 API 費 82.79%。** 開源先答為 49/50，其支出差距亦不能稱等品質節省。每成功任務成本以全部流程費用除以成功數，包含失敗費用；不代表可保證把剩餘題全部答對的成本。

CloudyFan 的兩題漏判（F32、F48）及開源先答的一題（F32）皆為公開驗收通過、隱藏驗收失敗，因此未進入選模或修正。CloudyFan 另外六題觸發修正，七次分類（一次走到第二層），全部修正通過。問題出在本輪公開驗收的有限案例漏收，不能歸因為這三個流程的 Jev 選模失敗；亦不能用六個成功修正證明 Jev 的普遍獨立增益。沒有把隱藏資料加入公開驗收或事後補跑。

429 費用政策接入通過 50 項相關離線回歸、完整 1,154 項測試與整合。三筆上輪實際 429 只做獨立複本及唯讀核對，以官方明文及嚴格請求範圍認定政策零費用，沒有偽造 generation 收據。新一輪 221 個 POST 均 HTTP200，沒有新的 429；真實限流恢復率仍未驗證。見[政策接入](rejection-fee-policy.md)。

本輪費用 US$5.805611568，帳戶 usage 差額完全一致，未知費用零。來源為 220 筆 generation 核帳收據及 1 筆回應 usage.cost；後者 generation GET 最新 HTTP408，費用已知 US$0.0006483，保留所有 GET 失敗證據，不冒稱 221 筆均取得 generation 收據。開源 API 費按實付計入，沒有視為免費。先前獨立 50 題 US$2.885073100 與 smoke US$0.060291624 均不混入本輪；本輪新付費校準零。此處費用不包含未知單價的主機、人工與付款儲值費。

資料是全新的 projects/events/members SQL 操作，10 類 × 5 題，難度作者分層 15/20/15；每題 2 公開、5 隱藏 fixture。事先完成 350 次獨立 Python oracle／SQLite 比對、350 次核心執行比對與 50 個語意 mutant 拒絕。分層不是獨立難度校準，也不等於 50 種無關任務。排程以全部 24 組順序各兩次，另加正反兩組，各位置 12/13 次；實際 cache counters 有保存，未宣稱快取隔離。每流程 wall 含作答、公開驗收及流程內核帳；不含後續整批核帳與隱藏驗收，API POST 時間另列。

固定模型／供應商：開源 openai/gpt-oss-120b low、BaseTen baseten/fp4；低群 openai/gpt-6-sol-20260922 low；中群 openai/gpt-6.1-sol-20260929 high；高群 openai/gpt-6-astra-20260903 xhigh（後三者固定 OpenAI）；分類 typesafe/jev-1.13-20260917、TypeSafe。兩次生成上限、分類最多兩次、無 provider fallback、無自動付費重試、未知費用／身份即停止皆保留。

[機器可讀證據](evidence/fresh50-recovery-20261020.json)與[200 列逐題費用／品質資料](evidence/fresh50-recovery-20261020-per-task.csv)不含原始提示或答案。完整私有原始紀錄留於 ignored runtime/fresh50-recovery-20261020/，舊 2,369 檔案雜湊完全未變。本輪未部署、推送或本機提交，正式資格維持待核定。

---

# CloudyFan 全新 50 題四組配對測試

成功標準：首答／最終答案同時通過公開驗收及後置隱藏驗收。分類、修正與失敗請求均計費；未知費用不視為零。

| 組別 | 題數 | 首答通過 | 最終通過 | 失敗／工程中斷／供應商拒絕／無法驗收 | 完整 API USD | 每成功任務 USD | 工作流程秒 平均／中位／P95 | API 秒 平均／中位／P95 |
|---|---:|---:|---:|---|---:|---:|---|---|
| cloudyfan | 50 | 42/50（未知 0） | 48/50 | 2 / 0 / 0 / 0 | 0.131666268 | 0.002743047 | 2.977 / 2.201 / 6.310 | 2.325 / 1.571 / 4.598 |
| fixed_high | 50 | 50/50（未知 0） | 50/50 | 0 / 0 / 0 / 0 | 4.109912500 | 0.082198250 | 8.705 / 8.363 / 14.316 | 8.083 / 7.669 / 13.835 |
| low_first | 50 | 50/50（未知 0） | 50/50 | 0 / 0 / 0 / 0 | 0.707202500 | 0.014144050 | 2.660 / 2.469 / 3.593 | 2.080 / 1.905 / 3.007 |
| open_source_first | 50 | 41/50（未知 0） | 49/50 | 1 / 0 / 0 / 0 | 0.856830300 | 0.017486333 | 4.659 / 2.258 / 22.141 | 4.012 / 1.486 / 21.091 |

## 相對全大模型的完整配對支出差異

| 組別 | 全批比例 CDR | 逐題平均 | 中位 | 最小 | 最大 | 變貴比例 | 可用稱謂 |
|---|---:|---:|---:|---:|---:|---:|---|
| cloudyfan | 96.80% | 97.00% | 99.18% | 78.66% | 99.30% | 0.00% | spending difference only |
| fixed_high | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | equal-quality sample savings |
| low_first | 82.79% | 82.69% | 82.51% | 80.58% | 85.61% | 0.00% | equal-quality sample savings |
| open_source_first | 79.15% | 80.91% | 99.20% | -49.62% | 99.34% | 16.00% | spending difference only |

## 費用、分類與修正

| 組別 | 呼叫 | 分類呼叫 | 修正題 | 修正後通過 | 分類費 USD | 首答費 USD | 修正費 USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| cloudyfan | 63 | 7 | 6 | 6 | 0.002043468 | 0.033089300 | 0.096533500 |
| fixed_high | 50 | 0 | 0 | 0 | 0.000000000 | 4.109912500 | 0.000000000 |
| low_first | 50 | 0 | 0 | 0 | 0.000000000 | 0.707202500 | 0.000000000 |
| open_source_first | 58 | 0 | 8 | 8 | 0.000000000 | 0.032515300 | 0.824315000 |

## stratum 分層結果

| 分層 | 組別 | 題數 | 首答／最終通過 | API USD | 流程中位秒 |
|---|---|---:|---|---:|---:|
| easy | cloudyfan | 15 | 14 / 15 | 0.023887382 | 1.711 |
| easy | fixed_high | 15 | 15 / 15 | 1.150637500 | 7.056 |
| easy | low_first | 15 | 15 / 15 | 0.207307500 | 2.453 |
| easy | open_source_first | 15 | 14 / 15 | 0.097940100 | 1.718 |
| hard | cloudyfan | 15 | 9 / 14 | 0.094820886 | 3.415 |
| hard | fixed_high | 15 | 15 / 15 | 1.336787500 | 10.983 |
| hard | low_first | 15 | 15 / 15 | 0.218717500 | 3.094 |
| hard | open_source_first | 15 | 9 / 15 | 0.661276200 | 2.971 |
| medium | cloudyfan | 20 | 19 / 19 | 0.012958000 | 2.069 |
| medium | fixed_high | 20 | 20 / 20 | 1.622487500 | 7.414 |
| medium | low_first | 20 | 20 / 20 | 0.281177500 | 2.347 |
| medium | open_source_first | 20 | 18 / 19 | 0.097614000 | 2.011 |

## category 分層結果

| 分層 | 組別 | 題數 | 首答／最終通過 | API USD | 流程中位秒 |
|---|---|---:|---|---:|---:|
| composed_flow_analytics | cloudyfan | 5 | 1 / 4 | 0.057728686 | 6.310 |
| composed_flow_analytics | fixed_high | 5 | 5 / 5 | 0.470812500 | 12.942 |
| composed_flow_analytics | low_first | 5 | 5 / 5 | 0.075122500 | 3.445 |
| composed_flow_analytics | open_source_first | 5 | 2 / 5 | 0.340513100 | 19.566 |
| cross_entity_grouping | cloudyfan | 5 | 5 / 5 | 0.003130600 | 1.954 |
| cross_entity_grouping | fixed_high | 5 | 5 / 5 | 0.393325000 | 6.616 |
| cross_entity_grouping | low_first | 5 | 5 / 5 | 0.068855000 | 2.289 |
| cross_entity_grouping | open_source_first | 5 | 5 / 5 | 0.003068100 | 2.084 |
| cross_source_coverage | cloudyfan | 5 | 4 / 5 | 0.018529488 | 2.837 |
| cross_source_coverage | fixed_high | 5 | 5 / 5 | 0.451375000 | 11.900 |
| cross_source_coverage | low_first | 5 | 5 / 5 | 0.072295000 | 3.094 |
| cross_source_coverage | open_source_first | 5 | 4 / 5 | 0.146172200 | 2.971 |
| cross_source_sets | cloudyfan | 5 | 4 / 4 | 0.003152000 | 2.184 |
| cross_source_sets | fixed_high | 5 | 5 / 5 | 0.404362500 | 9.434 |
| cross_source_sets | low_first | 5 | 5 / 5 | 0.069682500 | 2.554 |
| cross_source_sets | open_source_first | 5 | 4 / 4 | 0.003160000 | 1.957 |
| interval_and_control_pairs | cloudyfan | 5 | 4 / 5 | 0.018562712 | 3.346 |
| interval_and_control_pairs | fixed_high | 5 | 5 / 5 | 0.414600000 | 10.022 |
| interval_and_control_pairs | low_first | 5 | 5 / 5 | 0.071300000 | 2.456 |
| interval_and_control_pairs | open_source_first | 5 | 3 / 5 | 0.174590900 | 2.851 |
| membership_resource_audit | cloudyfan | 5 | 5 / 5 | 0.003016500 | 1.660 |
| membership_resource_audit | fixed_high | 5 | 5 / 5 | 0.381812500 | 7.097 |
| membership_resource_audit | low_first | 5 | 5 / 5 | 0.068282500 | 2.207 |
| membership_resource_audit | open_source_first | 5 | 5 / 5 | 0.003166500 | 1.718 |
| multi_key_selection | cloudyfan | 5 | 5 / 5 | 0.003342500 | 2.007 |
| multi_key_selection | fixed_high | 5 | 5 / 5 | 0.426350000 | 8.476 |
| multi_key_selection | low_first | 5 | 5 / 5 | 0.070390000 | 2.344 |
| multi_key_selection | open_source_first | 5 | 4 / 5 | 0.088114500 | 2.330 |
| role_aware_joins | cloudyfan | 5 | 4 / 5 | 0.017891582 | 2.246 |
| role_aware_joins | fixed_high | 5 | 5 / 5 | 0.395150000 | 8.100 |
| role_aware_joins | low_first | 5 | 5 / 5 | 0.069270000 | 3.056 |
| role_aware_joins | open_source_first | 5 | 4 / 5 | 0.091806800 | 1.733 |
| text_and_integer_projection | cloudyfan | 5 | 5 / 5 | 0.002979300 | 1.711 |
| text_and_integer_projection | fixed_high | 5 | 5 / 5 | 0.373675000 | 5.413 |
| text_and_integer_projection | low_first | 5 | 5 / 5 | 0.069755000 | 2.167 |
| text_and_integer_projection | open_source_first | 5 | 5 / 5 | 0.002966800 | 1.680 |
| window_state_features | cloudyfan | 5 | 5 / 5 | 0.003332900 | 1.784 |
| window_state_features | fixed_high | 5 | 5 / 5 | 0.398450000 | 6.170 |
| window_state_features | low_first | 5 | 5 / 5 | 0.072250000 | 2.238 |
| window_state_features | open_source_first | 5 | 5 / 5 | 0.003271400 | 1.846 |

## 證據及限制

- 本輪全部 API：USD 5.805611568；未知費用呼叫 0；未配對呼叫 0。
- 新校準：USD 0。先前獨立 smoke：USD 0.060291624，未納入本輪總額。
- Key usage delta：USD 5.805611568；與本輪 ledger 差 USD 0.000000000。採用 key-final-recheck.json；狀態 matched。派送當下 delta USD 5.805611568，當下與 ledger 差 USD 0.000000000；後續入帳增加 USD 0.000000000。原始快照保留。
- 未跨組重用模型答案；每題各組獨立請求。排程位置各 12 或 13 次；未宣稱快取隔離。
- provider_error_no_charge_policy 為供應商明文失敗零費用證據，與 generation_total_cost 核帳收據分開計數，不冒稱已核帳 generation receipt。
- 各組模型層級、實際快取 token counters、缺值計數、未完成時間及逐題資料均保存在 analysis.json / per-task.csv。
- cloudyfan 費用來源：{"generation_total_cost": 63}
- cloudyfan cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 7, "reported_calls": 56, "sum_reported": 33495}, "prompt_tokens_details.cached_tokens": {"missing_calls": 7, "reported_calls": 56, "sum_reported": 128096}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- fixed_high 費用來源：{"generation_total_cost": 50}
- fixed_high cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 261773}, "prompt_tokens_details.cached_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 0}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- low_first 費用來源：{"generation_total_cost": 50}
- low_first cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 261773}, "prompt_tokens_details.cached_tokens": {"missing_calls": 0, "reported_calls": 50, "sum_reported": 0}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- open_source_first 費用來源：{"generation_total_cost": 57, "response_usage_cost": 1}
- open_source_first cache：{"prompt_tokens_details.cache_write_tokens": {"missing_calls": 0, "reported_calls": 58, "sum_reported": 43818}, "prompt_tokens_details.cached_tokens": {"missing_calls": 0, "reported_calls": 58, "sum_reported": 122496}}；狀態 reported values only; missing is unknown；完成流程 wall 缺值 0。
- Saved aggregate snapshot comparison; original snapshots retained.
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
