# 首版模型池接入與更新（POOL-01～05）

沿用 qualified_snapshot、ScoreUpdater、ScoreCore 及 BudgetedCore。來源、SQL 驗收器、選模樹與舊實驗結果均不在執行途中變更。

## 一次整理既有來源

```sh
.venv/bin/cloudyfan-scores prepare-first-pool \
  --profile examples/first-pool-profile.json \
  --output-dir runtime/first-pool-review-01
```

此命令只讀本機 JSON，保存 report、qualification-pack、observations、registry、policy、review、requirements、decision 與 fee-gaps。目錄必須不存在；任何中斷保留部分材料，使用新目錄重試，不覆寫舊資料。沒有 fetch／current 切換或模型呼叫。

目前真實歸檔必須退出 **1／blocked**：精確版本映射、SQL 能力、時效與核定缺證。它是可讀的交付結果，不是 CLI 壞掉。來源選擇與缺件見[來源紀錄](first-pool-source.md)，費用見[缺證紀錄](first-pool-fee-gaps.md)。不能以測試用 approval、刷新日期或正式題答案補足。

## 資格與受控執行入口

prepare_pool(profile, now=...) 保留輸入及候選缺證，產出 hash 綁定報告。只有結構完整、逐設定映射／操作證據、既有 review 與來源時效皆符合時，才可取得有效資格快照。來源／設定／能力卡／政策改變必須重新產出與核定，不挑正式題調參。

核定輸入中的 capability_card 另外包含精確證據與 admission context 的 digest，涵蓋 configuration_evidence、來源選擇、需求、分類設定及 synthetic 身份。請以報告 qualification_pack.input_sha256／匯出的有效 registry 作審查綁定，不直接對原 profile.registry 算 hash。更換任一上述輸入時，舊 review 必須失效；沒有自動補核定。只有 digest 進公開卡，不將任意管理證據本文送入分類。

qualified_pool_core(client, report, run_id=..., budget_store=..., model_bounds=..., verify_bound=...) 是本版受控接入，回傳 BudgetedCore 包住的 ScoreCore。它重新核對整份報告，再沿用資格入口；正式模式拒絕合成／缺證包。費用保障、有效授權、完整預留與 unknown stop 獨立檢查，不因模型池合格就豁免。沒有內建真實供應商 verifier。

原任務續跑使用原 report／pack，Core 繼續核對任務、設定、公開契約及已存答案。原快照過期可完成原任務；新任務不可使用過期快照。低階 compile_snapshot／ScoreCore 仍是可信程式接口，不能繞過本入口便宣稱正式資格。

## 更新及人工操作

1. 將新來源觀測另存；保留真實取得日期與原始來源鏈，不修改既有觀測。
2. 準備新 profile 與 input-bound review，重新執行 prepare-first-pool。blocked 時先補具體缺證，停止，不改用其他來源或放寬門檻。
3. 依 [update-preview／update／update-inspect](score-update-operations.md) 使用前後 qualification-pack。確認差異、效期、責任及實際快照 ID 後，以新 operation ID 更新。
4. 操作失敗依原始 audit 處理；相同輸入可續跑，輸入改變用新 ID。並行衝突讀回 current，不能覆蓋另一個成功更新。
5. 新任務選取新包，舊任務仍取原包。缺證、過期、來源失敗或 unknown fee 均不觸發自動模型請求。

來源使用／更新 owner 仍待具名指派。首版可人工更新，不需要新增背景排程、通知或接完全部來源。正式 store 與任何 synthetic store 必須分開。

## 可重現的完整離線示範

```sh
.venv/bin/python -m cloudyfan_jev.scores.pool_demo \
  --output runtime/first-pool-demo-01
./scripts/check --integration
```

示範內建 MockTransport，沒有 API key／URL 選項：使用不可呼叫的 example 模型、AI fixture 核定與模擬費用上限，SQL 真正由既有 SQLite 子程序驗收。覆蓋首答通過、低分、中分、高分修正；第二版為明示合成分數變動。驗證新舊快照、到期拒絕、新任務費用缺證拒絕及保存答案零 POST 續跑。

報告 synthetic=true、production_ready=false；金額只標 simulated_api_usd。測試旗標 allow_synthetic 不是一般網路隔離器，更不是付費授權；測試必須注入 MockTransport，不將模擬 verifier 用於正式執行。

## 完成狀態與限制

本批交付的是可用的**本機整理／受控接入／更新及離線演示機制**，以及首版真實資料缺證包。沒有正式合格的真實三池、真實來源更新、目前模型可用性或供應商費用保障。RG-02／03／04 仍 pending，不修改其他 gate。

RG-01 維持凍結與必要回歸；不再審 S01～S08，不重新跑 50 題，不重算既有正確率或節費率。本輪無付費 API、來源網路請求、供應商聯絡、硬體操作、部署或推送。
