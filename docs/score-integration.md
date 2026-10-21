# 模型評分接入與固定快照

本版提供評分資料層；目前執行流程是「開源先答 → 公開驗收 → 未通過才低／中選模並作答驗收 → 仍未通過才中／高選模並最後作答驗收」，詳見[順序式路由](sequential-routing.md)。舊版兩次分類之間不作答、只有一次修正的流程仍保留於歷史證據，不套用新規則續跑。這不是新的付費實驗，也不修改歷史能力卡、門檻或結果。外部成績是能力先驗，不能建立任務正確性的證明。

## 資料來源與已實作範圍

|來源|介面|狀態與限制|
|---|---|---|
|LiveBench v1|官方 GitHub commit、靜態 RELEASES 宣告、同 commit 的 CSV／categories JSON|可自動發現最新版 v1 發布並固定 commit；逐子項讀原生成績。強度使用經審閱的本地 metadata 或明確來源設定綁定；缺少時維持未知。沒有執行遠端 JavaScript。|
|Artificial Analysis|`GET https://artificialanalysis.ai/api/v2/language/models/free`|實作分頁、index 版本一致性及金鑰介面；未使用真實金鑰驗證。免費回應的價格欄位不當實付。|
|OpenRouter benchmarks|`GET https://openrouter.ai/api/v1/benchmarks?source=artificial-analysis`|AA 轉載來源仍標為 `artificial_analysis`，channel 為 `openrouter`。上游 index 版本未知時保留不同版本標記，不能默認與 AA 直接資料合併。未做 authenticated live check。|
|CodexRadar|受控 JSON 匯入|接受模型評測頁 `https://deng.codexradar.com/` 的經審閱資料；拒絕帳戶積分。穩定遠端 API 未確認，因此沒有聲稱自動抓取網站。|

官方資料契約：
- https://github.com/LiveBench/new-livebench/blob/main/README.md
- https://artificialanalysis.ai/data-api/docs/reference
- https://github.com/OpenRouterTeam/skills/blob/main/skills/openrouter-benchmarks/references/benchmarks-api.md
- https://deng.codexradar.com/

上述 API 的方案與商業再散布條款須由整合者確認。CLI 不建立帳號、金鑰、訂閱或自動排程。

## 使用方法

```bash
uv sync --extra dev
# 完整離線示範：合成評分 → 三池 → Jev 兩階段 → 修正與驗收
uv run cloudyfan-score-demo --route high

# 公開唯讀網路操作，自動發現並固定 LiveBench v1 的 release/commit
uv run cloudyfan-scores fetch livebench --output runtime/livebench-observations.json

# 已審閱 metadata 可搭配固定 release/commit（兩者必須一起提供）
uv run cloudyfan-scores fetch livebench --release YYYY_MM_DD --revision FULL_40_HEX_COMMIT \
  --metadata runtime/livebench-metadata.json --output runtime/livebench-reviewed.json

# 以已由外部安全載入環境的金鑰讀取 API；不在命令列傳入金鑰
# AA: ARTIFICIAL_ANALYSIS_API_KEY；OpenRouter: OPENROUTER_API_KEY
uv run cloudyfan-scores fetch artificial-analysis --output runtime/aa-observations.json
uv run cloudyfan-scores fetch openrouter --output runtime/or-observations.json

uv run cloudyfan-scores import-codexradar --input runtime/radar-reviewed.json \
  --output runtime/radar-observations.json

# 每個來源／領域使用事先決定的 policy；不要把所有來源塞進單一平均
uv run cloudyfan-scores build --observations runtime/radar-observations.json \
  --registry runtime/registry.json --policy runtime/policy.json --review runtime/qualification-review.json \
  --store runtime/scores-coding
uv run cloudyfan-scores inspect --store runtime/scores-coding
uv run cloudyfan-scores select --store runtime/scores-coding \
  --requirements runtime/requirements.json --output runtime/selected-profiles.json
```

輸出檔已存在時拒絕覆寫。示範產生 `codexradar-import.json`、`registry.json`、`policy.json`、`requirements.json`、`observations.json` 與快照，全部是可查看的 **合成、不可呼叫模型 ID** 範例；可從 `scores/demo.py:example_inputs()` 了解結構，不能直接視為生產資格。

## 分群與設定身分

