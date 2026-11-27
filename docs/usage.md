# 使用說明：兩個終端機視窗完成一次來回

這份說明在一台電腦上開三個終端機視窗：一個跑轉送服務，另外兩個分別當「甲」與「乙」兩個客戶端。照著做完，甲會送一則訊息給乙、拿到回條，乙再回一則給甲，最後互傳一個檔案。

需要 Python 3.12 以上。不用安裝任何套件。以下指令都在 `cloudyfami-portal` 資料夾裡執行。

## 視窗一：啟動轉送服務

```bash
PYTHONPATH=src python3 -m cloudyfami_portal serve --db /tmp/portal-demo.sqlite3 --port 8790
```

看到這一行就代表服務起來了：

```text
Cloudyfami portal relay listening on http://127.0.0.1:8790 (db: /tmp/portal-demo.sqlite3)
```

這個視窗保持開著。按 Ctrl-C 停止。

## 視窗二：當「乙」，開始收聽

```bash
export PYTHONPATH=src CLOUDYFAMI_PORTAL_URL=http://127.0.0.1:8790 CLOUDYFAMI_PORTAL_NAME=乙
python3 -m cloudyfami_portal listen --save-dir /tmp/portal-inbox
```

乙會一直等訊息。收到的文字直接印出，收到的檔案存到 `/tmp/portal-inbox`。

## 視窗三：當「甲」，送出訊息

```bash
export PYTHONPATH=src CLOUDYFAMI_PORTAL_URL=http://127.0.0.1:8790 CLOUDYFAMI_PORTAL_NAME=甲
python3 -m cloudyfami_portal send 乙 你好
```

甲會等乙的回條，最多 10 秒。成功時印出：

```text
<訊息代號> → 乙（6 位元組）：acked
```

同時視窗二會印出：

```text
<時間> 甲 [text] <代號前 8 碼> 你好
```

## 反過來：乙回訊息給甲

在視窗二按 Ctrl-C 停止收聽，改送一則：

```bash
python3 -m cloudyfami_portal send 甲 收到了 --wait-ack 0
```

`--wait-ack 0` 表示不等回條，送進平台就結束，因為甲現在沒有在收聽。

回到視窗三，讓甲收一則就結束：

```bash
python3 -m cloudyfami_portal listen --count 1
```

甲會印出乙的「收到了」。甲沒在線時，這則訊息一直在平台排隊，所以先送後收也不會遺失。

## 傳檔案

視窗二重新開始收聽（指令同上），然後在視窗三：

```bash
python3 -m cloudyfami_portal send-file 乙 /etc/hosts
```

視窗二會印出存檔位置，例如 `/tmp/portal-inbox/hosts`。檔案會逐塊核對雜湊值，不一致不會存檔。

## 查紀錄

```bash
python3 -m cloudyfami_portal log
python3 -m cloudyfami_portal status <訊息代號>
```

`log` 列出每則訊息的每一次狀態變化與檔案傳輸統計，`status` 顯示一則訊息的完整歷程。

## 把訊息交給自己的程式

`listen --exec` 會對每則訊息執行一個指令。訊息內容從標準輸入給，另外設好四個環境變數：

| 變數 | 內容 |
|---|---|
| `CLOUDYFAMI_MESSAGE_ID` | 訊息代號 |
| `CLOUDYFAMI_SENDER` | 寄件客戶端 |
| `CLOUDYFAMI_KIND` | `text`、`signal`、`file` 或 `transfer` |
| `CLOUDYFAMI_FILE` | 收到檔案時的存檔路徑，否則是空字串 |

```bash
python3 -m cloudyfami_portal listen --exec 'echo "$CLOUDYFAMI_SENDER 說：$(cat)" >> /tmp/portal-messages.txt'
```

指令結束碼是 0 才會回條。不是 0 的話訊息不回條，平台逾時後會再交一次，所以處理失敗不會讓訊息消失。第一階段不限制這個指令能做什麼。

## 設定檔

每次都打 `--url` 與 `--name` 太麻煩時，可以寫一個設定檔 `~/.config/cloudyfami-portal/client.toml`：

```toml
url = "http://127.0.0.1:8790"
name = "甲"
```

優先順序是指令參數，其次環境變數 `CLOUDYFAMI_PORTAL_URL`、`CLOUDYFAMI_PORTAL_NAME`，最後是設定檔。設定檔也可以用 `--config` 或 `CLOUDYFAMI_PORTAL_CONFIG` 指定別的位置。

## 斷線時會怎樣

`listen` 連不上平台時不會結束，而是隔一段時間再試，間隔從 0.5 秒逐步拉長到 10 秒，並在標準錯誤印出原因。平台恢復後會接著領取還沒收到的訊息。收過並回條的訊息不會再出現；回條在斷線時沒送出去的，平台會再交一次，`listen` 會直接補回條，不會重複印出。
