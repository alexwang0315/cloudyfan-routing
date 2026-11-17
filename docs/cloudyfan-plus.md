# 路徑一加強版（`cloudyfan_plus`，#79）

依據：ADR 0034 第 8 點、ADR 0036 第 10 點。迴圈守衛見 `docs/loop-guard.md`（#74），經驗卡見 `docs/advisor-experience-card.md`（#76），經驗庫見 `docs/contract-experience.md`（#75）。

## 白話說明

純路徑一（`mode='cloudyfan'`：開源首答 → Jev 判斷升級到低／中 → 再判斷中／高）一字不改，留作比較基準。另做一個**明示選用、預設關閉、有版本**（`cloudyfan-plus-v1`）的路由模式 `cloudyfan_plus`，只多兩樣：

1. **迴圈守衛裁判**：每次作答的工具迴圈都由 #74 的 `guarded_attempt` 包住。同一錯誤重複、或修復作答裡工具回報的驗收檢查碼和上一次作答失敗時的公開檢查碼相同，就請開源裁判看一次；把握度夠才插一句換方向提示；插過仍卡住就**結束本次作答**（公開診斷記 `LOOP_GUARD_STUCK`），交回 Jev 照原規則決定升級到哪一層。開源首答階段就會作用，不依賴 Jev 被觸發。
2. **解題事實**：建題時從契約經驗庫取出這個客戶端、這個契約／類別的解題事實，凍結成一份只含事實的卡片（`runs/<id>/experience-facts.json`），以參考區塊 `[參考區塊 experience-<卡片編號>]` 整塊附在原題後面。卡片編號是內容雜湊，就是版本。

**模型成績只記錄、不使用**：每題結束時，各次作答的成績（精確模型版本、強度、供應商、通過與否、費用）回寫經驗庫，標記為 `pool_snapshot='cloudyfan-plus-v1'`，不會混進路徑二的池內成績。Jev 的分類輸入仍是原題、上一次答案、工具結果與公開診斷，**不含任何經驗成績或卡片**（ADR 0017）；要讓經驗影響選層需另寫 ADR。

## 怎麼用

```python
from cloudyfan_jev.cloudyfan_plus import CloudyfanPlusConfig
from cloudyfan_jev.loop_guard import LoopGuardConfig
plus = CloudyfanPlusConfig(judge={..., 'open_source': True},
                           loop_guard=LoopGuardConfig(mode='active'),   # 預設 shadow
                           experience=ExperienceLink(store, path=...))   # 選用
core = Core(client, profiles, decision, cloudyfan_plus=plus)
core.run(run_id, task, env, contract_version='v1', mode='cloudyfan_plus',
         experience={'client_id': 'c1', 'scope': 'sql_query_v1', 'created_at': '2026-11-17'})
```

- 沒給 `cloudyfan_plus` 設定：`cloudyfan_plus` 模式不存在（`Unknown workflow mode`）。
- 有設定但跑 `mode='cloudyfan'`：請求、manifest、結果與沒設定時逐位元組相同（設定只凍結在選用它的題目、預留與契約中）。
- `experience` 只在 `advisor_first` 與 `cloudyfan_plus` 可用；純路徑一各模式照舊在任何 I/O 前拒收。`cloudyfan_plus` 沒接 `ExperienceLink` 卻傳了 `experience` 也拒絕。
- `RegisteredContract(mode='cloudyfan_plus')` 要求 Core 有設定，並把設定凍結進契約描述。

## 已做

- `src/cloudyfan_jev/cloudyfan_plus.py`（新模組）：`CloudyfanPlusConfig`（裁判須明示 `open_source`；`judge_calls_per_attempt` 0 或 1；`loop_guard` 預設影子模式）、`descriptor()`（版本、裁判、守衛設定、`stats_policy='record_only_not_routed'`、事實卡規則）、只含事實的卡片組裝／凍結／核對（≤ 2 KB，超過從尾端整條省略）、`attempt()`（帶入上次公開檢查碼給偵測器）、成績與發現回寫（`LOOP_GUARD_STUCK` 不當成解題事實）。
- `engine.py`：`Core(..., cloudyfan_plus=None)`；`run()` 在 `cloudyfan_plus` 下沿用 `cloudyfan` 的階段與 Jev 選層，只換掉「附事實」與「單次作答」兩處；`_with_costs` 以本模式的裁判設定核對裁判收據、輸出 `judge_usd`，開源裁判進零模型費用情境；`read_completed` 核對凍結的本模式設定。
- `budget.py`：`_admit('cloudyfan_plus')` = 純 cloudyfan 預留 ＋ 3 個作答階段 × 每次裁判次數 × 裁判上界；缺裁判上界拒跑（裁判停用時不需要）；本模式設定凍結進預留指紋。
- `http_api.py`：契約選用本模式時的檢查與凍結。
- QCS（`selection_policy`）與本模式不能並用：執行與預留都拒絕。

## 已測（全部離線，MockTransport）

`tests/test_cloudyfan_plus.py`（19 項）：未選用時模式不存在；有設定時純 cloudyfan 逐位元組相同；插過仍卡住即結束作答、交回 Jev（Jev 照原規則選 low，輸入帶 `LOOP_GUARD_STUCK`）；提示有效時不升級；影子模式作答請求與純路徑一完全相同；裁判停用；修復作答撞回上次檢查碼觸發裁判；事實參考區塊格式、版本、manifest 凍結；Jev 輸入不含任何成績字樣（經驗庫已有 6 題成績時）；有無成績時 Jev 請求與純路徑一完全相同，成績有記錄且不混進路徑二快照；續跑沿用凍結卡片、回寫只算一次、卡片被改拒絕續跑；改設定拒絕續跑與重讀；純路徑一拒收經驗；身分格式檢查；QCS 不可並用；預算預留數字、缺裁判上界拒跑、預留指紋凍結設定；契約註冊。

## 未做／未知

- ADR 的導入順序是「先守衛、再事實」；程式兩樣都可用，`experience` 不傳就只有守衛，實際分階段開啟由營運設定決定。
- `ledger.summarize` 的角色欄目前只接受路徑二 `AdvisorConfig`；本模式裁判收據在全收據彙總中沒有 `role` 欄（單題結果的 `judge_usd` 正確）。
- `http_api` 的 provenance 只列作答與 Jev 呼叫，不列裁判呼叫（費用仍計入）。客戶端層級選用本模式屬 Monitor（#80）。
- 與 QCS 並用、與備援模型池執行期（`scores/backup_runtime`）並用都未支援。
- 何時把「帶事實後仍犯同錯」記為反例未定（與 #76 相同）。
- 守衛門檻、事實是否真的降低升級成本，都沒有真實數據；沒有呼叫任何真實模型，費用都是測試假值。三組並列比較（純路徑一／加強版／路徑二）屬 #81。
