# 先過品質與速度，再比較完整成本，決定是否需要 Jev

2026-10-20；本輪是離線實作，沒有重新測原四候選、呼叫真實模型或啟用正式模型池。
最新離線結果與來源指紋見[實作證據](evidence/qcs-offline-20261020.json)；原始測試、中斷與模擬收據留在忽略的 `runtime/qcs-implementation-20261020T072104Z/`。

## 實際改變

新增明示選用的 `hierarchical-qcs-v1`，對應配置描述的 `engine_version=0.3.0`。
原本 `hierarchical-sequential-v1`／`0.2.0` 保留預設行為；套件發行版本沒有改動。
舊計畫、原有付費答案與 manifest 不改寫。新政策須使用新 run ID，不能藉續跑換規則。

新政策仍先開源作答，公開驗收通過就結束。沒有通過或無法確認時：

1. 在原低／中品質池中，排除不適用、來源過期、能力不足或速度不符的候選。
2. 比較合格直接路徑的完整成本，選較低保守上緣；同價時選較快，兩者都相同才用預先設定的偏好。
3. 獨立校準若能證明付 Jev 費後仍有足夠節省，才分類。否則直接選模，實際不送 Decisions 請求。
4. 所選模型實際作答並公開驗收；仍失敗才對中／高執行相同檢查，最多三次生成、兩次分類。

第二次分類用最新失敗模型、答案與公開診斷。只缺低池不應讓中／高與開源全部失效；沒有合格可達路徑則在開源派送前拒絕。公開未知不算通過。此 opt-in 接口只允許 CloudyFan 模式，沒有恢復全大模型獨立對照。

## 三維資料與候選

`scores.metrics.normalize_benchmarks(raw, retrieved_at=...)` 是不連網的純函式：

- AA 原生 intelligence／coding／agentic 指數與 pricing 分開保存，不轉成答對機率。
- Design Arena 的 elo／win_rate 與平均生成毫秒分開保存，不把平均值冒充 p95。
- 沒提供 speed／pricing／effort／provider 就保留未知。費率保持十進位字串及 USD/token 語意。

`normalize_endpoints` 保留指定 endpoint 的 provider tag、服務層、量化、cache／request／分段費率及來源 hash。
latency 秒和 throughput tokens/s 分欄，不換算成整件任務耗時。內外 model ID 衝突或重複 provider 身份會拒絕；名稱中的日期不當成已證實的真實模型身份。
這些是新 opt-in 純函式，不表示舊匯入 CLI 已自動改成抓全部資料，也沒有新增遠端同步器。

`scores.qcs.prepare_qcs(snapshot, requirements, evidence, policy, now=...)` 在既有品質快照後增加任務資料：

| 輸入 | 必要內容及邊界 |
|---|---|
| requirements | domain、公開操作能力、context 與允許 provider；不讀隱藏答案 |
| 每候選 evidence | 精確 configuration digest、獨立校準來源／hash／效期、`quality_sufficient`、生成與完整成本區間、同統計量的流程耗時、可選正規化來源列 |
| policy | 固定 workload ID、成本安全餘量、最大流程耗時／統計量、各階段路徑與完整校準 |

來源列必須對上模型、強度、provider、服務層及已指定量化；不把別家最低價與最快速度拼成候選。
重抓資料不刷新已知舊 `as_of`。缺少候選證據會逐筆排除，不默認零費用或無限快。
完整成本包含生成、失敗、重試、修正、升級、後續分類、cache 與 reasoning；生成便宜不代表完成便宜。
群內排序也使用完整成本，榜單高／中／低仍保留原生品質語意，沒有強湊固定成本倍率。

這些可信輸入由應用端提供；布林 `quality_sufficient` 和自行填入 HTTPS URL 並不是正式資格證明。
正式部署仍需既有 `qualified_pool_core` 的來源與 SQL 資格審查，再透過 `BudgetedCore` 的有效授權／可強制費用保障。
本輪沒有核定任何真實候選、來源授權或校準機率。

## 什麼情況才值得付分類費

每個 `low_mid`、`mid_high:low`、`mid_high:mid` 都有自己的固定條件與預測；不是全域平均成本。
比較的是上一階已付費之後，剩餘完整流程的 USD 成本；共同已付費成本不重複計算。

```text
直接候選 = 合格候選中，完整成本上緣最低者；其次比速度，再依凍結偏好打破平手
分類預期成本上緣 = 本次分類費上緣
                   + max_p [p × 選低後完整成本上緣 + (1-p) × 選高後完整成本上緣]
保守淨節省 = 直接候選完整成本下緣 - 分類預期成本上緣
只有淨節省 > max(固定美元餘量, 直接成本下緣 × 固定比例餘量) 才分類
```

`p` 是獨立校準的有效低候選選擇機率區間，包含分類失敗／無效回覆回退到較高候選的結果；不是 Jev 自報信心或正式題事後答案。
保守淨節省是相對上述凍結直接政策的估計，不是通用最佳解或實付降本率。

