# Claude Code 作為 MRC 客戶端

## 目的

讓 Claude Code（或任何能跑 shell 指令的代理）以**一般客戶**身分把子任務送進 MRC，並讀回答案、每一層實際使用的模型、Jev 選池理由與費用。這樣不必改核心，就能從代理的角度觀察 MRC 的選模與計費流程。

這不是讓 Claude Code 本身的模型呼叫改由 MRC 處理。Claude Code 需要 Anthropic Messages API、工具呼叫與串流；MRC 目前的客戶入口只支援單輪純文字、非串流，兩者不相容。

## 組成

| 檔案 | 作用 |
| --- | --- |
| `src/cloudyfan_monitor/mrc_client.py` | 客戶端與 CLI：`check`、`ask`、`show` |
| `scripts/mrc-task` | 以核心 `.venv` 執行 CLI，路徑設定與 `scripts/preview` 相同 |
| `.claude/skills/mrc-task/SKILL.md` | 告訴 Claude Code 何時、如何使用，以及回報時的限制 |
| `tests/test_mrc_client.py` | 示範組裝與 IdentityStore 組裝下的 6 項測試 |

## 邊界

- 只走既有客戶 API：`POST /v1/tasks`、`GET /v1/tasks/<id>/result`、`GET /v1/monitor`。不匯入伺服器物件、不使用操作員憑證，也不能指定模式或模型池。
- 只接受明確帶埠號的 loopback HTTP 位址，規則與 `customer_simulation.local_origin` 相同。
- 憑證只從 `CLOUDYFAN_MRC_TOKEN` 讀取；`--demo` 使用 `demo.TOKENS["agent"]` 這個公開示範憑證，而且服務必須自報 `demo: true`。CLI 不接受憑證參數，輸出也不會帶出憑證。
- 相同 run ID 與內容重送沿用核心既有的冪等行為：只讀回原結果，不新增派送。
- 錯誤不自動重試，以 JSON `code` 回報。

## 試跑

```bash
./scripts/preview --legacy-demo --port 18791 --data /path/to/new-data
export CLOUDYFAN_MRC_ORIGIN=http://127.0.0.1:18791
./scripts/mrc-task --demo check
./scripts/mrc-task --demo ask --prompt "Return only the integer sum of 1 and 1."
```

示範服務的上游是 MockTransport，驗收器是算術環境：答案、模型（`example/*`）與費用都是合成資料，非算術題的「通過」沒有意義。新版預設 `./scripts/preview` 用 IdentityStore 隨機發放客戶憑證且不顯示，目前沒有提供給外部客戶取得憑證的指令；要接它，需要另外決定憑證發放方式。
