# G-02：固定官方 Python SDK 的離線互通

本輪驗證官方 **`ollama==0.6.3` 的同步 `Client`**，搭配
**`cloudyfan-ollama-text-v2`**。SDK 是實際安裝的官方套件，不替換其
`list`、`generate`、`chat`、序列化、回應型別或錯誤解析方法。
版本固定在 `pyproject.toml` 的 dev extra；`uv.lock` 固定相依版本與套件雜湊。
執行環境、套件來源及已安裝 SDK 原始碼雜湊見
[client manifest](evidence/g02-sdk-manifest.json)。其他版本須重做資格驗證。

## 驗證邊界與實際修正

```
官方 ollama.Client
  → httpx.WSGITransport（記憶體內 HTTP 傳輸）
  → OllamaApp / HTTPApp + 真實 BearerAuthenticator / IdentityStore
  → TaskService + Core / BudgetedCore + 註冊公開驗收
  → httpx.MockTransport（模型與帳務上游）
```

測試沿用禁止真實 socket 的全套 guard；沒有啟動 listener、Ollama runtime、
模型權重、付費 API 或部署。WSGI 的 `https` scheme 只驗證身份政策，
不是實際 TLS 握手或反向代理證據。

官方 SDK 的無工具 `chat` 仍送出 `tools: []`，原 v1 會以 422 拒絕。
v2 只對 chat 接受**型別確實為 list 且為空**的工具欄位，作為無操作欄位移除。
非空工具、null、false、0、字串及物件仍拒絕；generate 不接受 tools 欄位。
既有 v1 支援的純文字請求保持可用；協定識別明確升為 v2。

## 應用接入要求

應用先保存 run ID，再為該任務建立 SDK Client，明確設定 host、Bearer
credential、`X-CloudyFan-Run-ID`、timeout，且停用非必要的自動 redirect。
同一任務的重連沿用同一 ID；不要把一個帶固定 header 的 Client 用於不同任務。
生成需 `tasks:write`，讀取狀態／成本需 `tasks:read`，列出別名需 `models:read`；
這些 scope 不會互相授權。
以下是已核定應用環境中的接入範例，不會由文件自動執行：

```python
from ollama import Client

# endpoint / credential / saved_run_id 由應用設定與持久化提供。
with Client(
    host=endpoint,
    headers={
        'Authorization': 'Bearer ' + credential,
        'X-CloudyFan-Run-ID': saved_run_id,
    },
    timeout=30.0,
    follow_redirects=False,
    trust_env=False,
) as client:
    reply = client.generate(
        model=registered_alias,
        prompt=public_prompt,
        stream=False,
    )
    answer = reply.response
```

`model` 是伺服端註冊的路由別名，不是已載入物理權重的證據。
SDK module-level 函數或未設定身份／run ID 的任意既有 client 不在這個接入契約內。
SDK 的 `list()` 可解析只含別名的 tags；不會因此取得權重大小或量化資格。

SDK 的 `GenerateResponse`、`ChatResponse`、`ListResponse` 會忽略 CloudyFan
擴充欄位；不能從 typed response 讀取完整成本、路由 provenance 或 run 狀態。
應用以獨立的標準 HTTP client、同一 Bearer 身份查詢：

- `GET /v1/tasks/{saved_run_id}`：流程狀態與單任務成本。
- `GET /v1/tasks/{saved_run_id}/result`：答案、公開驗收、完整成本與 provenance。

這是既有 CloudyFan HTTP 擴充，不是官方 SDK 內建方法。查詢不會再次生成，
也不會自行向模型供應商做 GET 核帳。任務未完成時 `/result` 可以回傳 409；
先讀 status，不把讀不到結果當成可以新建任務的依據。

## 已驗證的行為

