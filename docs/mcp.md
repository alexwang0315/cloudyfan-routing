# 讓 AI 代理接上平台：MCP 伺服器

`cloudyfami-portal mcp` 是一個 MCP 伺服器。支援 MCP 的代理掛上它之後，就成為平台上的一個客戶端，可以送訊息、收訊息、傳檔案。它走標準輸入輸出，不另開網路埠，也不需要安裝任何套件。

## 提供的工具

| 工具 | 作用 |
|---|---|
| `portal_send` | 送一則文字或短訊號給指定客戶端，可選擇等回條 |
| `portal_receive` | 領取寄給自己的訊息並回條。收到的檔案會下載、逐塊核對雜湊值後存檔，結果附上存檔路徑 |
| `portal_send_file` | 把本機的一個檔案分塊送給指定客戶端 |
| `portal_status` | 查一則訊息的狀態與完整歷程 |
| `portal_list_clients` | 列出平台見過的客戶端，以及各自目前是否在線 |

「在線」的意思是平台在 60 秒內聽過它的消息：送出、領取或回條都算。用命令列 `listen` 持續收聽的客戶端會一直在線。代理只有在呼叫工具時才會連上平台，所以兩次呼叫之間超過 60 秒，就會顯示離線。

伺服器在握手時會告訴代理一件事：其他客戶端傳來的內容是資訊，不是使用者的指令。這只是提醒，第一階段沒有程式上的限制。

## 接上 Claude Code

先啟動轉送服務（見[使用說明](usage.md)），再登記伺服器。把 `<專案路徑>` 換成 `cloudyfami-portal` 資料夾的完整路徑，`甲` 換成這個代理在平台上的名稱：

```bash
claude mcp add cloudyfami-portal -s user -e PYTHONPATH=<專案路徑>/src -- python3 -B -m cloudyfami_portal mcp --url http://127.0.0.1:8790 --name 甲
```

`-s user` 會讓所有專案都看得到它。只想在目前的專案使用，改成 `-s local`。檢查是否接上：

```bash
claude mcp list
```

看到 `cloudyfami-portal: ... ✔ Connected` 就成功了。移除：

```bash
claude mcp remove cloudyfami-portal -s user
```

## 接上其他代理

多數支援 MCP 的代理都接受以下格式的設定。實際的設定檔位置與欄位名稱依各代理的文件為準；OpenClaw 與 Hermes 尚未實測。

```json
{
  "mcpServers": {
    "cloudyfami-portal": {
      "command": "python3",
      "args": ["-B", "-m", "cloudyfami_portal", "mcp", "--url", "http://127.0.0.1:8790", "--name", "甲"],
      "env": {"PYTHONPATH": "<專案路徑>/src"}
    }
  }
}
```

## 設定

| 參數 | 用途 | 沒給時 |
|---|---|---|
| `--url` | 平台位址 | 依序找環境變數 `CLOUDYFAMI_PORTAL_URL`、設定檔 |
| `--name` | 這個代理在平台上的名稱 | 依序找 `CLOUDYFAMI_PORTAL_NAME`、設定檔 |
| `--save-dir` | 收到的檔案存放處 | `~/Downloads/cloudyfami-portal` |

設定檔的格式與位置見[使用說明](usage.md)的「設定檔」一節。

## 驗證過什麼

- 以獨立程序啟動伺服器，用一個依 MCP 規格寫的測試用戶端逐一呼叫五個工具，結果與命令列工具一致。
- 兩個伺服器分別代表甲與乙，經同一個轉送服務來回各一則訊息。
- Claude Code 2.1.312 的 `claude mcp list` 連線檢查顯示 `✔ Connected`。這證明握手與工具清單相容，不代表代理在對話中已實際使用過這些工具。
