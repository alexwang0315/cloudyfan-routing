# 顧問先答流程（路徑二，#73）

依據：ADR 0034 第 1、2、4、10 點。設定、凍結與預算預留見 `docs/advisor-first-config.md`（#72）。

## 白話說明

客戶端選用路徑二（`mode='advisor_first'`）時，題目先交給開源顧問。顧問只能回一個固定格式的 JSON：

- `mode: "answer"`：自己直接回答，並附至少兩組池內備援建議；
- `mode: "delegate"`：判斷自己答不了，附至少兩組池內建議，依優先順序排列。

直接回答通過公開驗收就結束，不必付任何閉源模型的費用。沒通過或選擇改派時，MRC 不再問顧問，直接依序派送建議的池內模型。每題最多 3 次作答（顧問直接回答也算一次）。建議用完、全部作廢或顧問格式不對，就照實記錄後結束：不設預設接手模型，也不回頭走 Jev。

## 已做

- `src/cloudyfan_jev/advisor_flow.py`：
  - `advisor_messages()`：顧問輸入只有公開題目（先以 `routing._reject_private` 檢查整個 task，再經 `routing.project_public`）與池內設定識別（設定名、層級、模型版本、強度、供應商）。題目含 `hidden`、`private`、`gold`、`expected_answer` 等私有欄位時，在任何 I/O、建立 manifest 之前就拒絕。
  - `parse_decision()`：嚴格 JSON。整段內容必須是單一 JSON 物件（不接受 ```json 包裝），只允許 `mode`／`answer`／`recommendations`；`answer` 模式要有非空回答，`delegate` 模式不得附回答；建議至少 2 組。每組建議必須剛好有 `setting`、`tier`、`reason`，並對應到凍結池內同名、同層級的設定；不合規的逐項作廢並記錄原因（`malformed`、`not_in_pool`、`tier_mismatch`、`duplicate`）。解析失敗、建議少於 2 組或全部作廢，一律視為顧問未完成（`incomplete_reason` 為 `invalid_json`／`invalid_schema`／`too_few_recommendations`／`no_valid_recommendations`），連顧問的回答也不採用。顧問供應商回 429 拒絕時記為 `provider_rejected`。
  - `run()`：manifest 凍結、`advisor-decision.json`、`validation-a0.json`（顧問回答的驗收）與 `result.json` 都是不可覆寫的證據；重跑時內容不同即拒絕。
- 收據 `call_id`：顧問 `run-<id>-advisor`；顧問回答記為第 0 次作答（沒有生成呼叫）；第 n 組建議的作答為 `run-<id>-a<n>-t<turn>`。同一 `call_id` 續跑讀舊收據，不重付。
- 作答模型只收 `system` 與原題，不收顧問的判斷理由或顧問的回答草稿。
- 沒有驗收器（驗收回 `passed: None`）時，顧問直接回答記為 `unverifiable`，不算通過。
- 結果欄位：`advisor`（`calls`、`decision`：outcome、有效建議與理由、作廢清單、`score_snapshot`）、`attempts`（每次加註 `source`＝`advisor_answer`／`delegated`、`setting`、`pool_tier`、`recommendation_rank`）、`status`（`passed`／`failed`／`unverifiable`／`advisor_incomplete`）、`end_reason`（`passed`／`attempt_limit`／`recommendations_exhausted`／`advisor_incomplete`）、`selection` 恆為 `None`（不走 Jev）、`costs`（含 `advisor_usd`）。
- `engine.py` 的小改動：
  - `run_advisor_first()` 改為呼叫 `advisor_flow.run`。
  - 新增 `_generation_profile(tier)`：一般層級照舊查 `profiles`；路徑二作答收據的 tier 記為 `pool:<設定>`，查凍結的池內設定。`_attempt` 與 `_with_costs` 改用它，其他模式行為不變。
  - 兩個掛鉤：`_advisor_answer_messages(task)`（#76 在此附契約經驗的解題事實）與 `_advisor_delegated_attempt(...)`（#74 迴圈守衛裁判在此包住工具迴圈）。本單只放預設行為，不實作裁判與經驗卡。
- `AdvisorConfig.descriptor()` 多凍結 `flow`（`advisor-first-flow-v1` 與顧問系統提示全文）；改提示就是改設定，舊題續跑會被拒絕。

## 已測（全部離線，MockTransport）

`tests/test_advisor_first.py`（30 項）：直接回答通過；直接回答失敗改派第一組（不再問顧問）；第一組失敗改派第二組；建議用完即結束、不走 Jev；含直接回答最多 3 次；改派模式可用滿 3 次；`max_attempts` 設定生效；池外／層級不符／重複／格式錯的建議逐項作廢；9 種 JSON 失敗都記為顧問未完成且不派送；顧問 429；無驗收器時 `unverifiable`；作答模型只收原題；私有欄位在任何呼叫前被擋；驗收中斷後續跑不重付顧問與作答、完成後重讀一致；改設定拒絕續跑；自訂池設定派送其凍結設定並正確記帳；紀錄可追溯；`BudgetedCore` 預留後實際跑完路徑二。`tests/test_advisor_config.py` 中原本斷言「尚未實作」的一項改為斷言會先呼叫顧問。

## 仍未知／未做

- 迴圈守衛裁判（#74）只留掛鉤，未實作。契約經驗卡已由 #76 接上，見 `docs/advisor-experience-card.md`。
- 改派作答共用同一個工作區（與路徑一相同，工具動作照舊重播），但新模型只看到原題，看不到前一次的驗收診斷；這是依「只收原題」的字面規則，是否該附公開驗收診斷待實測後再議。
- 顧問直接回答若是 `unverifiable`，目前仍會繼續改派（比照路徑一「無法確認時」升級）；沒有驗收器的題目因此會用滿作答次數。是否改為無驗收器時不改派，待使用者決定。
- 顧問直接回答只驗收其文字回答，不能使用工具；需要改動工作區的題目，顧問實際上只能選改派。
- 池內設定與評分快照入池資格的自動對照仍未做：目前以凍結的 `pool` 為準，`score_snapshot` 只記錄身分。
- 容量佇列（`queue_initial`）尚未支援 `advisor_first`。
- 沒有呼叫任何真實模型；費用數字都是測試假值，顧問好不好用仍未知。
