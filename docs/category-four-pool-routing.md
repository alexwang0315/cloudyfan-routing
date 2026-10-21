# 各類獨立四池與選模（category-qcs-v1）

目的：讓寫程式、網頁、搜尋等任務各用自己的品質資料、候選及完整成本，不再把全部任務套同一個程式題模型池。這是可注入的離線接入，沒有切換正式 current、呼叫真實模型、改 SQL 考試或改寫歷史實驗。

## 使用方式與資料契約

1. 使用既有免費 GET 存下的 `qcs-source-v1` 觀測，建立各類明確的模型版本／強度／供應商 registry。`compile_category_matrix(observations, registries, policies, now=...)` 產生 23 個獨立視圖，每類都保留 `open_source / low / mid / high` 四個列表，各列表可以有多個候選。
2. 呼叫端明確提供 canonical `category`。`resolve_category` 只支援無歧義別名，例如 coding、website；`search` 不足以區分四種搜尋 benchmark，會要求指定細類。不從 prompt 猜類，也不為此額外呼叫付費分類器。
3. `prepare_category_selection(matrix, category, requirements, evidence, policy, now=...)` 選出該類可執行的四池候選與 QCS 計畫。`CategoryCore` 直接接既有 `Core.run`、注入驗收器及 `BudgetedCore`，不是另一套生成／核帳引擎。
4. 任務必須符合校準的 `workload_id`、工具／執行限制及驗收契約。呼叫端負責正確指定任務類別；通過格式檢查不代表系統能自動辨識任務或知道一般問答答案。

```python
from cloudyfan_jev.scores import CategoryCore, compile_category_matrix

matrix = compile_category_matrix(observations, registries, native_policies, now=stamp)
core = CategoryCore(client, matrix, "artificial-analysis:coding_index",
                    requirements, independent_evidence, qcs_policy, decision,
                    run_id="new-category-run", clock=clock, limits=limits)
result = core.run("new-category-run", public_task, environment_factory,
                  contract_version="frozen-validator-v1")
```

`client` 與費用保障／授權仍使用現有機制；這段不是網路執行授權。來源缺漏、資格未完成或無法保障費用時，不能因有矩陣便直接付費。

## 23 個分類

| 來源 | 類別 | 品質原生尺度 |
|---|---|---|
| Artificial Analysis | agentic_index、coding_index、intelligence_index | 各自指標，不混算 |
| Design Arena models | 3d、asciiart、audiorealism、codecategories、dataviz、gamedev、graphicdesign、image、imageediting、logo、svg、text-to-speech、uicomponent、website | 各類 Elo |
| OpenRouter | gpqa_diamond、tau_bench_verified_airline、search_browsecomp、search_dsqa、search_hle | 各自 accuracy |
| OpenRouter | search_widesearch | f1_by_item |

同一模型可以在不同類別落入不同品質池。開源是獨立路徑，與品質三池不是互斥的模型身分。來源／metric／arena／category／benchmark_type 必須相符；不能用某類最高分補另一類缺分，不能把新下載時間當成新評測時間。

[23 份政策模板](../examples/category-policies-v1.json)使用各來源原生尺度，標為 `exploratory_unvalidated`；門檻是候選準備設定，沒有用正式題答案最佳化，也不是已驗證的最佳商業分界。公開分數不是答對機率。模板的 `category_family_prior` 另要求每個固定設定提供 `category_qualification`：`category_id`、`configuration_sha256`、獨立報告的 `report_id`（64 位十六進位摘要）與 `valid_until`。這是各類報告的引用與適用範圍檢查，不是替報告內容或正式資格代簽。

開源模型可以沒有公開排行榜分數，但必須提供上述該類獨立能力證據、權重身分、固定設定及 QCS 資料；分數保留未知，不塞進低分池。若已提供公開觀測綁定但不符，仍排除，不默默改走無分數路徑。來源設定、強度或模型對照不明確時不自動猜測，保留排除原因。

## quality、cost、speed 如何實際影響派送

| 維度 | 用途與限制 |
|---|---|
| quality | 各類原生分數安排高／中／低；真正能否派送另需固定設定、該類獨立任務資格與 QCS 品質證據，不能借用其他類的 `quality_sufficient`。 |
| cost | 使用同類、同工作量、同完整流程的成本區間，包括生成、失敗、重試、修正、升級、後續分類、cache 與 reasoning。公開單價只作來源觀測，不冒充完整任務成本。 |
| speed | 必須滿足校準指定的統計量與整段流程限制；端點 throughput／TTFT 不直接冒充整題 p95。候選成本相同時優先較快設定。 |

先以能力、上下文、供應商、品質、時效及速度淘汰不符候選，再於各池選完整成本上界最低者，同成本選較快者，再以原生分數及固定 ID 破同分。不是把三個不同尺度相加得到一個虛假的總分。

跨池決定沿用 `hierarchical-qcs-v1`：以獨立校準的條件成本、選擇機率區間、分類費與完整流程時間，對比最便宜的直接路徑。只有保守淨節省同時超過 `minimum_saving_usd` 與 `minimum_saving_fraction`，且速度合格，才呼叫 Jev。沒有足夠校準或價差不足，直接選合格候選，不付分類費，也不為不可達分類預留費用。單看生成便宜但失敗／升級很貴，不足以通過這個判斷。

## 順序、續跑與隔離

- 開源首答；公開驗收失敗／未知才到低／中；再失敗／未知才到中／高。最多三次生成、兩次分類。因成本略過分類時，該階段仍會由合格候選實際作答。
- 第二階段使用最近一次答案與公開診斷，不能使用隱藏答案或其他實驗組結果。
- 類別、整份矩陣 ID、選中快照、候選、分類政策、完整成本校準與驗收契約會凍結。新矩陣不得套進舊任務；既有回答仍沿原設定重播，不重送已付費請求。
- 缺資料只阻擋該類，不能退回 coding 池。矩陣整體會檢查內容完整性，但另一類資料過期不會作為當前類的時效阻擋。
- 舊快照未提供 `open_source_pool` 時保留原單例；明確提供空列表時不得暗中回退。未知費用仍先停止，已知失敗費用仍累計。
- SQL 任務另外保留既有固定版本 SQL 資格；網站／圖像／搜尋等不能僅靠 SQL 測試取得其他任務資格。多模態傳輸與工具執行仍須對應 adapter 和獨立整合證據，本次沒有自動實作影像／語音生成服務。

## 離線驗證與仍未知事項

`tests/test_categories.py` 驗證資料與四池隔離；`tests/test_category_routing.py` 以 `Core.run + httpx.MockTransport` 驗證真實程式派送、分類略過、完整費用、預留及中斷重播。`scripts/demo_category_routing.py` 可用合成矩陣演練全部 23 類；它刻意只用算術驗收器檢查接線，不能證明真實模型會寫 SQL、設計網站或搜尋。

```sh
uv run --locked --offline python scripts/demo_category_routing.py --output runtime/category-demo-new
uv run --locked --offline pytest -q tests/test_categories.py tests/test_category_routing.py
```

可查核結果及完整檢查限制記錄於 [本輪證據](evidence/category-routing-offline-20261021.json)。真實候選仍須完成適用的版本／能力、完整成本、整段速度及來源使用證據；不因離線測試通過而核定模型、不新增實測節費率。


提交前審查與精確提交樹驗證見[提交前證據](evidence/category-routing-tree-precommit-20261021.json)；原工作區及舊副本狀態分開保留。
