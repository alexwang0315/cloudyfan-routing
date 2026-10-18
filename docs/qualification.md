# 任務領域模型池資格審查包（T02）

審查包是離線可重現的審查輸入與阻擋報告，並不是排行榜即時資格證明。`cloudyfan-qualification-pack-v1` 保留完整輸入、原生分群規則、來源證據、排除原因與核定紀錄；不擬合門檻、不讀 holdout、不呼叫模型或來源網站。

## 產出與核定

```sh
cloudyfan-scores qualify --observations observations.json --registry registry.json \
  --policy policy.json --review review.json --output qualification-pack.json
```

缺少核定可用空物件 `{}` 作為 review，命令仍成功交付 `status: blocked` 的報告；成功寫檔不代表候選合格。輸出採 exclusive create，既有資料不覆寫。CLI `build` 要求相同輸入及 `--review review.json`，通過核定後才更新 snapshot store；沒有核定時不建立 store。

review 的 `input_sha256` 必須是 `catalog.digest({"observations": observations, "registry": registry, "policy": policy})`。任何來源、供應商、版本、強度、門檻、覆蓋或有效期變更，舊核定便不適用。每個領域使用單一來源／版本／metric，所有原生尺度與精確模型映射仍由既有 catalog 檢查。

review 分別包含 `domain`、`source`、`thresholds`、`coverage`、`expiry`、`access`，以及 `models` 下每個 `config_id`（含開源首答設定）的核定。每筆核定格式：

```json
{"status":"approved","reviewer":"responsible-reviewer","reviewed_at":"2026-10-18T10:00:00Z","evidence":["review-record-reference"]}
```

任何未核定、缺審閱者、無證據參照、未標時間或未來時間都維持待決。這些是**可信管理者的核定聲明**，不是密碼學簽章，也不會自動確認證據參照內容是否真實；本地設定與審查紀錄須受存取控制。不得把此範例值當作實際核定。

審閱內容至少包括：

|領域|必要證據|
|---|---|
|domain|任務範圍、公開能力需求；不含正式測試答案|
|source|單一主來源、版本、評測項目與來源觀測的適用性|
|thresholds|原生 low/mid/high 邊界及事先選擇依據；禁止從 holdout 調參|
|coverage|樣本涵蓋、缺項處理；min_samples=0 的明確核定理由|
|expiry|來源、可執行設定、評測日期及快照有效期|
|access|實際使用通道、帳戶存取資格、資料使用／再散布條款的核對證據|
|models|來源設定到精確版本／強度／供應商的對照、可執行能力與資格；成本和延遲估計的依據|

三池預覽只使用原 catalog 資格資料，不替未批准的模型把 `approved` 改成 true。`effort_unknown`、`ambiguous_evidence`、`source_stale`、`score_missing`、未批准設定等仍排除，不能填入低分池。來源/領域等尚未核定時，預覽仍可存在，但整包不能發佈。缺少合格開源設定時整體 preview 可為 null，阻擋理由明確保留，審查輸入不丟失。

排序沿用成本估計→延遲估計→穩定設定 ID，未知估計排最後。輸入保留 `cost_basis`、`estimated_request_usd`、`estimated_latency_ms`；審查者須核對估計依據。估計不是實付，也不是成功機率。不能藉由修改排序忽略模型資格。

## 消費格式

- `pack_id`：完整包雜湊；`input_sha256`：所有 observations、registry、policy 的精確綁定。
- `status`：`blocked`／`synthetic_only`／`qualified`；`production_ready` 只有正式核定且全部必要條件滿足時為 true。
- `inputs`、`review`：可重現來源與核定依據；`preview`：既有快照格式（含三池、開源設定、排除原因）。
- `blockers`：穩定文字碼，例 `thresholds:review_pending`、`model:low:review_pending`、`low:no_qualified_candidates`。
- `qualified_snapshot(pack, now=...)`：檢查包完整性、核定與當下效期，將 `qualification: {pack_id,status,production_ready}` 放入快照並重算 snapshot_id。
- 後續更新或實驗計畫必須保留 `pack_id` 與 snapshot_id，不因先前 ticket 結案繼承真實資格。

`--synthetic` 與 `qualified_snapshot(..., allow_synthetic=True)` 僅用於明確隔離的離線測試。合成資料永不產生 `production_ready: true`。本旗標不是網路隔離器；執行測試時必須注入 `httpx.MockTransport`。既有 `compile_snapshot`／`SnapshotStore`／`ScoreCore` 是可信程式接口，不會攔阻呼叫者刻意繞過 review；正式接入須採上述核定接口，CLI build 已強制使用此路徑。

## 本輪證據與正式缺口

- 合成案例從資格包→三池各一個候選→固定快照→ScoreCore/Core.run→開源首答失敗→Jev 低／中→低分修正通過，全程 mock。
- 既有 LiveBench 公開唯讀結果不能自動轉成正式池；缺少強度映射時維持未知。
- AA 與 OpenRouter authenticated access 未在本單驗證；CodexRadar 遠端 API 未驗證，僅受控匯入。
- 本單未核定任何真實領域、來源、門檻或模型，未新增付費、來源網路請求、部署或資料授權；正式生產池仍須相應審查證據。