|情況|官方 SDK／公開 HTTP 的觀察|上游結果|
|---|---|---|
|tags、generate、單一 user chat|實際 typed response；答案為公開驗收通過的 `2`|一次模擬生成|
|缺少／無效／撤銷／停用／過期／缺 write scope|`ResponseError(401, unauthorized)`|零模型請求|
|models:read 與 tasks:write 分離|能列出別名不等於能生成，反向亦然|拒絕操作不派送|
|缺少或不合法 run ID、未知別名|SDK 收到 422／404 的安全錯誤|零模型請求|
|公開驗收 false／unknown|SDK 收到 422；GET 保留 failed／unverifiable|分類、修正與失敗費均保留|
|預算不足|SDK 收到 403|零模型請求|
|公開驗收器工程中斷|503；同 run native POST 不隱含續跑|只允許明確 `/resume`，重用原答案|
|任務完成後下游回應遺失|SDK 拋出 ReadTimeout，沒有自動重送 POST|GET 或相同 run native POST 讀回原結果，總生成仍一次|
|抵達服務前的模擬連線失敗|SDK 拋出 ConnectionError，沒有自動重試|零模型請求；確認沒有任務後沿用原 ID|
|上游 timeout 且費用未知|SDK 收到 409；GET complete cost 為 null|重連／重複 native POST／resume 均不重送|
|streaming、多輪、非空工具、圖片與不支援參數|SDK 收到具體 422 錯誤；stream iterator 在開始迭代時拋出|零模型請求|
|show、embed、pull 等未映射原生 endpoint|正式 Bearer scope mapping 先以 401 拒絕，非 `endpoint_not_supported` 404|零模型或下載動作|

最後一列與注入寬鬆 fixture authenticator 時的 404 不同：正式身份元件不授權
未知 endpoint。它們未被默默支援，但不能聲稱 SDK 取得了 endpoint-specific
錯誤說明。沒有讓 client 自動 pull、降級或略過驗收的 fallback。

## 重現與證據

```sh
uv run --locked --offline --extra dev python -m pytest -q \
  tests/test_ollama_sdk.py tests/test_ollama_api.py
./scripts/check --integration
```

聚焦 SDK／協定檢查為 **83 passed**，其中 40 項官方 SDK、43 項原生協定；
本輪新增 46 項回歸案例（40 SDK＋6 空工具欄位邊界）。完整檢查結果與實際收據
另記在 [verification.md](verification.md)；原始 red／green／套件準備紀錄保留於
工作區 `.scratch/g02-sdk/`，不納入原始 prompt 或憑證。

第一次檢查也揭露 SDK 會忽略擴充欄位，測試依既有 HTTP 擴充讀取帳務；
這不是宣稱修改 SDK 使其保留 metadata。真正的 chat 422 相容性問題在正常 SDK
請求上先重現，再以最小 v2 修正通過。

## 放行範圍

這補齊 **指定同步 SDK＋固定文字子集＋模擬上游**的 G-02 證據。
正式 registry 的 pending 不由測試自動核定；需由放行責任人依精確範圍審閱證據。
未驗證 AsyncClient、JS SDK、Open WebUI、OpenClaw、真實 TCP 中斷、TLS、代理重試、
並行取消或部署；不宣稱完整 Ollama 相容。
模型／領域品質、供應商完整費用上限及實際身份／保留部署仍分屬
RG-02／RG-04／RG-05，未由這輪 SDK 測試取得資格。

套件依據：[官方 Python SDK](https://github.com/ollama/ollama-python)、
[固定版本套件](https://pypi.org/project/ollama/0.6.3/)。SDK 行為以本輪實際安裝
版本與測試觀察為準，不以會變更的 main README 取代版本證據。

## 本機 Git 保存

第一次提交前複核使用原程式指紋，重新跑完整檢查取得 844 passed；
[新收據摘要](evidence/g02-precommit.json)與 [verification.md](verification.md)
保留此次結果。實際 commit 身分見本機 Git 歷史；不把歷史基線當成本輪來源。
正式 SDK scope 審閱後，下一個技術驗證優先 RG-04 真實供應商完整費用上限與核帳，
不以本輪 MockTransport 的正向路徑取代供應商資格。

首次 commit 被秘密掃描對無效 credential fixture 的直接字串指派攔下，
調整為測試動態產生無效值後，完整 844 項重新通過；應用程式與 SDK pin 未變。
[最終收據摘要](evidence/g02-precommit-final.json)固定此次調整後的測試來源，
前一筆收據保留為歷史，沒有略過 hook。
