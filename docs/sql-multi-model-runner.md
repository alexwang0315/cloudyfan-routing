# SQL主備候選：多設定資格執行器

這個版本讓四池主備位置能指向各自的資格考試，不再為了只有四個role而複製考試或混用供應商費用。這是**候選考試的工程接入**，不是CloudyFan路由成效測試，也不核定模型或啟用模型池。

原`QualificationRunner`／`sql-model-capture-v1`保留四個role及其預設ID／預算語義。新增`MultiCandidateQualificationRunner`／`sql-model-capture-v2-multi`使用獨立`candidate-exams/`、請求前綴及預算命名空間；原manifest不改寫、舊請求不重送。驗收器、50題資格題庫、公開契約與隱藏判分器不變，RG-01維持凍結。

## 先準備，再明確選擇要考的設定

先用[候選與證據對照](sql-backup-candidate-evidence-20261021.md)整理四池12位置；這張表會保留兩版低池提案、缺欄位、過期、舊研究適用範圍及新備援流程缺證。準備結果不會呼叫模型。

新API位於`cloudyfan_jev.scores.sql_multi_runner`：

- `prepare_multi_candidate_table(entries, *, slots=None)`：接收明確的entry清單，按既有`configuration_digest`去重，保留首次出現順序；輸出固定suite、精確設定hash及候選ID。最多12個唯一設定，不因跨池佔兩位置重考。
- `slots`為池名到configuration SHA256清單的對照，各池最多3個不同模型；同模型不同供應商可分別考，但不能在同一池冒充3個不同模型。部分考試可以只考需補證的設定；它不表示12個位置已填滿。
- 不自動把完整盤點全部送考、不讀舊50題答案或其他組答案挑候選。是否重用舊能力紀錄先看實際設定、題目與契約，不自動匯入舊答案封存。只有明確選定的可執行entry才交給此API。
- `model_bounds`必須以**configuration SHA256**為鍵。相同模型不同provider／強度／參數各自綁定上界，不得只按模型名挑最低費用。不可在同樣wire請求下，靠改量化等不能指定的metadata假稱兩個可區分設定。

精確身分契約仍要求`accepted_models == [model_version]`、固定provider、`allow_fallbacks: false`與明確reasoning參數。完整盤點profile不一定符合這個考試契約：舊研究的多alias回應清單不得偷偷縮成單一值後沿用舊hash；provider-default或未知effort不得改成none。需改契約時另立設定及計畫，保留不能重用的原因。`:free`請求不得換成付費canonical。

## 執行與費用

構造器沿用原Client、BudgetStore及上界驗證器，由應用注入；不尋找憑證、不建立支出授權、不提供預設真實HTTP CLI。`synthetic=True`必須實際使用`httpx.MockTransport`，不能把真實網路標成模擬。

`capture(run_id, max_new_requests=...)`先為N個設定各作身分確認，再依Q001～Q050輪替設定先後。每個設定每題最多兩次作答；公開驗收通過便不補答，修正只看自己的上一個答案與公開診斷。Jev分類為0，不換模型升級；升級是另外的路由驗證範圍。

完整預留為`101 × sum(各唯一設定可強制單次上界)`，即每設定1次身分確認＋50次首答＋最多50次修正。所有實付、失敗及未知事件留在共享帳本；沒有授權、缺上界、額度不足、費用未知、身分不符或429時停止新派送。每個設定已包含的API費用不因佔多個slot而重複加算，核帳total取代usage費用而非相加。

續跑先回放原intent／response／公開驗收checkpoint，再更新原帳務事件；不自動GET核帳、重試或補送不明請求。完成後的答案封存、billing revision與明確核帳分開，核帳不改原答案；改table、slot或來源版本不沿原exam ID換規則。

`records(run_id, candidate_id)`只讀保存的回答。`grade(run_id, now=...)`必須整個新考試capture完成及封存一致，才讀隱藏考官；回傳每個設定的成績及slot引用，仍為`formal_qualification: pending`、`pool_activated: false`。個別設定的成績不能當整批路由品質或Jev增益。

## 離線重現

```sh
.venv/bin/python -m pytest -q tests/test_sql_multi_runner.py tests/test_sql_model_runner.py tests/test_generation_request.py
.venv/bin/python scripts/demo_sql_multi_runner.py --output runtime/sql-multi-demo-NEW
```

示範只用虛構模型與MockTransport，固定5個唯一設定（包含同模型不同供應商及非推理`:free`設定），真SQLite驗收、公開補答、驗收中斷續跑、完成後零POST重播，再明確做模擬核帳與獨立判分。考官答案映射只在模擬上游端，不進入capture或選模。合成金額不是報價、真實帳單或供應商保障。

真實候選能力、別名契約、當前健康、完整路由成本／速度、Jev配對校準及費用保障仍各自待核對。[本輪證據](evidence/sql-backup-multicandidate-offline-20261021.json)會區分現行檔案、保留舊副本的整個工作區檢查、模擬演練及未驗證事項。沒有付費模型、來源GET、供應商聯絡、部署或推送。
