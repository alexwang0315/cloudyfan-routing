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

# Retro follow-up 3–5：正式放行、介面分工與接手狀態（2026-10-18）

本輪基線為 `d8af729668a0e4a2ebe6572d7f50d26ff1db8efa`，驗證對象為其上尚未提交的工具／測試變更；程式、測試與建置輸入以 [去敏證據摘要](evidence/retro35-offline.json) 的 `source_digest` 固定，不把基線 commit 冒稱為本輪已測提交。

- `./scripts/check --integration`：**660 passed**（既有 567＋新增 93）、Ruff、依賴一致性、專案紀錄檢查、Core／ScoreCore high mock demo、sdist／wheel 均通過。兩個 demo 各 4 次 mock 請求、2 次分類，真實 API 費用零。
- 新增放行條件檢查 41 項、共享介面／分工檢查 20 項、接手狀態檢查 31 項，以及入口 fail-fast 1 項。涵蓋過期／缺漏／錯誤範圍證據、不當資格標記、循環與責任重疊、來源摘要失效、歷史誤用及授權限制。
- 獨立唯讀審查發現 registry 頂層為陣列時會崩潰；已先檢查形狀並補陣列／null／數字／字串回歸案例，完整測試包含修正。
- [目前接手狀態](handoff.md) 與分工完成紀錄在完整驗證後更新，再執行 `./scripts/project-state check`；本輪離線完成與正式資格分開。10 項正式 gate 仍 pending，沒有具名核定或真實接入證據。
- 歷史 `.scratch/implement-spec-1/` 狀態／協調／merge 紀錄加註歷史用途與新入口，原有過程內容保留。此次沒有變更 `src/` 的路由行為。

完整命令退出碼收據：`runtime/checks/7429bafa8c8d4ff6b637d1f348a95924/checks.json`。本輪沒有付費 API、模型下載、硬體操作、部署、推送、GitHub 寫入或新的 Git commit。下一項正式資格優先規劃 G-02 固定版本真實客戶端互通，可先以 mock 上游隔離模型品質與費用；本輪未執行該資格測試。

# 全實作審查五項修正（2026-10-18）

本輪修正兩項 P1 預算控制缺陷與三項 P2；細節、相容性與限制見 [review-fixes.md](review-fixes.md)。保留既有 retro 3–5 未提交變更與全部歷史證據，基線仍為 `d8af729668a0e4a2ebe6572d7f50d26ff1db8efa`；新測試對象以 [來源摘要](evidence/review-fixes-offline.json) 固定，不冒稱已提交。

- `./scripts/check --integration`：**699 passed**，比審查前 660 項增加 39 項；Ruff、依賴一致性、專案紀錄、Core／ScoreCore high mock demo、sdist／wheel 全部通過。
- 兩個 demo 各 4 次 mock 請求、2 次分類，真實 API 呼叫與費用零。完整退出碼收據：`runtime/checks/5c418db4ac2346578c10a8b57c72b313/checks.json`。
- 兩項 P1 補強共享停止、核帳風險預留與逐次派送曝險檢查，包含舊 schema 資料庫、重啟、跨 client 與舊收據回放測試；預算聚焦驗證 26 passed。獨立唯讀複核未發現新的阻擋缺陷，另完成雙 SQLite 連線並行核帳／派送探針。
- P2 補強本地 intent 前父目錄 fsync、候選模型版本投影、校準成本缺漏不可當零。路由版本升為 `hierarchical50-binary-v2`，原始收據保持不變；舊版凍結流程會在 POST 前拒絕續跑。
- 完整驗證後更新工作單狀態與去敏證據，再核對 `project-state`、文件連結與 diff。十項正式放行條件仍 pending，離線通過不代表真實 provider cap、模型品質或突然斷電持久性已驗證。

本輪沒有付費 API、硬體、模型下載、部署、推送、GitHub 寫入或新 Git commit。私有修正與驗證記錄位於工作區 `.scratch/review-five-fixes/`。

# 本機提交前複核（2026-10-18）

依使用者授權，將上述五項修正與它們依賴、前輪已審查的 retro 3–5 放行／分工／接手檢查工具一併保存至本機 Git。歷史驗證與原始私有收據保持原檔，沒有把原始 prompt、回答或 runtime 加入提交。

- 重新執行 `./scripts/check --integration`：**699 passed**；Ruff、鎖定離線依賴、紀錄檢查、兩個 mock demo 與 sdist／wheel 皆通過，8 個步驟退出碼皆為 0。
- 本次來源摘要與上輪修正後完全相同：`e64ea1909715a9ba1e99c197cf0ee0b9f18ba388fb7c383668361cb410ba0f46`。完整收據：`runtime/checks/7051d29469ba4d2aae5a9b09de8fa9e9/checks.json`；可提交的去敏摘要：[review-fixes-precommit.json](evidence/review-fixes-precommit.json)。
- 提交前再次核對暫存差異、文件連結與 metadata；commit 由既有 secret hook 及 quick check 保護。commit 身分以本機 Git 歷史為準，不在證據內建立循環自引用。
- 下一項正式資格仍優先 G-02：選定固定版本的真實客戶端，對接 mock 上游，驗證支援的文字協定、認證、錯誤、重連及不重送；不支援能力須明確拒絕。這項互通資格尚未執行，provider cap 與模型品質仍分別由 RG-04／RG-02 驗證。

此次未執行真實 API、部署或推送；正式十項 gates 仍 pending。
