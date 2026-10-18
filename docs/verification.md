# Initial extraction verification — 2026-10-18

Verified locally on macOS with CPython 3.13.13:

- `uv sync --extra dev`: independent virtual environment and locked dependencies installed.
- `uv run pytest -q`: **205 passed**. Tests prohibit real socket connections; all API evidence is synthetic MockTransport data.
- `uv run ruff check .`: passed (F rules).
- Offline demos: pass = 1 mock request / 0 classifiers; low = 3 / 1; mid = 4 / 2; high = 4 / 2. All final public validators passed.
- `uv build`: source distribution and wheel built. Both exclude runtime, workspaces, credentials and receipt directories.
- Wheel installed in a separate virtual environment, run from `/tmp` with Python isolated mode and no PYTHONPATH: high-path demo passed. No dependency on the original workspace package.
- All 10 original source-file hashes recorded in `provenance.json` remain unchanged.

Extraction review found and fixed two new orchestration/accounting edge cases, with regression tests:

1. A completed workflow now rejects a reconciled receipt whose model/provider no longer matches its admitted profile.
2. A duplicate generation whose call ID sorts before the original no longer hides the original known charge. The duplicate remains unresolved, and no trusted grand total is fabricated.

Malformed tool invocations stop as engineering failures with preserved billable evidence. `ledger.summarize` includes incomplete intents and failures even when no completed workflow exists.

No paid API request was made for extraction, validation, review or repository creation. These checks do not establish new routing savings, production deployment, Windows support or new model qualification. CI is configured for macOS/Linux and Python3.12/3.13 but has not run on a remote host.
# 評分資料層增量驗證（2026-10-18）

- `uv run pytest -q`：310 passed（包含原有 205 項及新增 105 項），真實 socket 連線在測試中禁止。
- 最後的來源 HTTP User-Agent 修正後，重跑 `uv run pytest -q tests/test_score_adapters.py`：65 passed。
- `uv run ruff check .`、`git diff --check`：通過。
- `uv build`：sdist 與 wheel 成功；沒有新的 runtime dependency。
- `uv run cloudyfan-score-demo --route high`：合成評分匯入、三池、快照、兩次二選一與修正驗收通過；4 個 MockTransport 請求、真實 API 呼叫 0。pass／low／mid／high 四路徑另有測試。
- `uv run cloudyfan-scores fetch livebench --output runtime/score-live-read-20261018/discovered-observations.json`：真實公開唯讀、自動發現並固定版本成功；1,633 筆觀測、71 個模型、23 個項目。未知強度保持未知，未發布到生產池。
- 不使用 API key、不做付費生成、沒有新的實驗降本率。AA／OpenRouter authenticated access、CodexRadar 遠端 API、正式門檻與可執行模型映射、背景排程均未驗證／未啟用。詳見 [score-integration.md](score-integration.md)。

# 主規格 #1 T01～T12 與終審修正（2026-10-18）

本機 CPython 3.13.13；固定既有評分層 baseline `0ddd24ddcfa3ea5a5bd7d6d8d65a82d9e691ea94`。測試全程禁止真實 socket 連線。實作、使用文件與正式缺口對照見 [implementation-spec1.md](implementation-spec1.md)。

- `uv run --locked --offline --extra dev pytest -q`：**529 passed**（原有 310、12 張 ticket 新增 212、終審來源欄位回歸新增 7）。
- `uv run --locked --offline --extra dev ruff check .`、`git diff --check`：通過。
- HTTP／Ollama／排隊公開介面：94 passed；包含首答、兩層路由後修正、未知來源、核帳後合法模型別名更新、跨使用者拒絕，以及重複查詢不新增模型 POST。
- 終審修正採 red → green：HTTP 缺少 provenance、合法核帳後仍返回舊模型別名、Ollama 缺少 provenance 均先出現失敗，再修正通過。
- 共用 Core 設定與獨立 filesystem ReceiptStore 重構後，既有 client／local／budget／engine 公開契約測試：80 passed。
- `uv run --locked --offline --extra dev cloudyfan-jev-demo --route high --output <private-scratch>`：通過，4 個 mock 請求、2 次分類、open_source → high、最終公開驗收通過。
- `uv run --locked --offline --extra dev cloudyfan-score-demo --route high --output <private-scratch>`：通過，合成三池／固定快照到相同 high 路徑；4 個 mock 請求、真實 API 呼叫 0。

終審補上安全結果來源資訊，並讓它與已驗證的核帳收據一致；共用設定描述消除多層凍結欄位差異，本地 transport 與雲端 transport 改共用純檔案基底。這些測試證明離線契約，不能推論真實模型品質、provider cap、第三方客戶端、硬體、商業 SLO 或新節費率。本輪沒有新增付費呼叫、下載模型、操作硬體、部署或推送程式碼；歷史驗證數字保留於前文。

# Retro follow-up：統一驗證與跨接入契約（2026-10-18）

本輪以 `82197b5` 為基線，變更提交至本機整合分支。新增[統一入口](checks.md)、[接入契約矩陣](transport-contracts.md)與[正式放行索引](release-readiness.md)，未改動模型路由 production code。

- `./scripts/check --integration`：**567 passed**（既有 529＋跨接入矩陣 32＋驗證入口／hook 6）、Ruff、依賴一致性、Core／ScoreCore high demo、sdist／wheel 皆通過。兩個 demo 各為 4 次 mock 呼叫，真實 API 費用零。
- 接入相關四檔聚焦驗證：126 passed。三種程序內錯誤注入各被四條協定路徑攔截：來源 provider 遺失、未知費用變零、讀取結果新增模型請求。正常程式未改動；未載入注入 plugin 的測試重新通過。
- 驗證入口測試涵蓋同步失敗停止、缺少 uv 無 fallback、專案 Python、測試失敗阻止 demo／build、hook 鏈順序、舊秘密掃描失敗與必要 hook 缺失停止。
- 沒有 `.venv` 的全新來源副本，於既有依賴快取下執行 `--quick` 通過；刻意提供錯誤 PYTHONHOME／PYTHONPATH／VIRTUAL_ENV／UV_PROJECT_ENVIRONMENT，仍建立該副本的 `.venv` 並成功匯入 pytest。這是新 checkout／暖快取驗證，不是沒有快取仍可離線安裝。
- 本機 repo 專屬 hook 已安裝並實際執行 quick check；原全域 hook 檔案雜湊不變，其他 hook 以 symlink 保留。入口實作階段沒有建立 commit，後續依使用者授權完成本機提交；未推送。檢查腳本另經獨立 read-only review，未發現具體問題。
- GitHub workflow 已改成依賴準備後呼叫相同入口，**遠端 CI 未執行**。應用／測試依賴沿用 uv.lock；isolated build dependencies 使用 build-system 範圍，不宣稱位元一致建置。

每次入口結果寫入 `runtime/checks/<id>/checks.json`。新 checkout、hook 保存／安裝、錯誤注入及最終內容核對證據保存在專案工作區 `.scratch/retro-improvements/`；這些私有本機證據沒有上傳。此輪無付費 API、部署、推送或 GitHub 工作單寫入。正式放行索引的所有適用真實資格仍為 pending。