`policy` 固定 `source`、`source_version`、`metric`、`domain`、`id` 與原生尺度門檻。邊界語義：`score < low_mid` 為低；`low_mid <= score <= mid_high` 為中；`score > mid_high` 為高。CodexRadar 的 80／100 可以直接表達；不得套到 LiveBench 或 AA 當作通用門檻。本版不自動擬合跨來源門檻、不計算跨來源平均、不從 holdout 結果調整規則，也不自動計算 LiveBench 類別／整體平均。

`registry.candidates` 的每筆設定有：

- `config_id`、明確的 `source`／`source_version`／`source_model_id`。
- `profile`：固定版本、允許回傳的 model ID、強度、指定供應商、公開能力卡。
- `approved`、`catalog_checked_at`、`capabilities`、`context_tokens`：管理者審閱的可執行資格；不是自動探測供應商可用性的結果。
- 選填 `estimated_request_usd`、`estimated_latency_ms`；有成本估計時必須提供 `cost_basis`。未知排最後，同價按延遲、再按穩定 ID 排序。這不是已實測的完整流程成本優化。
- 來源沒有強度欄位時，只有 `verified_source_effort` 與 HTTPS `effort_evidence_url` 的明確審閱綁定才可補足；不得依顯示名稱猜測。當來源已明確指定強度，不能用綁定覆蓋不一致的強度。

同一來源模型 ID 可以有不同實測強度，分別作為候選。跨來源或版本的同名 ID 不沿用映射。重複或衝突觀測標為 `ambiguous_evidence`，不選最好看的分數。缺失成績、未知強度、覆蓋不足、過期資料、未核可設定進入 `excluded`，不當低分。

`registry.open_source` 另行指定合格的開源設定與 `open_weight: true`，不要求它也被分進三池；不因它是開源就宣稱執行費用為零。本版保留既有 API 實付與假設開源費用為零的兩份帳。

## 時效與完整性

`retrieved_at` 表示取得時間，`evaluated_at` 表示評測時間；兩者不互相替代。`min_samples: 0` 明確允許未提供樣本數的來源，非零則不足即排除。`max_evaluation_age_days: null` 明確允許評測日期未知；若設定天數，未知日期也不能入池。`max_source_age_hours`、`max_catalog_age_hours` 與 `snapshot_ttl_hours` 同時生效，快照效期取最早到期者。

每份快照包含原始分數、來源 URL／雜湊、政策、分群、排除原因、完整 registry／observations 的輸入雜湊。來源觀測檔另保存於指定檔案。所有快照檔與 current pointer 先寫暫存並 fsync，再在鎖內原子替換；任何失敗保留上一份 current，不無聲切換來源或放寬門檻。不得以過期 current 開啟新任務。雜湊用於完整性核對，不是來源簽章；本機 store 應視為可信設定保護。

抓取只使用固定官方端點的 GET，限制回應大小，不跟隨重新導向、不自動重試。不繼承傳入 client 的憑證、cookie、自訂 headers 或 query params。錯誤不輸出遠端本文或金鑰。

## 接入核心

```python
from cloudyfan_jev.scores import ScoreCore, SnapshotStore

store = SnapshotStore("runtime/scores-coding")
# 新任務可讀 current；續跑必須讀原本 snapshot_id，不能改成最新排名。
snapshot = store.load(original_snapshot_id) if resuming else store.load()
engine = ScoreCore(
    client, snapshot,
    {"domain": "coding", "capabilities": ["tools"], "context_tokens": 12000},
    decision_profile, run_id="job-001",
)
result = engine.run("job-001", task, make_fresh_isolated_environment,
                    contract_version="public-contract-v1")
```

每層先依任務需求及供應商限制準備一個候選，不額外支付分類費；Jev 接到顯示模型名稱、必填的固定 `model_version`、強度、明確提供的供應商身份，以及公開能力卡與評分證據。候選只投影這些允許欄位；憑證、隱藏答案與其他附加欄位不會進入分類請求。固定版本變更即使同一模型家族名稱不變，分類輸入也會改變。任何一池或開源設定不符合需求，整個新任務在呼叫前拒絕，不暗中放寬。`ScoreCore` 在綁定與真正開始前檢查時效，且只允許同一 run ID。已建立 manifest 的續跑可以使用過期的原快照，仍必須維持原任務、公開契約、候選、分類設定與限制；已付費回答依既有機制重用。

