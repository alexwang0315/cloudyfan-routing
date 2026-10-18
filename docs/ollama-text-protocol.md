# Ollama 文字協定子集與互通證據（T08）

本版固定 CloudyFan 子集 **`cloudyfan-ollama-text-v1`**；離線協定消費端 **`cloudyfan-ollama-protocol-consumer-v1`** 位於 `tests/test_ollama_api.py`，透過 httpx WSGITransport 操作公開 HTTP seam，模型上游為 MockTransport。官方文件核對日期 **2026-10-18**。沒有安裝／執行 Ollama runtime、官方 SDK 或產品 client。

Ollama 官方說明其 API 未嚴格版本化，因此本文件的版本是本地凍結的子集與消費端版本，不能解讀成已通過某個 Ollama runtime 版本。[官方版本說明](https://docs.ollama.com/api/introduction)

## 凍結的範圍

|Endpoint|要求與結果|
|---|---|
|`GET /api/tags`|回傳 `models`，每筆只有 name/model 路由別名；cloudyfan.protocol_version 明示子集。沒有宣稱本機已安裝權重，不編造 size/digest/量化資料。|
|`POST /api/generate`|僅接受 model、prompt、stream 三欄，stream 必須明確為 false。公開驗收通過後回傳 model、created_at、response、done=true 與 cloudyfan metadata。|
|`POST /api/chat`|僅接受 model、messages、stream。messages 必須恰好一個 `{role:"user",content:"純文字"}`。結果以 message.role/content 回傳。|
|`/v1/tasks/{run_id}` 與 `/result`|沿用 T07 狀態／結果查詢，GET 不重新生成或核帳網路呼叫。|
|`POST /v1/tasks/{run_id}/resume`|T07 明確續跑入口，本文必須 `{}`；維持原契約、候選、工具、預算與未核帳停止規則。|

原生模型查詢與文字欄位參照 [tags](https://docs.ollama.com/api/tags)、[generate](https://docs.ollama.com/api/generate)、[chat](https://docs.ollama.com/api/chat)。官方串流預設開啟；此子集會拒絕省略 stream、true 或非布林 false，避免默默改變請求語意。[串流說明](https://docs.ollama.com/api/streaming)

以下都明確不支援，未知欄位亦拒絕：多輪 history、tools/tool calls、圖片／音訊／影片、streaming、context、system/template/raw、format、options、think、keep_alive，以及建立／下載／刪除／推送模型、embeddings、`/api/ps`、`/api/version` 等其他 endpoint。錯誤使用 HTTP 狀態與原生頂層字串 `error`；不回傳任意例外文字。[官方錯誤格式](https://docs.ollama.com/api/errors)

沒有偽造 token 數、載入／生成時間或實際下游模型身分。外層 `model` 是使用者指定且伺服端註冊的 **路由別名**，不是證明某一物理權重在執行。完整流程 API 費用來自同一 TaskService 帳務，包含分類、修正與失敗；無法核帳維持 null。一般 Ollama client 未必理解 CloudyFan metadata，必須另驗證。

## 必要的應用層擴充

每個請求沿用注入的身分驗證；即使使用 localhost 也不取消 CloudyFan 的驗證邊界。這與一般本地 Ollama 預設免驗證不同，是明確的相容限制。[官方驗證文件](https://docs.ollama.com/api/authentication)

生成／聊天額外要求 **`X-CloudyFan-Run-ID`** header，呼叫端在第一次送出前固定 run ID。model 必須精確對應 `OllamaApp` 的伺服端 alias→registered contract map；不能由請求送入驗收器、shell、工具、隱藏答案或預算設定。沒有註冊的客觀契約就拒絕；任意聊天不因回答非空變成通過。

```python
from cloudyfan_jev.ollama_api import OllamaApp

# service 是 T07 已設定的 TaskService；此建構不啟動 listener。
app = OllamaApp(
    service,
    models={"cloudyfan/arithmetic:v1": "arithmetic"},
    authenticate=trusted_authenticator,
)
```

公開驗收 `False` 回傳 422 / public_validation_failed；`None` 回傳 422 / public_validation_unavailable。回答與驗收狀態仍可透過原 run 的 `/result` 查閱。200/done=true 只發給經註冊公開契約通過的任務；這不是對其他問題正確率的推論。

成功／失敗回應的 `cloudyfan` 會帶 protocol_version、run_id、status、costs（若任務已建立）。未建立任務的身分／輸入錯誤僅回傳 error，不推測費用。policy_denied 為 403；accounting_interrupted 為 409；engineering_interrupted 為 503。

## 斷線與重連

呼叫端先保存 run ID，再送出一次請求。讀取逾時／斷線後查詢 `/v1/tasks/{run_id}` 與 `/result`，不能另建 run ID 重送。相同 run ID、相同輸入的 native POST 只讀取已存在狀態；若原任務已開始但中斷，不隱含續跑。原狀態仍為 ready 才可第一次執行。相同 ID 改 prompt 或契約會拒絕。

- 回應遺失但任務已完成：GET 讀回原結果；相同 native POST 不重送生成。
- 驗收中斷：GET 顯示 engineering_interrupted；native POST 維持中斷，只有明確 `/resume` 才續跑，已付費答案仍重用。
- 上游 timeout／可能已付費且費用未知：保留 accounting_interrupted 與未知費用，查詢、native POST 或 `/resume` 都不自動重送。
- 不支援串流，所以未驗證串流中途取消；這也不是實際 TCP 掉線、反向代理重試或商用 client 的證據。

## 證據矩陣與 G-02

|項目|協定 fixture|指定實際 client|狀態／限制|
|---|---|---|---|
|模型別名查詢→generate 單輪純文字→公開驗收／完整帳務|已測|未測|HTTP WSGI consumer v1；不宣稱權重已安裝|
|單一 user message 的非串流 chat|已測|未測|不是多輪 client 互通|
|驗證、未知契約、重複 key、非法／過大輸入|已測|未測|拒絕發生在模型呼叫之前|
|公開驗收失敗／未知|已測|未測|不把非空回答當成功|
|模擬回應遺失後查詢／同 run 重用|已測|未測|非真實網路斷線測試|
|上游 timeout 未核帳停止、明確驗收續跑|已測|未測|MockTransport；沒有付費請求|
|多輪、工具、多模態、串流與其他 API|明確拒絕|未測|不在本子集內|
|官方 Python／JS SDK、Open WebUI 或其他產品 client|未作為消費端|未測|待選定版本、header／alias mapping 與實際測試|
|真實模型 runtime／Mac 硬體／部署|未測|未測|本票不授權下載、啟動或部署|

**G-02 實際 client 整合仍未完成。** 本票交付的是可測的明確文字子集與缺口報告；不以 fixture 通過取代產品 client 的整合放行，也不宣稱完整 Ollama 相容。
