# 路徑二經驗卡（#76，依 ADR 0036 第 10、11 點）

## 白話說明

顧問先答路徑（路徑二，`mode='advisor_first'`）每題開頭，MRC 從契約經驗庫（#75）組一張不超過 2 KB 的「經驗卡」給顧問，讓它知道三件事：

1. **自己答這類題的成績**：顧問在此契約／類別直接回答的通過率——決定要不要自己答的主要依據；
2. **池內各設定的成績**：派送次數、通過率、平均成本——顧問據此排建議，但仍只能選池內設定；
3. **解題事實**：這類題常見的錯誤指紋與驗收檢查碼。

交給作答模型時，作答模型**只收到解題事實**，以參考區塊整塊附在原題後面，看不到任何成績或顧問理由。

只用於路徑二。路徑一（`cloudyfan` 等原有模式）不收經驗：傳了 `experience` 會在任何呼叫前拒絕。

## 怎麼用

```python
from cloudyfan_jev.experience_card import ExperienceLink
config = AdvisorConfig(..., experience=ExperienceLink(store, path='experience.json'))
core.run(run_id, task, env, contract_version='v1', mode='advisor_first',
         experience={'client_id': 'c1', 'scope': 'sql_query_v1', 'created_at': '2026-11-17'})
```

- `scope` 是契約／類別代碼；三個值都只收不含空白的代碼型字串。
- 設定了 `ExperienceLink` 但這題沒傳 `experience`：照 #73 原本流程跑，不附卡、不回寫。
- 沒設定 `ExperienceLink` 卻傳了 `experience`：拒絕。

## 已做

- `src/cloudyfan_jev/experience_card.py`
  - `build_card()`：三塊內容；成績依「精確模型版本＋強度＋供應商＋模型池快照（`score_snapshot`）」分組，換版、換供應商或自動換池（快照改變）後不沿用舊成績。
  - 可驗收的題目少於 5 題標 `sample: "insufficient"`，且不給通過率（`pass_rate: null`），顧問系統提示說明此時改以模型池分數為主。
  - 沒有驗收器的題目（`unverifiable`）另計，不算通過，也不算進樣本數與通過率分母。
  - 客戶端凍結設定 `use` 為開時用共用成績；關閉時只用該客戶端自己的成績。解題事實取 `facts_for()` 的私有層＋共用層（共用層用改寫後的通用敘述）。
  - 大小上限：顧問看到的卡片 JSON（UTF-8）≤ 2048 bytes；超過時從尾端**整條**省略解題事實，並記 `facts_omitted`；連一條事實都沒有仍超過就拒絕。
  - `facts_block()`：參考區塊沿用 `context_preparation` 的固定框架 `\n\n[參考區塊 <id>]\n<text>`，區塊 id 為 `experience-<卡片編號>`（卡片編號是內容雜湊，即版本）。沒有事實時不附區塊，原題一字不變。
  - `freeze()`：建題時組卡，存成不可覆寫證據 `runs/<id>/experience-card.json`；續跑讀回、核對雜湊後沿用同一張，不重新讀經驗庫。卡片被改動就拒絕續跑。
  - `write_back()`：每題結束回寫一筆觀察——顧問直接回答記 `role: advisor`，改派記 `role: solver`，結果 `passed`／`failed`／`unverifiable`、實際費用、模型池快照；`supports` 取未通過作答的公開檢查碼、#74 `loop_guard.experience_findings()` 的發現（主動與影子模式都收；`tool_error` 指紋轉成 `error_fingerprint`「工具.code.category」，`validation` 檢查碼轉成 `check_code`；裁判判斷、換方向提示不進經驗庫）。只存代碼，不存題目或答案。同一題重寫內容相同時不重複計算；存檔後、回寫前中斷時，下次讀已完成結果會補寫。已被要求刪除的題目不回寫。
- `experience.py`（#75）小擴充：代碼允許 `/`（模型版本如 `vendor/model-v1`）；成績可帶選用的 `pool_snapshot`；`model_stats(..., pool_snapshot=)` 篩選；`is_withdrawn()`。舊快照格式不變。
- `advisor.py`：`AdvisorConfig.experience`；設定時描述多凍結 `experience`（卡片版本、上限、樣本門檻、顧問提示附註、事實區塊標頭）。未設定時描述與 #73 完全相同。
- `advisor_flow.py`：顧問輸入多 `experience_card`，系統提示補一段固定附註（只是參考資料，不能改題目、池或權限）；結果多 `experience`（卡片編號、經驗庫版本、事實區塊 id）。
- `engine.py`：`Core.run(..., experience=None)`；`_advisor_answer_messages(task, card=None)` 掛鉤附事實區塊。`BudgetedCore.run` 原樣轉傳並凍結在預留描述中。

## 已測（全部離線，MockTransport）

- `tests/test_experience_card.py`（10 項）：空庫全標樣本不足；滿 5 題才給通過率；`unverifiable` 另計；池內成績與平均成本；換模型池快照、換模型版本、換供應商都不沿用；事實區塊格式與版本；共用事實用改寫敘述；2 KB 上限與整條省略；關閉共用使用只看自己。
- `tests/test_advisor_experience.py`（15 項）：顧問收到 ≤ 2 KB 卡片；作答模型只收事實區塊；沒有事實時原題不變；結果與發現回寫；`unverifiable` 回寫；卡片凍結、續跑沿用、回寫只算一次；卡片被竄改拒絕續跑；不傳經驗時路徑二不變；路徑一拒收；設定與身分檢查；描述凍結；迴圈守衛（主動／影子）的工具錯誤指紋與重複驗收檢查碼以真實 #74 流程回寫；`BudgetedCore` 轉傳。

