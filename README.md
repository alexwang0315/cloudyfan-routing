# cloudyfami-portal

Cloudyfami 平台的平台轉送：把一個客戶端的訊息送給另一個客戶端，兩端各自連到平台，不必在同一個網路。依據 [ADR 0039](../docs/adr/0039-platform-relay-of-hca-summaries-to-granted-clients.md) 與 [ADR 0040](../docs/adr/0040-cloudyfami-platform-one-software-two-deployments.md)，整體規格見 [Cloudyfami 平台規格 v0.1](../docs/cloudyfami-portal-spec-v0.1-2026-11-27.md)。

目前完成 [#85](https://github.com/alexwang0315/cloudyfan-routing/issues/85)：訊息信封、轉送服務與網際網路（HTTP）這一條鏈路。**只在本機驗證過**，還沒有部署，也還沒在兩台不同網路的設備上測過。第一階段不做授權檢查，客戶端以名稱自報身分，只適用於內部測試環境。

## 執行

只用 Python 標準函式庫，需要 Python 3.12 以上。測試借用 `../cloudyfan-mrc` 的環境裡的 pytest。

在本機跑一次完整來回：

```bash
PYTHONPATH=src python3 -m cloudyfami_portal demo
```

啟動轉送服務：

```bash
PYTHONPATH=src python3 -m cloudyfami_portal serve --db /tmp/portal.sqlite3 --port 8790
```

執行測試：

```bash
../cloudyfan-mrc/.venv/bin/python -m pytest -q -p no:cacheprovider
```

## 結構

| 檔案 | 負責 |
|---|---|
| `envelope.py` | 訊息信封。完整形式是 JSON；精簡形式是一個不超過 200 位元組的封包，供 LoRa 使用。兩種形式可互相轉換，內容不變 |
| `relay.py` | 轉送服務：排隊、領取、回條、狀態查詢與傳送紀錄，存在 SQLite。它不知道訊息走哪條鏈路 |
| `http_link.py` | 網際網路這條鏈路：HTTP 轉接層，以及主動連出的客戶端 |
| `__main__.py` | `demo` 與 `serve` 兩個指令 |

## 訊息信封

| 欄位 | 內容 |
|---|---|
| 訊息代號 | 16 位元組的隨機值。同一個代號重送只算一次 |
| 寄件與收件客戶端 | 名稱，1 到 32 位元組，不含控制字元與 `/` |
| 建立時間 | 整數秒。用整數是為了讓精簡形式不損失精度 |
| 類型 | 文字、短訊號、檔案 |
| 內容 | 位元組 |
| 大小與雜湊值 | 由內容算出。完整形式會附上並在接收時核對；精簡形式只帶雜湊值前 4 位元組 |

精簡形式的固定開銷是 28 位元組加上兩個名稱的長度。超過 200 位元組時明確拒絕，不截斷；分段屬於 LoRa 鏈路轉接層（#93）。

## 投遞狀態

```text
已排隊 queued ──► 已送達 delivered ──► 已回條 acked
   │                  │
   └──► 已過期 expired / 失敗 failed ◄──┘
```

狀態只能往前。已回條、已過期、失敗是終點。「已送達」代表收件端領走了，「已回條」代表收件端確認收到。

## HTTP 介面

客戶端一律主動連出。

| 方法與路徑 | 作用 |
|---|---|
| `GET /health` | 健康檢查 |
| `POST /v1/messages` | 送出一則訊息 |
| `GET /v1/messages/{id}?until=acked&wait=秒` | 查一則訊息的狀態與歷程；可等到指定狀態 |
| `GET /v1/inbox/{客戶端}?wait=秒&limit=則數` | 領取排隊中的訊息，沒有訊息時可等待 |
| `POST /v1/inbox/{客戶端}/{id}/ack` | 回條 |
| `GET /v1/transmissions` | 傳送紀錄 |

## 新增一條鏈路

鏈路轉接層只需要呼叫 `Relay` 的 `submit`、`claim`、`ack`，並傳入自己的鏈路名稱。`tests/test_relay.py` 裡的 `FakeLink` 是最小範例，它用精簡形式收發，`relay.py` 不需要修改。

## 還沒做的

- 沒有回條時的重新投遞、過期、斷線重連（#86）。目前領走但沒回條的訊息會停在「已送達」。
- 大檔分塊（#87）。目前單則上限約 8 MB，整則放在記憶體裡。
- 命令列收發工具（#88）、MCP 伺服器（#89）。
- LoRa 鏈路（#93）、基地部署與斷網切換（#94）。