**生產整合先使用 `qualified_snapshot(pack, now=...)` 通過[資格審查](qualification.md)，再使用 `ScoreCore`。** 低階 `compile_snapshot`／`SnapshotStore` 是可信程式注入介面，不會代替人員審批；CLI `build` 現在要求 `--review`。 CLI `select` 是檢視／整合輸出；將其結果交給普通 `Core` 不會額外執行評分快照時效檢查。

`ScoreCore` 僅提供 CloudyFan 路由模式；實驗基準使用原有 `Core` 明確綁定。高分池中最便宜的候選不一定是全榜最高分，不能稱它為「全最高分數模型」對照。

## 本輪驗證與未驗證

- 全部本機測試阻止真實 socket 連線，測試中的金額／回答都是 mock。
- 來源 adapter、分群、資格、時效、原子更新失敗、原快照續跑、四種示範路徑與帳務契約皆有離線測試。
- 2026-10-18 公開唯讀 LiveBench smoke：commit `90f8d44da9650d4bf4a8454157947d7b8432551e`、v1 release `2026_06_25`；解析 71 個模型、23 個項目、1,633 筆觀測。沒有提供審閱 metadata，1,633 筆強度皆保留未知；沒有發布為合格生產池。證據放在忽略目錄 `runtime/score-live-read-20261018/`。
- 固定版本與 CLI 自動發現流程皆已成功實讀。第一次 discovery 因缺少 User-Agent 收到 GitHub 403；補上固定識別並加入回歸檢查後成功，並未繞過驗證或使用帳戶憑證。
- 尚未驗證 AA／OpenRouter 的帳戶 API 存取、CodexRadar 遠端 API、LiveBench v2、真實候選的資格與跨來源門檻校準。沒有新增付費模型請求或節省率結論。
- 更新命令可交給既有排程器每日執行；本輪沒有安裝背景排程，故不宣稱部署後會自動每日更新。
- GitHub `alexwang0315/cloudyfan-routing` 於本輪開工查詢沒有任何 Issue；沒有建立／修改 Issue，沒有推送程式碼或新增 remote。

## 更新營運

新增離線 `update-preview`／`update`／`update-inspect`，以核定包顯示前後變動並保存可重跑操作紀錄。流程、並行衝突與排程啟用清單見 [score-update-operations.md](score-update-operations.md)。沒有安裝排程或實際發送通知。

## 分類投影版本

歷史政策 `hierarchical50-binary-v2` 已將候選固定版本與明確供應商身份納入分類輸入。目前政策 `hierarchical-sequential-v1` 另在兩次分類間加入實際作答／驗收，第二次分類使用最新失敗設定、答案與公開診斷。舊 manifest／v1實驗計畫不能在新版核心續跑；原始 intent、回答與核帳紀錄保持不變，不重送已付費請求。需要續用舊凍結流程時應保留原版執行環境，不能直接改寫歷史版本標記。這項相容性限制不表示新版分類效果或節省率已實測。

## 首版公開來源補證（2026-10-20）

[補證與限制](first-pool-public-evidence-20261020.md)記錄 CodexRadar 頁面明示 score 介面的免費唯讀取得；保留舊介面 410、新介面 200、新 source 版本與受控匯入。這是單次來源查核，未新增遠端自動 adapter／排程、未啟用 current，也沒有把 latest_graded_at 轉成 evaluated_at。新版統計母體與舊版不同。LiveBench 官方 HEAD／README 本次 404 留存，不影響既有離線 adapter 測試，也不假設歷史 v1 資料今天仍可重新取得。

## v2分群參考模式

首版新增明示的family_effort_prior模式，必須由first-pool-profile-v2啟用；v1投影保留。榜單仍使用單一來源／原生80／100分群，評分不證明精確API版本或SQL正確。只有通過[獨立SQL資格](sql-model-qualification-v2.md)的固定API設定，才可在有效資格與review下使用該分群；快照同時受來源、catalog、SQL資格及snapshot效期限制。未知榜單評測日期不填值，API測試日期另存，不以重抓榜單續命。

## 各類四池的可選接入

[category-qcs-v1](category-four-pool-routing.md)新增明確類別 → 該類原生品質四池 → 獨立完整成本／速度 QCS → Core.run。舊 ScoreCore 及 SQL 資格流程保留；新接入不自動辨識任務、不以來源分數核定能力、不在舊凍結任務換規則。