## 未做／未知

- `http_api.TaskService` 與 Monitor 尚未把客戶端、契約範圍傳成 `experience`；目前只有程式介面（客戶端層級設定在 #80）。
- `task_id` 直接用 `run_id`；不同 MRC 根目錄若重用同一 `run_id` 寫同一個經驗庫，會因內容不同被拒絕。
- 未通過作答的檢查碼一律記為「支持」，不記反例；何時算反例（例如帶事實後仍犯同錯）未定。
- 顧問建議本身（`advisor_advice`）尚未回寫。
- 經驗庫存檔沒有跨程序鎖（#75 已知限制）。→ 已由 `ExperienceStore.update` 檔案鎖解決，見下方「主服務接上經驗庫檔案與改寫器」。
- 卡片多花的輸入成本、經驗是否真的降低升級成本，都要等 ADR 0036 的時間序列對照評估；沒有呼叫任何真實模型。

## 任務服務帶入客戶端與契約範圍（2026-11-19）

- `TaskService.create` 在新題建立時，若註冊契約的路徑（路徑二或路徑一加強版）接上了經驗庫，就把經驗身分凍結進 `job.json` 的 `experience`：
  - `client_id`：驗證後的 principal（客戶＝客戶端，ADR 0009）；不是代碼型字串時改存 `client-sha256:<雜湊>`，原字串不進經驗庫。
  - `scope`：註冊契約 ID（同樣不合代碼格式時改存雜湊）。
  - `created_at`：建題的 UTC 日期。
- `execute` 把凍結的身分原樣交給 `Core.run(experience=...)`；重送不改身分。沒接經驗庫的契約不寫這個欄位，行為與先前相同。
- 尚未做：以「類別」而非契約 ID 當範圍；Monitor 畫面尚未顯示每題的經驗身分。

## 主服務接上經驗庫檔案與改寫器（2026-11-19）

白話：主服務現在有一個固定的接法，把經驗庫存成一個檔案，多個程序共用；
「把事實改寫成通用敘述」不在作答時做，改成另外批次做，先預留預算再呼叫開源顧問。

```python
from cloudyfan_jev.experience_rewrite import open_experience, rewrite_pending
link = open_experience('runtime/experience.json')            # 啟動時組裝一次
config = AdvisorConfig(..., experience=link)                  # 路徑二；路徑一加強版同樣傳 link
# 維運批次（另外排程，不在作答流程中）：
rewrite_pending(runner, 'runtime/experience.json', advisor_profile, run_id='experience-rewrite-20261119', limit=10)
```

- `open_experience`：讀既有檔案（沒有就第一次回寫時建立），改寫器固定為 `defer_rewrite`。
  作答回寫時，新達共用門檻的事實只標成「待改寫」（`pending`），**作答流程不呼叫任何改寫模型**，
  所以每題的最壞預留不必包含改寫。
- 組卡前重讀檔案（`ExperienceLink.refresh`，路徑二與路徑一加強版都會）：其他程序的回寫、
  Monitor 改的「貢獻」「使用」開關，在下一題建卡時就生效，不會沿用啟動時的舊內容。
- `rewrite_pending`：
  - 改寫器設定必須明示 `open_source: True`，且要有已確認的費用上界；缺上界就在任何呼叫前拒跑。
  - 一批最多 `limit` 條；先以 `BudgetedCore` 的預算庫預留「筆數 × 上界」，每次呼叫照收據記帳。
  - 呼叫編號 `experience-rewrite-<事實編號>-<支持集合雜湊前 16 碼>`：同一批中斷後用同一個 `run_id` 重跑，讀舊收據、不重付；
    和題目流程一樣，只有整批呼叫都完成才釋放預留。
  - 改寫器只收到契約範圍、事實種類與代碼；回覆只收單行、≤ 400 字元的敘述。
  - 結果持檔案鎖寫回：通過去識別檢查才進共用層（`accepted`），否則 `rejected`；
    改寫期間支持集合變了則記 `stale`，維持待改寫，下一批再改。
  - 被擋下的事實，之後有新的支持才會再排進待改寫。

已測（`tests/test_experience_wiring.py`，12 個，全部 MockTransport）：作答回寫只標待改寫、開檔沿用同一鹽值、
改寫後共用層出現敘述、預留金額與固定呼叫編號、寫回前中斷後同批續跑不重付、外洩敘述被擋、支持改變後重新排隊、過期結果不套用、
`limit` 同時限制呼叫數與預留、非開源或缺上界拒跑、Monitor 在另一程序關掉「使用」後新卡不再附共用事實。

未做／未知：
- 沒有排程器或指令列入口來定期跑 `rewrite_pending`；目前由主服務自行呼叫。
- Monitor 的示範組裝尚未改用 `open_experience`。
- 真實開源顧問的改寫品質、每條改寫的實際費用都沒測（沒有呼叫任何真實模型）。
