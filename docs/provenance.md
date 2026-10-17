# 抽取來源與行為差異

原工作區沒有 Git history，因此用 `provenance.json` 的原檔 SHA-256 固定來源，沒有假造來源 commit。原始實驗檔與舊 A/B、alpha/beta 不修改。

|新檔案|原始來源|改動|
|---|---|---|
|routing.py|hierarchical50-20261018/routing.py|僅套件相對 import；保留原 policy version、tenant、提示、二層規則|
|client.py|hierarchical50-20261018/client.py|僅獨立套件文件說明；保留 durable POST／核帳語意|
|decisions.py|src/cloudyfan/decision_v2.py|保留純型別、payload、parser、fingerprint；移除舊 provider adapter、HTTP 層與 cache 實體|
|errors.py|src/cloudyfan/errors.py|原錯誤契約|
|engine.py|hierarchical50-20261018/driver.py 的 workflow／attempt／selection|以可注入驗收器與工具環境取代 dataset、絕對路徑、固定題數、實驗排程；保留四種模式與兩次生成上限|
|ledger.py|新增|完整 durable 帳本摘要，包含沒有完成 workflow 的呼叫|

新 engine 不是原實驗 runner 的逐 byte 重播：通用 system prompt 移除合成快照等實驗專屬要求；使用明確 task／contract version／config manifest；replay 要求工具結果完全一致而不只 `ok` 一致；核帳後重新檢查模型／供應商。新增 entry point 只跑 MockTransport demo。

沒有複製原資料集、隱藏答案、原始回應、generation IDs、鍵值、客服草稿、帳戶憑證、工作區或原6,262份歷史成果。能力卡只含當時公開校準的彙總與樣本數警告。

`tests/test_routing.py`、`test_client.py`、`test_decisions.py` 搬移原契約測試，新增完整 engine integration tests。測試禁止真實付費 HTTP；沒有新的 API 成本。

本 repo 的初始版本為0.1.0；路由 `VERSION=hierarchical50-binary-v1` 保留以識別原選模政策，名稱中的50是原題庫名稱，不能解讀為已跑50題。