**若直接與分類後使用同一份候選成本預測，付分類費不可能勝過其中最便宜的直接路徑。** 程式會略過 Jev。
不能指定昂貴的 `direct_tier` 來製造分類優勢；這欄只在同成本、同速度時打破平手。
各直接 `paths` 上緣至少涵蓋該候選已知的完整成本上緣，不允許把升級成本從階段預測中漏掉。

若主張 Jev 能挑出「適合低模型的子集合」而減少試錯，必須另給 `classified_paths` 與 `classified_calibration`：

- 同一份獨立校準母體及 workload、同一剩餘成本期間與全部費目。
- 明確 stage、previous tier、公開失敗／未知情境，以及有效機率已含分類 fallback。
- 固定 Decisions 版本；Core 必須使用該版本。
- `classifier_contract_hash` 綁定實際二選一 payload 模板、問題版本、候選卡及順序、前一失敗設定與 fallback 規則。

這是「在該分類器選到此候選的條件下」的完整後續成本，與無條件直接成本不同，不能偷換兩者。
不提供此證據就不使用樂觀條件成本；綁定錯誤或已過期則拒絕。
同一分類費只在本次 classifier interval 加一次，後續分類仍包含在後續路徑。

## 速度與預算是不同檢查

所有成本／速度都是校準預測，不是供應商可強制保障。每個階段的剩餘耗時之外，必須提供 `complete_policy_flow`：

- metric `whole_task_flow_ms`、與政策相同的 mean／p50／p95／p99，以及不超過 `max_flow_ms` 的上緣。
- 獨立校準同母體與效期，涵蓋開源首答、公開驗收、可能分類、生成、修正及失敗的整件流程。
- `flow_contract_hash` 綁定整份 snapshot、requirements、候選 evidence 和政策；改設定必須換相應校準，不能沿用舊預測。

政策另須有 `execution_contract`，與實際 Core 的 limits（包含工具／輸出輪數）、系統提示、公開診斷契約、本地／執行位置／拒絕費用政策，以及 `contract_version` 一致；Core 在派送前比對。
可用 `core.execution_contract_descriptor(contract_version)` 取得要校準的執行設定。應用端仍須如原 Core 契約，當工具、驗收器或工作區改變時更新 contract_version；此版本宣告不能證明未揭露的外部環境真實性。

不能把單次平均時間、provider 吞吐或各階段 p95 加總，說成整件流程 p95。
符合預測限制也不保證每一次真實請求一定在期限內；正式延遲承諾仍待實測。

`SelectionPolicy.reservation_plan()` 列出可達生成階段及最壞分類次數。
`BudgetedCore` 仍用原有已驗證模型／工具費用上界，依 max_turns 預留，不拿預期成本當封頂。
純直接路徑可不要求不會呼叫的低模型／分類器上界；任何可達階段仍須有保障。
歷史未知費用、身份不符、未知回應及核帳中斷仍阻擋，已知 429 失敗費用仍累計。

## 離線使用與重播

先經正常快照／資格準備，再建立獨立校準包。預測與 hash 都應在實際校準、正式題之前凍結：

```python
policy['complete_policy_flow']['contract_sha256'] = flow_contract_hash(
    snapshot, requirements, candidate_evidence, policy)
plan = prepare_qcs(snapshot, requirements, candidate_evidence, policy, now=now)
core = ScoreCore(client, snapshot, requirements, decision_profile,
                 run_id='new-qcs-run', qcs_plan=plan)
# task 必須有與政策相同的 workload_id。
# 真實執行仍必須透過 BudgetedCore、有效授權與已確認的費用保障。
```

不要用示範內合成校準或 `verify_bound=lambda ...: True` 作正式資格。
新的離線示範只使用 `httpx.MockTransport`：

```sh
.venv/bin/python scripts/demo_qcs_routing.py --scenario direct --output runtime/new-qcs-direct
.venv/bin/python scripts/demo_qcs_routing.py --scenario classify --output runtime/new-qcs-classify
./scripts/check --integration
```

每個輸出目錄必須全新。manifest 保存完整政策與選擇計畫；經濟檢查另留不可變 checkpoint。
中斷後保留既有答案／費用，完成的任務重播不新增 POST。原任務可使用原始過期快照／估計重播，不能用它開新任務。

## 尚未驗證

真實模型版本／SQL 能力、來源使用範圍、可採用的完整成本及整件耗時校準、分類條件機率、供應商完整費用保障，都仍待取得。
本輪合成路徑只驗證程式決策與帳務機制，不證明真實節費率、真實延遲、Jev 單獨增益或商業利潤。
原四候選與 US$60 提案仍維持暫停；不在原組合上直接開考。RG-01、正式放行 registry、SQL 題庫／門檻及歷史付費結果不變。
