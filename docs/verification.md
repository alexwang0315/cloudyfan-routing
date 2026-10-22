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

# RG-04／RG-05 程式整合與離線驗證（2026-10-18）

本輪基線為本機 `651571216fc80a64e3f13e84632a356dfcb76773`，變更尚未提交。實作、審查修正與待驗證邊界見 [rg04-rg05.md](rg04-rg05.md)；[去敏驗證摘要](evidence/rg45-offline.json) 固定完整 executable／test／build 輸入。原有 699 項與所有歷史證據保留。

- `./scripts/check --integration` 最終 **798 passed**（新增 99 項）、Ruff、鎖定離線依賴、紀錄檢查、Core／ScoreCore high mock demo、sdist／wheel 全部通過。8 個步驟退出碼皆為 0；兩個 demo 各 4 次 mock 請求、2 次分類，真實 API 呼叫與費用零。
- 完整收據：`runtime/checks/c29853c7ce3244e29c8ba01a36bf468f/checks.json`。完整輸出、red／green、獨立審查與併發探針保存在工作區 `.scratch/rg04-rg05/`，沒有上傳。
- 新增驗證涵蓋供應商合約／狀態／credential／endpoint／payload 綁定、GET 與 intent 延遲到期、無派送零費用、完整 Jev 兩層修正；身份 scope／expiry／撤銷／輪替／停用／時鐘回退；TTL／精確批准／未知費用／unsafe 與開檔停止／保留鎖／Queue 副本／零派送清除／退休 ID 不重用。
- 實際以本機 lsof 檢查合成檔案，確認開檔阻擋及持有保留鎖時的清除行為。實體清除僅限每次測試新建的合成 fixture，明確批准精確清單；沒有清除既有使用者資料。
- 獨立審查修正資格到 POST 前過期的 P1，與 Queue／retention 鎖順序反轉；各自重現及複核通過。中途完整檢查有 5 個舊 fixture 因固定引用上一輪基線證據失敗，已改為依實際基線載入。一次直接 console pytest 聚焦命令因 scripts namespace 不在匯入路徑而 collection 失敗；採專案統一入口的 `python -m pytest` 最終全套通過。這些失敗收據保留，不算成成功測試。
- 最終 metadata 更新後另核對 project-state、文件連結、歷史證據 hash 與 diff；不將本輪未提交來源冒稱為基線 commit 的內容。

實際付費 OpenRouter 尚無經核定的完整費用 ceiling 合約／control-plane probe，預設仍拒絕；合成簽署內容只驗證 adapter 邏輯。RG-05 的正式 TLS、身份與密鑰營運、保留政策、備份／log 範圍與維護 fence 尚待部署資格。十項正式 gate 保持 pending。本輪沒有真實 API、部署、推送、硬體、模型下載、GitHub 寫入或新增 Git commit。

# RG-04／RG-05 本機提交前複核（2026-10-18）

依使用者授權檢查既有 RG-04／RG-05 變更並保存至本機 Git；原始離線證據與私有審查、失敗／修正收據保持原檔。本次檢查未發現需追加修正的阻擋問題，沒有變更已測程式。

- 重新執行 `./scripts/check --integration`：**798 passed**；Ruff、鎖定離線依賴、專案紀錄、Core／ScoreCore high mock demo、sdist／wheel 均通過，8 個步驟退出碼皆為 0。
- 程式、測試與建置來源摘要仍為 `ca24f2b9f1753addca7202b33b070559594321e02b8b74fbd3745d1a7a8b858c`，與前輪完整驗證及審查修正後一致。完整收據：`runtime/checks/33c15264e1ea4f01a57ed08c0e42df25/checks.json`；可提交的去敏摘要：[rg45-precommit.json](evidence/rg45-precommit.json)。
- 提交前核對來源清單、暫存差異、文件連結、歷史證據與 project-state；既有秘密掃描及 quick-check hook 保持啟用。實際 commit 身分以本機 Git 歷史及工作區 `.scratch/rg04-rg05/` 的提交後核對紀錄為準，證據不建立循環自引用。
- 下一項正式放行優先 G-02：固定版本的真實客戶端對接 mock 上游，確認文字協定、認證、alias／run ID、錯誤、重連與不重送，以及不支援能力的明確拒絕。這項互通資格尚未執行。

RG-04 真實供應商完整費用上限與 RG-05 真實部署身份／TLS／資料保留仍待資格驗證，十項正式 gates 仍 pending。本次沒有真實 API 呼叫、部署、推送、硬體操作、模型下載、GitHub 寫入或清除既有使用者資料。

# G-02：官方 SDK 固定版本互通（2026-10-18）

本輪基線為本機 `6c8fdcc85d55ad8fc6b0c1a03d47a10903defa68`，變更尚未提交。沿用已確認的官方 SDK→公開 HTTP→Core／BudgetedCore→MockTransport 邊界，使用 [client manifest](evidence/g02-sdk-manifest.json) 的官方 Python 同步 `ollama==0.6.3`，沒有替換 SDK 的生成、聊天、序列化、錯誤或 response model 方法。完整證據：[g02-offline.json](evidence/g02-offline.json)；接入與限制：[ollama-sdk.md](ollama-sdk.md)。

- `./scripts/check --integration`：**844 passed**（既有 798＋新增 46）、Ruff、鎖定離線依賴、依賴一致性、專案紀錄、Core／ScoreCore high mock demo、sdist／wheel 全部通過；8 個步驟退出碼皆為 0。完整收據：`runtime/checks/f4391251c6e74ab29ce5972cac355b51/checks.json`。兩個 demo 各 4 次 mock 請求、2 次分類，真實模型 API 呼叫與費用零。
- 聚焦 SDK／協定測試 **83 passed**：40 項官方 SDK、43 項原生協定，其中本輪新增 40＋6 項。涵蓋角色 scope、過期／撤銷／停用、run ID、別名、typed response、驗收 false／unknown、預算拒絕、驗收工程中斷與明確續跑、回應遺失、連線失敗、費用未知停止，以及串流／多輪／工具／圖片／參數拒絕。
- 正常 SDK chat 先重現 422：即使未指定工具，SDK 仍送 `tools: []`。以最小修正只接納 chat 的精確空 list，協定版本升為 `cloudyfan-ollama-text-v2`；非空工具與其他型別仍拒絕，generate 規則不放寬。原 v1 歷史證據保留。
- SDK typed response 會忽略 CloudyFan 擴充 metadata，這點先由失敗檢查揭露。實際工作流程以同 run 的標準 HTTP GET 讀取完整帳務／狀態／provenance，沒有修改 SDK 或假造原生費用欄位。中途另修正新測試的 provenance 欄位路徑誤寫；SDK metadata 產生曾誤用全域 Python，改採鎖定專案環境後成功，沒有做全域套件安裝或 fallback。所有中間失敗紀錄保留。
- 正式 BearerAuthenticator 對 show／embed／pull 等未映射 endpoint 回 401；不將寬鬆 fixture 的 404 當成正式身份下的觀察。這些呼叫均未觸發模型、下載或其他動作。
- SDK 套件只加入 dev extra，普通 runtime 依賴仍是 httpx。準備階段下載一般開發依賴；驗證入口全程 locked／offline，測試保留禁止 socket guard。SDK wheel SHA256 與 uv.lock／官方套件記錄一致。
- 完整驗證後，metadata／工作單完成狀態、source digest、文件連結、歷史證據雜湊及 diff 另外核對；不把基線 commit 冒稱為本輪已測來源。

指定同步 SDK／文字子集／模擬上游的互通證據已補齊，但正式 registry 的十項 gates 仍 pending，需責任人依精確範圍審閱核定。AsyncClient、JS／產品 client、真實 TCP／TLS／代理重試、Ollama runtime／權重與部署未驗證；模型品質、供應商費用上限與實際身份／保留部署亦未由本輪取得資格。沒有付費模型 API、硬體操作、模型下載、部署、推送、GitHub 寫入、新 Git commit 或既有使用者資料清除。私有收據與 red／green 紀錄位於工作區 `.scratch/g02-sdk/`。

# G-02 本機提交前複核（2026-10-18）

依使用者授權檢查 G-02 變更並保存至本機 Git。原始 SDK、協定及完整驗證證據保持原檔；本次未發現需追加修正的阻擋問題，沒有修改已測程式。

- 重新執行 `./scripts/check --integration`：**844 passed**；Ruff、鎖定離線依賴、依賴一致性、專案紀錄、Core／ScoreCore high mock demo、sdist／wheel 均通過，8 個步驟退出碼皆為 0。
- 來源摘要仍為 `32b6113658b3413c70f7cce678efc89c765333bc270eeee270409c22c0a02005`，與上輪完整驗證一致；19 個原變更檔案與私有最終清單雜湊吻合。官方 SDK 的 4 份原始碼與 manifest 相符，既有 15 個依賴版本未變，SDK 僅加入 dev extra。
- 新收據：`runtime/checks/21af1d62e2474b2580c59b4524841723/checks.json`；去敏摘要：[g02-precommit.json](evidence/g02-precommit.json)。SDK typed response 忽略擴充欄位、須同 run GET 取回完整帳務的限制仍保留。
- 提交前核對精確路徑、暫存差異、文件連結、歷史證據與 project-state；既有 hook 鏈保持啟用，quick hook 不冒稱再次執行完整測試。實際 commit 以本機 Git 歷史及工作區 `.scratch/g02-sdk/local-commit-verification.json` 為準，不建立循環自引用。
- G-02 的指定同步 SDK／text-v2／HTTP 擴充範圍證據可供正式審閱；下一個技術驗證優先 **RG-04：真實供應商完整費用上限及核帳**。須確認精確模型／credential／請求設定下的有效限制覆蓋分類、生成、修正與失敗，且失效／到期會停止派送。一般 OpenRouter key 額度或 token 估價不足以建立此資格；缺乏合格供應商合約／probe 時，正式付費派送仍拒絕。

本輪沒有真實模型 API、部署、推送、硬體、模型下載、GitHub 寫入或既有使用者資料清除。G-02 的真實 TCP／TLS／代理與其他 client，及其他正式資格仍未因此取得核定；十項正式 gates 保持 pending。

# G-02 提交掃描修正與最終複核（2026-10-18）

第一次 commit 被既有 ECC 秘密掃描攔下，命中新測試的無效 credential 直接字串指派，未建立 commit。已改由每次測試新產生的臨時 credential 截短一字元，形成無效格式；相同認證拒絕案例保留，不儲存秘密常值、不略過掃描、不修改掃描器或應用程式。首次攔截輸出保存在 `.scratch/g02-sdk/local-commit.log`。

- fixture 調整後重新執行 `./scripts/check --integration`：**844 passed**；Ruff、鎖定離線依賴、依賴一致性、專案紀錄、兩個 high mock demo、sdist／wheel 八個步驟全部成功。
- 最終 executable／test／build 來源摘要：`564a13e3c59e7d2f9f3945ec003f1d574d24c00b199a0cb3fa00e7dcade98239`。前一筆摘要及收據保留為調整前的歷史結果；實際 App、SDK pin、wire 協定與測試預期沒有變更。
- 最終完整收據：`runtime/checks/230f2c59fd2643269eff3f0414b82089/checks.json`；去敏摘要：[g02-precommit-final.json](evidence/g02-precommit-final.json)。最終提交內容與結果由本機 Git 歷史及私有 `local-commit-verification.json` 核對。

本次仍無付費模型 API、部署或推送；正式資格狀態不變，下一個技術驗證仍優先 RG-04 真實供應商費用上限與核帳。

## 2026-10-18 — RG-04 qualified component bounds (offline)

- Baseline HEAD: `b11a2efb4c50d7e8a6ad5b6165a5ce131391808a`; its Git blob source digest was matched to prior G-02 evidence. No new baseline suite was invented. Prior state/work plan retained in `docs/evidence/rg04-components-prior-*.json`.
- Added `ComponentBoundVerifier`: eight explicit fee dimensions, independent hash-pinned review, exact model/provider/text scope, separate Decisions cap, no bundled real qualification or supplier-specific invented HMAC service.
- Full flow reserves initial generation, at most two classifiers and sole repair; HTTP failures retain paid costs, unknown outcomes stop, generation GET replaces provisional fees once. Selection policy unchanged. Classifier POST now uses pinned version rather than alias.
- Fixed exact budget arithmetic through reservation, event commitment, provisional holds and available balance. Added persistent shared review withdrawal with reviewed path/incarnation binding; empty directory recreation cannot revive approval.
- Red/green regressions reproduced unsupported decision media, low-precision workflow rounding, in-memory withdrawal revival, implicit model variants, classifier alias/version mismatch, registry-read expiry and completed concurrent withdrawal during registry read.
- First integration passed **893 tests**; retained [initial evidence](evidence/rg04-components-initial-pass.json). Final review found an additional withdrawal-during-read race; regression failed before the fix and passed after the final withdrawal recheck.
- Final `./scripts/check --integration`: **894 passed in 29.84s** (**50 new cases**); locked offline sync, dependency consistency, project records, Ruff, both high-route MockTransport demos and sdist/wheel build passed.
- Source digest: `d276cff46322d56170e2ffdd472ad0cd8f60e919e0673bd784f683b1cfad1492`. Private receipt: `runtime/checks/09c63c1c5bc744259cf37a258497b201/checks.json`; SHA256 `a3a6c1015d3b82989e00a26001932854c5a1ebfb26f45116463fc876818550b3`. [Sanitized evidence](evidence/rg04-components-offline.json).
- Independent read-only review has no remaining blocking findings; it is not human production approval. Current metadata/links are checked separately after finalization.
- **No real API calls, API fee, deployment, source push or new commit. RG-04 and the other production gates remain pending.** Real billed-quantity, effective-rate, inventory, failure and in-flight guarantees still require actual supplier evidence and independent qualification. See [contract and limitations](component-bounds.md).

### RG-04 local commit review

- Standards and Spec read-only reviews found **0 findings / 0 blockers**. No implementation changes were needed; no source/test/build inputs changed after the recorded 894-test integration pass.
- Rechecked current source digest, all eight successful integration steps and actual receipt SHA256; preserved original [offline evidence](evidence/rg04-components-offline.json) and [commit preflight](evidence/rg04-components-precommit.json). The full suite was not redundantly rerun; normal commit hooks still run offline checks.
- Supplier qualification priority: pinned Jev Decisions complete billed quantity ceilings, including rejected input/serialization, output/reasoning, internal retry/failure/cancellation and in-flight guarantees. Effective binding rate inventory and subsequent authorized billing observations follow; Chat token parameters do not prove the classifier cap.
- The repository-local commit excludes runtime receipts, credentials, original experiments and workspace-root documents outside this Git repository; those workspace documents remain in place and their hashes are retained in the preflight. Actual saved commit and post-commit status are recorded by local Git and ignored `runtime/rg04-components/local-commit-verification.json`. No API, deployment or push is authorized by this review.

## 2026-10-19 — RG-04 complete zero-rate component evidence (offline)

- Baseline: local Git `c271ddbbc94fbdefb5fca2a6de0f9da53b9b5dea`, whose executable source digest matched the existing 894-test component evidence. Preserved prior state/work plan as `docs/evidence/rg04-zero-rate-prior-*.json`; no new baseline test pass was invented.
- Added explicit schema v2: each of the eight components uses a bounded quantity/rate or an independently reviewed all-path zero-rate guarantee without `max_units`. A zero-price quantity component still requires its cap; v1 artifacts, reviews and v1-only descriptor retain their strict legacy contract.
- Zero-rate proof binds exact configuration/endpoint/account/period and all success/rejection/failure/cancellation/upstream attempts, excludes extra/reclassified fees, and covers price changes/withdrawal/expiry for already-dispatched requests. Sources, whole-artifact review, durable registry, expiry/revocation and POST lease checks remain mandatory. All fixtures are synthetic.
- Monetary output-cap qualification is separated from Chat's operational positive `max_tokens` and exact text/provider/effort/payload restrictions. Free output does not imply free reasoning/cache/input or whole-call cost; no automatic price-snapshot conversion is added.
- Tracer regressions failed first on the v1-only artifact check and on Chat dispatch requiring `max_units`; both passed after the extension. Fixture corrections selected a one-answer Chat scenario and asserted the existing durable-violation accounting behavior. Budget/Client/engine implementations were not changed.
- Focused component suite: **100 passed**, including **50 new cases**. Covers each zero-rate fee dimension, mixed legacy/v2 and zero-only `Core.run`, missing/invalid/nonzero-price guarantees, remaining paid input/reasoning caps, v1 nonconversion, proof changes, expiry/withdrawal, exact low-precision reservation, unknown interruption, positive-charge breach stop without resend, and idempotent generation reconciliation.
- Full `./scripts/check --integration`: **944 passed in 28.47s**. Locked offline dependency sync/consistency, project records, Ruff, both high-route MockTransport demos and sdist/wheel build passed; all eight receipt steps returned zero. Private receipt: `runtime/checks/aca2fb840e7c45089bb325d80fcc4c58/checks.json`, SHA256 `980aae4d07073719b28f021a5c6ff54560021d96c503852febf1e64555467012`. Full console: `runtime/rg04-zero-rate/integration-console.log`.
- Executable/test/build source digest: `89ed1980ff527fb940d93fb018365a9cc4b44aee21eef39ecff1c8f5e9adeafd`. [Sanitized evidence](evidence/rg04-zero-rate-offline.json); [v2 contract](component-bounds.md#schema-v2-explicit-all-path-zero-rate-evidence). Metadata/links and retained hashes are checked separately after finalization; no executable changes after the integration run.
- Review was integrator source/contract/diff inspection with regressions, not independent supplier or human production approval. Workspace-root glossary/ADR/README were updated outside the standalone core Git checkout; their hashes are preserved in the evidence.
- **No real API requests, API fee, deployment, push, new commit, GitHub write or deletion.** No real supplier guarantee was attached. All ten production gates remain pending; RG-04 still needs pinned Jev cumulative billed input, full fee/zero-rate coverage, internal retry/cancel/failure settlement and in-flight rate guarantees before qualification. No real cost savings or supplier enforcement claim follows from synthetic tests.

### RG-04 v2 local commit review

- Reviewed the complete changed verifier, regression cases, schema-v1/v2 compatibility, all-path zero-rate source/review binding, exact arithmetic and dispatch restrictions. Integrator correctness/contract/STRIDE inspection found **0 findings / 0 blockers**; this is not independent human production or supplier qualification.
- No executable/test/build input changed after the recorded **944-test** integration pass. Recomputed the current source digest and verified the original console, all eight successful integration steps and receipt SHA256. The full suite is reused with explicit provenance rather than described as newly rerun. Original [offline evidence](evidence/rg04-zero-rate-offline.json) is unchanged; the new [commit preflight](evidence/rg04-zero-rate-precommit.json) records this review.
- The staged commit is confined to the RG-04 v2 verifier, regressions, contract/handoff/README and sanitized evidence/status files. Private runtime receipts, review artifacts, model data and credentials are excluded. Workspace-root glossary, ADR and README remain outside this standalone Git repository; their preserved hashes were checked.
- Normal inherited secret scanning and the repository quick-check hook remain required; no bypass is used. Actual commit identity, hook log/check receipt and post-commit clean-status verification are retained locally under `runtime/rg04-zero-rate/`, avoiding circular commit hashes inside the commit itself.
- Next supplier evidence priority: an exact-version/provider/endpoint-bound **cumulative billed input ceiling per external Jev Decisions call**, including serialization/hidden wrapping, rejected input, cancellation, failures and every internal upstream attempt, with an effective in-flight rate ceiling. Output/reasoning can use independently qualified all-path zero rates; a public zero price alone still does not provide the complete guarantee.

This review authorizes only the user-requested local commit. No API spend, deployment, push, GitHub mutation or supplier contact; RG-04 and all other production gates remain pending.

## 2026-10-19 — Release index status and first-release evidence clarification

- Documentation-only clarification at source commit `f1dd57c8a7963422c5ffd7e1c27766c7115ebd9b`: the release index now gives each gate separate **implementation completed**, **offline verification completed**, and **formal qualification pending** columns. Completed scope is explicit; no pending production gate is described as an unfinished software task.
- First-release evidence priorities: named scope/owners; domain validation and exact Jev cumulative-input/full-rate guarantees; one selected score source and actual model pool; all activated entry/local/queue paths; then evidence for the specific quality/cost/latency claims. Conditional evidence is mandatory when its feature is enabled, not automatically waived. No release scope or not-applicable gate was approved by this documentation change.
- Extra sources, background refresh/notifications, additional clients/protocols, multi-host/automatic recovery and the plan-only control arm are separated as optional extensions. The historical spec-1 index is labeled as an initial-delivery snapshot so its former verifier/identity/SDK gaps cannot override completed later delivery.
- Executable inputs, prior verification/evidence and `release-gates.json` are unchanged. The existing 944-test integration is checked for provenance, not rerun or relabeled as a new pass. Documentation/link/status and offline quick-check results are recorded in [documentation evidence](evidence/release-index-status-20261019.json).
- No paid or account API, model requests/downloads, hardware, deployment, push, GitHub write, supplier contact, deletion or new Git commit. All ten formal gates remain pending; named review and real evidence still apply.


## 2026-10-19：首版範圍／RG-01／RG-04 證據準備

以本機提交 `661f06f6dd5ad5407b83f4a541f6c69f28d2393f` 為整理基線，新增首版範圍草案、RG-01 驗收器資格準備包及 RG-04 缺證矩陣；另保留 20 份既有來源的 path／SHA256、五個歷史候選、八個費用元件與七類路徑的待核定清單。沒有改動 executable/test/build inputs、正式 release registry、agent-state 或 work-plan；舊 944 項完整整合驗證只核對原始收據與來源雜湊，不聲稱本輪重跑。

本輪實際離線檢查：

- `./scripts/check --quick`：鎖定離線依賴、依賴一致性、project records、Ruff 全部通過。收據 `runtime/checks/91b23254cb68436b92b42529eb39dbf7/checks.json`。
- 核心 `tests/test_engine.py`、`tests/test_diagnostics.py`、`tests/test_component_bounds.py`：**141 passed**，使用核心 `.venv`、禁止網路的既有 conftest；只證 mock 契約。
- 舊工作區 `.venv` 跑兩份 SQL 測試先因 `pydantic_core.ArgsKwargs` import error 停止（exit 4，尚未執行測試），原失敗 log 保留。沒有修依賴或網路下載。
- 改用核心已鎖定 `.venv`、停用無關舊 conftest／第三方插件自動載入、程序內 socket/DNS guard，重播未改動的 `tests/test_workflow_sql.py` 與 `tests/test_sql_execution_contract.py`：**93 passed**。這兩檔只用 pytest 內建 fixtures；不代表舊環境已修復、全舊專案或正式 SQL 接入已合格。
- 補證稽核另外檢查嚴格 parser／公開正反例／已知固定答案 false acceptance、DiagnosticContract 拒絕原始 SQL return 與巢狀 feedback、test ID 可追溯、來源 hash、版本／上限、文件連結、待核定矩陣與 registry 不變。實際結果與檢查程式 hash 見下列證據。

[本輪來源／缺項清單](evidence/release-preparation-inputs-20261019.json)與[驗證摘要](evidence/release-evidence-preparation-20261019.json)只含去敏材料。可重現腳本、JUnit、console 與稽核結果留在 ignored `runtime/release-evidence-preparation-20261019/`。原始 prompt、答案、收據、key 與私人帳本沒有複製或改寫；未查當前帳戶餘額／歷史未知總數。

本次發現的正式缺口為：具名 scope／語義審閱、獨立核心 SQL Environment／診斷投影與工程錯誤分類、硬資源隔離的適用證據、模型池／來源資格，以及全部可達端點的完整費用保障／終局核帳。這些維持待核定，不寫入正式 evidence 或捏造批准。未聯絡供應商、未呼叫 API、未啟動服務／模型／硬體、未部署、未推送，也未執行 Git commit。


## 2026-10-19：補證文件提交前審閱

本輪僅修正 RG-01 文件的歧義措辭：關閉的是 extension，query_only／authorizer／setlimit／progress handler 是啟用的控制，與既有程式一致。來源、驗收語義、選模門檻與正式 registry 不變。原準備證據的文件 hash 保留為修訂前快照；[提交前審閱紀錄](evidence/release-preparation-precommit-20261019.json)記錄修訂後的文件 hash 與核對結果，不覆寫原本 141 項／93 項測試、失敗環境及 944 項歷史整合收據，不聲稱本輪重新跑過那些測試。

此審閱只授權使用者指定的本機 Git 提交；缺證項目保持 pending，未聯絡供應商、花費 API、部署或推送。提交時正常執行既有 secret scanner 與離線 quick hook，實際收據及提交後核對留在 ignored `runtime/release-preparation-commit-20261019/`。


## 2026-10-19：RG-01 SQL Environment 接入與公開診斷

基線 `7be8616ac5ca155e2ab6a633ca3401760afe1e47`。新增獨立 `sql.py`／無資料 `sql_executor.py`、SQL Environment、凍結公開契約、flat code 投影與工程分類；Core／Client／BudgetedCore／診斷 schema、路由規則及 `release-gates.json` 未修改。舊 source、題庫、答案與帳本未改，也未複製進核心。

- 新增 **47 項 SQL 回歸通過**：`runtime/rg01-sql-environment-20261019/focused-final-tests.xml`，涵蓋首答／兩層分類／唯一修正、unsafe／parser／結果／資源錯誤、preflight、工程中斷、無重送、預算保留及 finite-fixture false acceptance 限制。
- 最終 `./scripts/check --integration`：**991 passed in 50.41s**，Ruff、鎖定離線 sync／依賴與 project records、兩個 mock high-route demo、sdist／wheel 全通過。收據 `runtime/checks/2362d205ecaa40eaae70f12d6d3a3558/checks.json`。
- TDD 初始 missing-module、工程異常未分類、projection／preflight 失敗紀錄全部留在 runtime；沒有隱藏紅燈。原始完整驗證另遇 Ollama SDK 的 pydantic_core import error：查到 `.so` 僅剩 iCloud placeholder，以本機 uv 快取同版本／平台、符合已安裝 RECORD 的 SHA256／大小，僅新增補回缺失檔案。未刪 placeholder／其他檔案、未改 metadata／lock／版本、未下載。復原證據 `runtime/rg01-sql-environment-20261019/dependency-restoration.json`。
- 依賴復原後一次完整執行為 987 pass／1 fail，原因是舊 project-state 測試硬寫當前待辦第一項必為 G-02；只使該測試的 fixture 自帶固定待辦，保留離線完成不等於正式核定及其他錯誤驗證，不改 checker。其後 988 全通過，再新增兩層 SQL 路徑及已知常數答案限制三個案例，得到上述最終 991。
- `SqlValidationUnavailable` 明確歸 engineering；setup／build／runtime／I/O／allocation／未知 SQLite 及 wall timeout 不冒充模型答錯，也不啟動付費補救。確定的 SQL／VM／輸出不合格才返回公開 False；缺公開例子為 None。共享預算在工程中斷保持預留，明確續跑重驗原答案，不新增 POST。

[本輪離線證據](evidence/rg01-sql-offline.json)保留來源指紋、原始成功／失敗收據、抽取選單、環境復原及建置物雜湊。[接入文件](sql-environment.md)、範圍／資格準備包及放行索引已同步實作現況。正式仍缺具名領域／expected／覆蓋審閱、適用的硬隔離、模型及供應商保障；十項正式 gate 均 pending。未聯絡供應商、呼叫真實／帳戶 API、操作硬體、部署、推送或新增 Git commit。


交付紀錄更新後另跑 `./scripts/check --quick`，收據 `runtime/checks/78c0d447a65d46dab555cdb5dcbe1e99/checks.json`，確認 `implementation_complete=True`、`production_qualification_recorded=False`。文件 116 筆本地連結、證據／來源雜湊、work-plan 及 diff 檢查通過；程式來源指紋為 `b318bb59eee43aef1cba01f0825da72f39ff2fa5a65475881f0c3d9158c4dc5e`。後續文件封裝檢查僅重建包，不重跑或另稱新一批模型實驗。


## 2026-10-19：RG-01 集中補齊工程與離線審閱材料

基線 `bdc3b084bb2db680afaf42d17cf33b953cbec850`。新增 SQL v2 disposable worker、bounded pipes、父程序 deadline／kill／reap、CPU／SQLite hard_heap_limit 及嚴格可選 RLIMIT_AS；worker 不繼承憑證環境與開啟 FD，不匯入 provider SDK。路由、生成上限、預算介面與正式 registry 不變。

`sql-review-catalog-v1` 提供 S01～S08、每類五組公開合成資料、獨立 Python oracle 與手算基準、14 個已知錯誤 SQL；另測複製首組常數答案的誤收。這不是新模型實驗或一般 SQL 正確性證明。

失敗紀錄保留：本機 RLIMIT_AS／DATA 設定遭拒；嚴格配置會派送前停止。語義初測 18 pass／1 fail，原因為測試錯誤期待已完成結果重讀有 resumed=True，改為核對原結果及請求數不變。新 baseline 曾錯連 v1 uncommitted evidence，project-state 正確拒絕，已用 Git commit blob 指紋核對後建立獨立 baseline 紀錄。原始記錄留於 ignored `runtime/rg01-review-ready-20261019/`。

SQLite 32 MiB 限制不是 Python 全部 RAM，process containment 不是完整 OS security sandbox。具名 scope／語義／expected／覆蓋／目標環境核定仍 pending；真實模型、費用及品質主張分別沿其他 gate。沒有 API、供應商聯絡、硬體操作、模型下載、部署或推送。實際最終驗證、來源雜湊與收據見[本輪離線證據](evidence/rg01-review-ready-offline.json)。

首次整套驗證為 1025 pass／1 fail，收據 `runtime/checks/acc4d18d8ee24d4685dee75b3aa4e3ae/checks.json`。失敗是 project-state synthetic fixture 依賴當前 ownership 有未提供的 src 介面才建立來源目錄；改為測試自建 src，且 work-plan 保留既有 core-run 介面依賴。checker 未修改。審閱另補公開 fixture 型別拒絕：禁止 SQLite affinity 將字串轉整數／整數轉文字，以及 INTEGER PRIMARY KEY 的 NULL 自動編號；新增四項回歸。覆蓋 hash 現在包含全部公開資料／expected、手算、操作、reference 與 mutants。

最後審閱另固定平台 kernel／architecture，並要求 worker 回報的 SQLite version／native limits／執行契約與父程序完全一致；不一致在 preflight 拒絕。這避免移除環境變數後載入不同 SQLite runtime 而沿用父程序假定。對應回歸收據保留於本輪 runtime。

最終 `./scripts/check --integration` 全通過：**1031 passed in 137.47s**，加上 Ruff、locked/offline sync、依賴／紀錄、兩個 mock high-route demo 與 sdist／wheel；收據 `runtime/checks/e6e92cef7ee04345a7f40e8ebf9291e2/checks.json`。對照 v1 991 項，本輪新增 40 項，沒有新增模型請求。來源精確對應本輪離線摘要。

新 wheel 的 sql／worker／process／review／executor bytes 已與來源逐一核對，再從解開的 wheel package 實際執行 SQL 子程序驗收通過；原環境與 wheel 執行區分保留。公開覆蓋報告為 8 類／40 組、14 個已知 mutants 被拒絕，八類常數答案拒絕另在完整測試中驗證。狀態紀錄標示本輪工程／離線材料完成，正式十 gate 仍 pending；後續快檢與文件重建包只確認完成紀錄，不另稱新完整測試。


## 2026-10-19：S01～S08 題意／expected／反例覆蓋離線審閱

基線 `666b79be14ccd227045a3108fb89c1eeb8894e54`；source fingerprint 保持 `a7495567821e97142ffd2d2e52e224d882894830a08ebd29a21d621704a27f72`。本輪僅新增文件／審閱證據，不改 source／tests／路由／能力卡／門檻／原五組 fixtures、expected、mutants 及正式 registry；沒有重跑模型或整套 pytest。舊 1031 pass 僅為相同來源的歷史證據。

逐筆另列 40 組 expected，實際 SQLite reference 與 Python oracle 均一致；原 14 個 mutants 再驗均拒絕。13 個選定、事後、非盲錯誤 SQL 均可過凍結五組，卻在對應的新公開診斷資料答錯。這是覆蓋缺口，不是原 40 組答案錯誤，也不能估母體誤收率。另有相同多重集合不同插入順序的 int64 中間累加溢位，最終和可表示卻可能工程中斷，需釐清數值域。S07 完整唯一排序鍵下 RANGE／ROWS 的等價正向控制通過，避免把字面不同當成錯誤。

執行腳本與 console 保留在 ignored `runtime/rg01-semantic-review-20261019/`；[逐題審閱](rg01-semantic-review-20261019.md)、[原始診斷資料](evidence/rg01-semantic-review-probes-20261019.json)及[摘要](evidence/rg01-semantic-review-20261019.json)分開存放。這些是全新合成、公開測試資料，不含私人實驗 prompt／答案／原始帳本或憑證。正式 owner／reviewer 未代填，gate 全 pending；未花費 API、聯絡供應商、部署或推送。

文件／紀錄完成後 `./scripts/check --quick` 通過：`runtime/checks/a99e8d1b6ec04d85b32a98e8eabc47e1/checks.json`，含 locked/offline sync、依賴、project records 與 Ruff。108 筆本地文件連結及來源／證據 hash 核對通過；正式核定 false、十 gate 全 pending。新診斷的錯誤被接納本身是審閱發現，不被這份 quick PASS 清除。


## 2026-10-19：依使用者委託決定 RG-01 四項選擇

使用者明確委託代決。本輪採用首個 S01～S08 SQL 資格切面、既定題意／NULL／排序／文字語義、新 amount 絕對值 10^9／最多 1000 訂單的充分安全數值域、AI 技術責任與專案維護者發行責任，以及 Linux 隔離取證目標。完整規則見[決策紀錄](rg01-first-scope-decisions-20261019.md)及[policy](evidence/rg01-first-scope-policy-v1.json)；工作區 root ADR 另記取捨。這是被委託的決策，不是原始碼已實作、使用者簽名或正式放行。

source／tests／原 catalog／原 expected／診斷／正式 registry 保持原樣，既有未提交語義審閱也保留。不為取得綠燈刪除 0／負 ID 或無主引用；13 個已知反例仍須留在新域內。新數值限制只適用未來版本，不將舊最大 int64 診斷改算成功。實際檢查、舊 1031 項測試的重用範圍、來源及新文件 hash 另存[本輪摘要](evidence/rg01-delegated-decisions-20261019.json)。本輪不做付費 API、供應商聯絡、部署、推送或 Git 提交。

本輪新數值域的四組真 SQLite 合計檢查（1000 筆全正、全負、抵銷、全 NULL）均通過；數學界限與既有 40 組／13 個反例仍在域內已核對。另實測凍結 adapter 仍接受 amount=1000000001，故明確記錄新政策未由 runtime 強制，不冒充實作完成。完成紀錄 quick 通過：`runtime/checks/0024e5ba23034770bae8046f5631c441/checks.json`；沒有重跑完整 1031 項來源測試。

## RG-01 remaining tickets — 2026-10-19

- User authorized all eight local review drafts through mattpocock/implement; no issue publication, paid API, provider contact, provisioning, deployment or push. Related prior uncommitted semantic/decision docs preserved.
- Implemented bounded catalog/policy, all 13 counterexamples and strict Linux cgroup/bwrap launcher. Python identity gap found independently by Standards/Spec, reproduced then fixed with worker v2 and four mismatch/missing zero-POST regressions. Both follow-up reviews: 0 remaining findings.
- Exactly one full integration run: **1098 passed in 127.00s**; Ruff, locked offline sync/pip check, record consistency, both mock demos, sdist/wheel passed. Receipt `runtime/checks/aaae39715e8d4daeb8326af6d4d946a7/checks.json`; private console `runtime/rg01-tickets-integration-20261019.log`. Source digest and receipt hashes are in [new sanitized evidence](evidence/rg01-tickets-offline-20261019.json).
- Installed-wheel isolated-Python smoke passed 8 cases and loaded 13 counterexamples; no source checkout import. Candidate catalog independently cross-checks original 40 hand values and rejects 14 old + 13 new mutants. These are post-hoc synthetic regressions, not model quality/cost results.
- RG01-01..05 and RG01-07 completed in their offline scope. RG01-06 real Linux attack evidence and RG01-08 actual named formal approval remain blocked, explicitly recorded. Formal registry unchanged; no production qualification. No new API spend.


## Scout dedicated preparation — 2026-10-19

User-authorized system packages/permissions; administrator authentication was
performed by the user in their own terminal. Installed pinned bwrap and an
isolated wheel/locked runtime. Actual first startup failed on cgroup migration,
then on an incorrectly non-executable ELF loader. Preserved both failures and
v1 artifacts; fresh systemd user delegation and a side-by-side read-only v2
rootfs resolved those preparation faults without global cgroup or service changes.

Real ordinary-user `LinuxIsolation` artifact/lease checks and SqlContract SQL
preflight/validation passed (`SUM` of public 1,2 => expected 3), 1.132 seconds;
worker leaf emptied, transient scope released afterward. Ten existing containers
remain running and both originally healthchecked containers healthy; no new
failed user unit observed. This inventory is not full application-health testing.

`./scripts/check --quick`: PASS (dependency compatibility, project-state, Ruff),
receipt `runtime/checks/c57e846f065d47bd991bf582e2e3b308/checks.json`.
An earlier quick check rejected an invalid metadata authorization flag; corrected
that record field and preserved its failed receipt. Final project-state and
`git diff --check`: PASS. Twelve operation receipts hash-checked, 70 local links,
JSON records and operational script syntax checked. Source digest remains
`e3818e9500757ad02b44e87c4113de73a060af9643dd5b7a7d74c83b0d52de3b`;
no new pytest run. Prior 1,098-test full-suite evidence is historical, unchanged.

See [environment record](rg01-scout-environment-20261019.md) and
[scoped evidence](evidence/rg01-scout-environment-preparation-20261019.json).
Preparation task is complete; RG01-06 attack/resource/interruption evidence and
RG01-08 named formal review remain blocked. Formal registry unchanged. No real
model API, stress suite, supplier contact, persistent service deployment, source
commit or push in this preparation delivery. Original assets were not deleted.


## RG01-06 controlled Scout evidence — 2026-10-19

Implemented opt-in adversarial qualification entry and fixed trusted probes,
with Core.run/MockTransport and real immutable SQL worker interruption/resume.
Preserved preparation documents and all prior uncommitted work. Independent AI
read-only review found cancellation false-success P2; ready-marker admission and
external KeyboardInterrupt propagation plus regression fixed it. Final evidence
re-review found no remaining blocker; this is not named human approval.

First target run stopped on existing helper mock provider SyntheticFixture versus
Example profile identity mismatch after one synthetic POST; seven rows stayed
not_run. Fixed fixture identity, added portable full-eight normal/reuse regression
and used separate r2 stage/scope/output. No production identity rule was relaxed.
Both raw runs remain under private runtime. Final Scout: normal 8/8 and target
7/7 passed; 96 copied remote files hash-verified, before/after ten-container
inventory identical, worker leaf empty, scope released, rootfs still read-only.
First six rows prove trusted probes in the same sandbox envelope. Last row
SIGSTOPs actual SQL worker after saved answer, observes production timeout, then
reuses identical response without a second POST. Active reservation 0.482→0;
synthetic provisional hold 0.02 remains. No real billing reconciliation claim.

Final ./scripts/check --integration PASS: 1,102 tests in 127.53s; dependency
compatibility, project-state, Ruff, both mock high-route demos and sdist/wheel
passed. Receipt runtime/checks/49d8383c93094d5896532114f81e13a1/checks.json.
Earlier full check failed collection because the same-version native pydantic_core
.so was absent and iCloud placeholder remained; restored only absent binary from
local cache, verified installed RECORD SHA256 and size. No version/lock change,
download or deletion; failed receipt and repair record retained. Earlier 1,100
and 1,101 runs do not replace the final 1,102 source-verification result.

[Offline summary](evidence/rg01-target-offline-20261019.json),
[target summary](evidence/rg01-linux-target-runtime.json) and
[scope/interpretation](rg01-scout-target-20261019.md) retain source/receipt hashes.
RG01-06 technical work complete; RG01-08 actual named formal review remains
blocked. Registry unchanged. No real API, supplier contact, persistent service
deployment, GitHub issue write, source commit or push in this turn.


## RG01-06 local precommit review — 2026-10-19

User explicitly requested checking and committing the RG01-06 changes locally;
formal qualification stays pending, no paid API/deployment/push. Reviewed the
qualification CLI, trusted probe module, four new regressions and linked
preparation/target records. No new executable correction was required. Source
fingerprint matches the prior full 1,102-test / integration receipt exactly.
Fresh ./scripts/check --quick passed (runtime/checks/76a04ca3531147378caabdf7571ce296/checks.json),
and tests/test_sql_qualification.py passed 7 tests in 8.70s. This is a focused
commit-time check, not another full-suite/Scout attack run. Original target
8/8 and 7/7 results remain the same preserved observations.

Added [RG01-08 review checklist](rg01-formal-review-checklist-20261019.md) without
changing adopted business semantics or filling any formal owner/reviewer field.
[Precommit record](evidence/rg01-target-precommit-20261019.json) preserves source,
receipt and registry hashes; staged review excludes runtime, private answers and
credentials. The authorized local commit is checked afterward with Git log/tree;
no external source or Issue write is part of this delivery.

## RG01-08 existing-evidence technical review — 2026-10-19

User authorized technical review only, no signatures or formal gate changes.
Read-only semantic and control-plane AI reviews plus root integrity synthesis
found no new blocker within the explicitly limited public-SQL/pinned-envelope
claim. Rechecked 26 indexed hash references and all 96 copied target files,
saved-answer hash, initial blocked/not-run evidence and unchanged source digest.
No SSH, target probes or model API this turn. Historical full 1,102-test evidence
was reused by matching digest; no new full-suite run is claimed.

Local in-memory SQLite 3.50.4 replay of trusted existing references/public data
confirmed 40 original manual/oracle pairs, 53 reference case/sample pairs and
rejection of 14 old mutants plus13 post-hoc wrong queries. It is deterministic
review of existing material, not new holdout/model/Linux qualification. The first
private replay command resolved the venv Python symlink to a non-venv executable
and failed import; reran the same unchanged script through the absolute venv
entry, passed. Both diagnostic outputs remain private under runtime.

Created [review report](rg01-technical-review-20261019.md) and
[hashed review summary](evidence/rg01-technical-review-20261019.json); updated
checklist/handoff and the separate completed technical-review subtask. Actual
RG01-08 formal review remains blocked. Registry, formal fields and source are
unchanged. Human confirmations are limited-risk acceptance, actual owner/reviewer
and scoped formal outcome, effective/expiry/withdrawal/revalidation responsibility.
No reopening of already-delegated S01-S08 semantics or numerical-domain choices.

Current review-only checks: ./scripts/check --quick PASS, receipt runtime/checks/ccf7ca210c94445f8870c429daaa4180/checks.json; 19 review-input/method receipt hashes and48 local links checked, docs JSON and git diff --check passed. Formal registry bytes match committed fd2c591 exactly; RG01-08 remains blocked. No executable/test changes or new full pytest/Scout execution.

## RG01-08 documentation precommit review — 2026-10-19

User authorized local review and commit only. Reviewed the technical report,
checklist, evidence summary, prior snapshots and state/plan changes. Clarified
that the earlier no-commit statement describes the technical-review stage.
Fresh ./scripts/check --quick passed (locked offline dependencies, record
consistency and Ruff); 20 review input/receipt/check hashes match, prior snapshots
match fd2c591 committed records, and executable fingerprint remains unchanged.
Document JSON/link and whitespace checks are recorded in the
[precommit summary](evidence/rg01-review-precommit-20261019.json). The earlier
1,102-test integration is historical same-source evidence, not a new test run.
Formal registry bytes remain identical, RG01-08 stays blocked/unassigned and
RG-01 stays pending. No signature, API, Scout execution, deployment or push.

## Direct RG01 routing execution tests — 2026-10-19

User stopped additional S01-S08 review and directed immediate tests. Focused SQL
adapter/bounded/process/isolation/qualification tests: 139 passed in 93.94s.
Initial full run: 1101 passed, one plan-ownership fixture failed because the new
execution task had unordered overlaps with historical tasks. Preserved failure;
restricted the active plan to execution scope, preserving prior state/plan.
Metadata regressions: 31 passed in 3.89s. Final ./scripts/check --integration:
1102 passed in 201.11s; dependency checks, Ruff, both mocked high-route demos
and sdist/wheel build passed. Four core routes plus four completed replays passed;
replays each made zero new requests. Counts are overlapping checks, not summed.
[Execution summary](evidence/rg01-routing-execution-tests-20261019.json) binds
actual receipts/output hashes. Source fingerprint and formal registry unchanged.
No more S01-S08 review prerequisite, no signature, paid API, deployment or push.

## Real-model RG01 integration and compiler fix — 2026-10-19

User authorized real-model end-to-end tests. Frozen existing S01-S08 and fixed
profiles/efforts; no re-review or threshold tuning. v1 completed seven workflows
and stopped S08 after saving its answer: compound ORDER BY output-term error
was not classified. Narrow exact-message mapping to existing syntax_error plus
two real-SQL/mock regressions; focused 3 passed, full integration 1104 passed
in 112.36s including Ruff/demos/build. Retain old source, plan and stopped record.
A new repair-only plan used the saved S08 answer, one true Jev selection and
one true low-model repair, without repurchasing its first answer. Three
additional low/mid/high endpoint probes passed. Fifteen paid calls total
(13 generation, 2 classifier), USD0.060291624, all fifteen GET generation
receipts now verified and key usage delta matches. Initial final-generation
GET404 preserved; explicit GET followup succeeded without a POST. Original
cohort is not relabeled 8/8; no population quality, CDR, second-stage routing
or formal qualification claim. See [live evidence](evidence/rg01-live-integration-20261019.json).

## SQL live-integration precommit review — 2026-10-19

User authorized local review/commit with no API tests, deployment or push.
Reviewed the exact compiler mapping and two regression cases; no new code
fix needed. Updated stale README history to link the real integration results.
Rechecked 15 original responses/reconciled costs against the recorded account
delta: USD0.060291624. Original seven completed results, S08 interruption,
unchanged initial answer, new validator continuation and GET404 followup remain
separate and preserved. A private all-file inventory binds the live runtime.
Fresh focused regressions: 3 passed/50 deselected in 3.87s. Fresh quick checks
passed. The existing full 1104-test integration receipt matches the unchanged
source fingerprint; it was not rerun during this commit review. See the
[precommit record](evidence/rg01-live-precommit-20261019.json). No API request,
spending, formal signoff or release-registry change this review.

## Cost-resolved generation429 recovery — 2026-10-19

Final `./scripts/check --integration` passed 1,138 tests, Ruff, mock Core/score
demos and sdist/wheel. Thirty-four new rejection regressions cover binary/direct
upgrade, two-attempt bound, fee/identity stops, authoritative GET, durable replay,
budget reservation and version isolation. The initial plan-fixture failure and
successful rerun are both retained in [offline evidence](evidence/provider-429-offline-20261019.json).
All 2,369 original fresh50 files match the saved SHA inventory; no old answer,
fee or grade changed. No new real API, deployment, push or formal qualification.

## Scoped fee-policy bridge and new frozen50 — 2026-10-20

An explicitly injected archived text-only429 policy reaches native Client/Core
and ledger, with request/response/GID/source/expiry binding. Actual response or
generation fees win; policy attribution is not a generation receipt or supplier
qualification. Fifty focused MockTransport checks passed; full integration
passed1,154 tests, Ruff, both mock demos and sdist/wheel, with zero real APIs in
tests. Source digest68d912f7275c0888811591a46c078bebf1afac8263f73654e8f0fb2407555301
matches the frozen trial. See [offline evidence](evidence/rejection-fees-offline-20261020.json).

Separately authorized real comparison completed50 fresh tasks x4 arms with
frozen source, profiles, public contracts, bounded attempts and no hidden-driven
repair. All200 completed before hidden grading started. New221POSTs allHTTP200,
USD5.805611568, zero unknown/unmatched charges, exact key delta.220 generation
receipts and one authoritative response usage.cost are distinct. Latest GET408
for the latter is preserved; the fee is known, not a fabricated receipt. Three
old429 copies had read-only policy verification before dispatch; new429 recovery
rate was not observed. Old2,369 files and release registry hash unchanged.

Hidden-final outcomes: CF48/50, high50/50, low-first50/50, OSS-first49/50.
Three public false accepts never triggered repair; all six CF repairs passed.
All failures and fees remain. Only low-first vs high qualifies as equal-quality
sample savings82.79%; CF96.80% is spending difference. See [full report](fresh50-recovery-20261020.md)
and [sanitized evidence](evidence/fresh50-recovery-20261020.json). No new paid
calibration, deployment, push, local commit or formal qualification. Earlier
smoke and earlier50-task costs are disclosed separately.

Final independent cost/grade aggregation,12 frozen-file hashes,source digest,
200-state/grade counts,grading chronology and sanitized links passed; quick
checks passed project records and Ruff. See [final consistency evidence](evidence/fresh50-recovery-final-check-20261020.json).

## General public SQL suite — 2026-10-20

New opt-in builder/test files only; native SqlContract/Core/fee behavior unchanged.
Independent application Python expectations are materialized into public samples
before dispatch, coverage requires data witnesses, and provenance is frozen.
No historical task ID, query-shape rule or hidden oracle is embedded in the
mechanism. Twenty-eight synthetic regressions cover general semantic contrasts,
typed mutation, absent coverage, nondeterminism, bounded repair and replay.
Focused state/suite59 passed; final full integration1,182 passed in184.33s,
Ruff/demos/sdist/wheel passed. Code-only review's input-type mutation issue was
fixed before final verification. Initial failures and reruns remain in the
[offline evidence](evidence/sql-public-suite-offline-20261020.json).

Seven independently generated shared public cases per task reject three known
bad saved answers and retain five controls;16 controlled single-axis checks
isolate duplicates/orphans/no eligible children. No private hidden fixture,
expected or reference SQL was loaded into the analysis. These are post-hoc
offline regression results, not new live quality/cost evidence. All2,552 current
raw trial files, three result documents and2,369 older files match SHA inventories.
The original paid grades/costs remain unchanged. No new API, supplier contact,
deployment, push, local commit or formal qualification.

## Public-suite staged-source verification — 2026-10-20

Local precommit review verified the exact source/test subset in an independent
index export:1,166 tests passed in150.63s, Ruff, mock demos and sdist/wheel passed.
The earlier1,182-test receipt belongs to the larger working tree with16 pending
fee-policy tests; it remains unchanged. Metadata-only finalization is checked
separately. [Precommit record](evidence/sql-public-suite-precommit-20261020.json)
binds both scopes, source hashes and original file preservation. No new API,
deployment, push or formal qualification is authorized by this review.

## Remaining fee-policy and report precommit verification — 2026-10-20

Source review reproduced five failures for two defects and their stop behavior:
policy expiry/source changes blocked later real receipt GET, and embedded
derived fee fields bypassed independent policy-proof validation. Policy v2 fixes
both with original/actual-only reconciliation and strict original-response fields.
The same five cases pass after fixes; all55 fee/provider regressions pass.
Full integration passes1,187 tests in132.36s, Ruff, both mock demos and build.
[Precommit evidence](evidence/fee-report-precommit-20261020.json) binds source,
receipts, failing reproductions, successful reruns and independent follow-up review.

Purely offline reaggregation matches200 rows/221 saved calls, first/final grades,
per-arm complete costs and median latency. API total USD5.805611568 equals the
saved account delta, with220 generation receipts and one response usage.cost.
Both raw trial inventories (2,552 and2,369 files) and three original report
artifacts are unchanged. Prior1,154/1,182/1,166 receipts and original v1 sources
remain preserved; no historical grade or cost was updated. No actual API GET,
paid request, deployment, push or formal qualification during this review.


## First-pool POOL-01–05 — 2026-10-20

`./scripts/check --integration`: 1,206 passed in 130.68s; Ruff, dependency/project-record checks, Core/ScoreCore mocked demos and offline sdist/wheel passed. Receipt runtime/checks/4eb0294fe47b47f4a5a63e46a2f28b82/checks.json. Nineteen new regressions; earlier 158-test focused run preceded the five evidence-binding regressions, not a contradictory full count. Archive prepare intentionally returns 1/blocked; fixed MockTransport + actual SQLite lifecycle passes four routes, new snapshot selection, expiry rejection and zero-POST original-snapshot resume. Fees in the lifecycle are simulated; USD0 real spend. Both review axes resolved their one finding, without fake human approval. Current source/evidence and limitations are in [the first-pool record](evidence/first-pool-offline-20261020.json). No RG01 change, paid trial rerun, supplier contact, source fetch, deployment or push.


## Release-readiness document commit review — 2026-10-20

Reviewed the release index against local implementation commit ec58e74 and existing first-pool/ticket/financial evidence. Clarified unverifiable-to-selector and mid-to-second-selector wording; no code or RG01 contract changes. All 42 local links and ten gate rows match; full 1,206-test evidence and profile hashes still match current source. Fresh `./scripts/check --quick`, release-gates and diff checks pass; no new full pytest run is claimed. [Document commit evidence](evidence/release-readiness-precommit-20261020.json) preserves the current document hash, quick receipt and formal pending state. Only the index and sanitized verification records are committed; no API, supplier contact, deployment, push, or formal qualification promotion.


## 2026-10-20 — RG-02/RG-03 public evidence supplement

Existing material  + 32 explicitly archived unauthenticated public GETs (26 HTTP 200,
3 old-score 410,3 LiveBench 404), no inference POST/spend or supplier contact.
[Evidence and limitations](first-pool-public-evidence-20261020.md) and
[machine-readable matrix](evidence/first-pool-public-20261020.json) contain
precise URLs, times, hashes, fixed candidate identities and scoped pending facts.
New three-row import/profile preserve old inputs and all thresholds/TTLs; no current
activation or formal qualification. Declared context is metadata, not SQL quality;
API canonical slug is not benchmark snapshot identity. latest grading is not evaluation.

- Existing focused first-pool/update/adapters/scores/qualification tests: 141 passed
  in 2.94 s, socket-denied fixtures. No source/test changes.
- `./scripts/check --quick`: locked offline dependencies, project records and Ruff
  passed; receipt `runtime/checks/e997cf19a41045f1b85365803c4aef3a/checks.json`.
- Offline evidence checker: 32 raw hashes,4 configuration digests,3 aggregate score
  recomputations, original/new report replay, evaluation-date blocking, 49 h future
  source expiry, local links and 257 unchanged tracked files passed. Two initial
  runtime checker import/setup errors were resolved without source changes and
  recorded separately. No failed model outcome was dropped.
- CLI prepare of the new public profile exited 1/blocked as expected; empty review
  and unresolved mapping/SQL/date evidence do not produce a qualified pool.
- Executable digest remains `0eeeef7cb03aecb605dfaf40961048ef8bcf0cc6eee847a5fb69f3357c2ed283`;
  prior 1,206 full tests remain historical reference, not a new full-suite run.

No S01-S08 review, RG01 changes, paid experiment, account API, supplier contact,
deployment, push, local commit or formal gate promotion. Raw public task details
remain ignored locally; only selected public facts/provenance are versioned.


## 2026-10-20 — Public-evidence local commit review

Reviewed the complete RG02/RG03 public-supplement and candidate-input diff against
archived local sources only. Fresh focused regression: 141 passed in 10.67 s.
Independent derivation confirms three 64-task aggregates and 80/110/74 included
rewards; original 32-response/receipt hashes and blocked/expiry behavior match.
[Precommit evidence](evidence/first-pool-public-precommit-20261020.json) records
actual offline checks, source identity, intended paths and preserved evidence.
No substantive defect found in this bounded document/data review; no supplier
statement was upgraded to a model qualification. Full 1,206-test result remains
historical, with unchanged executable digest. RG01 and formal registry stay frozen;
unknown evidence stays pending. New local Git commit is authorized; no API,
supplier contact, deployment or source push is authorized or performed.


## 2026-10-20 — Independent SQL model qualification v2

User explicitly approves separating source-native family/effort ranking priors from
exact API configuration SQL qualification. Added opt-in v2 profile policy, isolated
exam/grader, saved Client evidence normalization, private offline preparation/grading
scripts, version table and prospective API plan; V1 inputs/rules remain available.
No RG01 module/data, classifier tree, original experiment or formal registry edit.

Final `./scripts/check --integration`:1,281passed in337.12s, Ruff/dependencies/project
records, both pre-existing MockTransport demos and offline sdist/wheel passed.
Receipt: `runtime/checks/2c765bf6e8064a24ae5d9ce84b88505b/checks.json`.
[Sanitized evidence](evidence/sql-model-offline-20261020.json) binds current uncommitted
source digest and preserves baseline/state/plan, initial64pass/2fixturefail and
system-Python import setup failures; the final suite contains75new tests.
All50 reference controls agree with Python/SQLite and50semantic mutants fail;
capability/admission tests cover version/effort/provider changes, missing/altered/
expired/synthetic evidence, unknown costs, unrequested repair, prior-only metadata,
private preparation/no overwrite and real-fee priority without duplicate accounting.

Prepared actual-clock materials in ignored `runtime/sql-model-qualification-20261020/prepared-final/`.
Earlier explicit fixture-clock preparation is retained and is not a real model
execution timestamp. Original77selected RG01/profile/gate/trial artifacts match the
starting hashes. No paid/public API, contact, deployment, push or commit; real
model answer count0 and all candidate qualifications remain pending/blocked.

API plan is404maximum generations, classifier0, no automatic retries. Archived-rate
token scenarioUSD46.2263424 / proposed budgetUSD60 (25%scenario reserve fits);
unknown/additional fees and enforceable provider caps remain unresolved. Actual
execution needs new authorization and capture/budget preflight; this plan grants none.

## 2026-10-20 — Main-checkout sequential routing and SQL qualification integration

- Scope: apply reviewed isolated patch with new versions; preserve SQL qualification v2, frozen SQL semantics and original trial evidence. State/plan/coordination updated incrementally, prior main records archived.
- Full final check: `./scripts/check --integration`, **1,304 passed in 493.59s**, Ruff, dependency/record checks, two mocked high-route demos and sdist/wheel passed. Source digest: `057d6fbc5f15da1082298e651f91a5874ff4476e489d68d4a23d26a37e8f6c27`. Receipt: `runtime/checks/14b5123d5ff740609a6e6cf7c4f876c0/checks.json`.
- Additional three regressions explicitly check latest diagnostic/configuration, legacy manifest refusal before environment/POST, and archival v1 plan reporting with execution refusal. Dedicated sequential suite:22; preserved SQL qualification suite:75.
- Retained preliminary failures: mistyped test filename (no tests ran); focused139 passed/1 Q021 SQL worker timeout, isolated unchanged recheck1 passed; first full1291 passed/13 SQL worker/resource failures in794.25s, receipt `runtime/checks/7dcfbf3b30f2466594002d5e66f4e87d/checks.json`; unchanged focused recheck13 passed/100 deselected in37.60s. No SQL rule, resource limit or threshold change. Host swap observation is not proof of cause.
- Preserved132 protected files, including SQL worker/validator, exam/grader, qualification adapters, candidate profiles and historical evidence; formal registry unchanged. [Sanitized evidence](evidence/sequential-integration-offline-20261020.json); private logs under `runtime/sequential-integration-20261020/`.
- Real model requests0, API USD0; no hardware, supplier contact, deployment, push, local commit or qualification promotion. Mock results do not establish real-model savings, latency or correctness.
- Post-record closure: `./scripts/check --quick` passed, receipt `runtime/checks/6d6c4483d388406c89e490380bd36a28/checks.json`; current records report implementation_complete=True and production_qualification_recorded=False. Final scope audit confirms132 protected inputs unchanged,282 local links resolve, source digest matches the full run and Git HEAD is unchanged; `git diff --check` passed.

## 2026-10-20 — Sequential integration local commit review

- User authorizes local commit only; no new API, deployment or push. Baseline c6e53fdec9b403c8dc04e99c1e77d16f3f73d26f. Combined new route/SQL qualification source digest remains `057d6fbc5f15da1082298e651f91a5874ff4476e489d68d4a23d26a37e8f6c27`, matching the full1,304-test integration receipt; this is reuse of verified identical code, not a fresh full run.
- Two read-only Standards/Spec reviews found no blocking issue; Standards notes one optional test-name mismatch (both_attempts now asserts3), with no behavioral defect. Spec reviewer reran22 sequential tests:22 passed in0.82s.
- Original failed integration receipt,13-case unchanged recheck log, final integration log/receipt and earlier1,281-test SQL evidence are preserved and hashed. Runtime/private content is not staged. Independent release-readiness prose rewrite is preserved unstaged; only intended SQL-v2/sequential verification updates enter the commit.


## SQL-model capture runner and manual source workflow — 2026-10-20

`./scripts/check --integration`: **1,322 passed in517.76s**, Ruff, locked offline dependencies/records, both existing mocked high-route demos and sdist/wheel passed. Receipt: `runtime/checks/9cb9f51c1eb14235b080c8cec7e86981/checks.json`; [sanitized evidence](evidence/sql-runner-offline-20261020.json). New runner regressions:18passed in59.22s. A complete50x4 mock rehearsal additionally checks public repair, saved-answer interruption/resume, zeroPOST replay and explicit reconciliation before sealed grading;200/200 synthetic final outcomes,205mockPOSTs and1mockGET. API cost0; synthetic fees0.208 are not real spend or model performance.

Preserved failures include actual Client/normalizer encoding mismatch, repair JSON replay ordering, same-version provider role binding, completed-capture fee refresh, and the initial complete mock. First full check stopped during collection on existing SDK pydantic_core import; offline uv cache restored the missing native file after RECORD hash/size match, retaining the iCloud placeholder and original error. Final full run passed on unchanged source. No SQL limits, exam, formal gate or historical trial result changed. No API, source fetch, supplier contact, deployment, push or Git commit.


## SQL qualification runner local commit review — 2026-10-20

The user now authorizes review and local Git commit; earlier no-commit statements describe their capture time. Direct review found no new blocking issue. The current executable/test/build fingerprint exactly matches the preserved1,322-test full integration result; this review reuses that result, not a fresh full run. A fresh offline quick check passed. All621 files from the original failed mock and the original failure/fix receipts remain intact;136 of137 protected inputs are unchanged, with only the declared RG02 Client-encoding fixture correction. [Precommit evidence](evidence/sql-runner-precommit-20261020.json) records the checks and preservation. RG01 and formal gates remain unchanged/pending. Raw answers, prompts and receipts remain in ignored runtime; no API, supplier contact, deployment or push.

### 2026-10-21 category-qcs-v1 離線接入

23 類各自四池、類別資格、完整成本／速度 QCS 與 CategoryCore 已完成；類別由呼叫端明確指定。SQL 資格與 RG-01 保留；非 SQL 類別不要求通過 SQL 考試。開源候選可憑該類獨立資格入池，不偽造排行榜分數。價差不足不呼叫或預留 Jev；類別／矩陣／政策／驗收契約凍結，既有回答不重送。

最終現行測試 1,561 passed（明確排除保留的 `* 2.py` 舊副本，沒有新增全域忽略）；相關 226 項、Ruff、Git diff whitespace、23 類 mock 演練、既有 core/score 及兩種 QCS 示範、最終 sdist/wheel 皆通過。標準整體檢查診斷為 2,082 passed／70 failed，70 項皆是同批既有舊副本衝突；該診斷早於最後測試增補，與最終現行測試分列，不宣稱整體 gate 通過。原始收據、初次收集失敗與開源候選回歸 red/green 都保留。

[實作與使用](category-four-pool-routing.md)；[本輪證據](evidence/category-routing-offline-20261021.json)。沒有網路、模型 API、部署、推送、提交或刪除。正式模型／來源／完整成本與速度資格仍 pending；合成結果不是實測降本。


### 2026-10-21 本機提交前審查

審查補正候選隔離與身分核對邊界：非物件 SQL 資格資料只排除該候選，不中斷全矩陣或 QCS 接入／重播；未上榜開源候選的來源觀測不得用空模型 ID 通過身分核對。原始失敗及修正後 230 項回歸保留。

暫存內容匯出成獨立目錄，`./scripts/check --integration` 全部 1,538 項通過，Ruff、兩個既有 mock 示範與離線 sdist/wheel 通過；未設測試忽略，也未將原工作區的未追蹤舊副本搬入提交。先前 1,561 項、70 項舊副本失敗及所有原始資料保持原紀錄。

[提交內容驗證](evidence/category-routing-tree-precommit-20261021.json)與[原工作區回歸](evidence/category-routing-workspace-precommit-20261021.json)分列；兩者來源指紋不同，工作區狀態索引與提交索引分別指向各自證據。必要 QCS／費用預留／來源正規化相依項目納入；SQL 計畫、廣泛來源工作及舊副本留在工作區，原始 runtime 不入 Git。正式資格 pending；本輪無模型 API、部署或推送。

## 各類三候選備援離線實作 — 2026-10-21

已完成明示選用`qcs-backup-policy-v1`／`BackupCategoryCore`：23類各自四池、主1備2準備與缺額、status/uptime/latency健康資料、持久冷卻及受控恢復、同池429/5xx故障切換、全域生成／分類／工具限制、涵蓋所有可達模型的費用預留、原始期限與不重送續跑、實際profile核帳。非推理模型明示none/omit的transport與資格／費用契約已接入；原低池v1候選包與舊研究不直接改版或啟用。

**最終現行檔案1,769項通過，187項新增相關回歸通過，Ruff與Git diff whitespace通過。** 現行測試明示只在該次命令排除保留的`* 2.py`舊副本，沒有新增全域ignore或刪除。標準`./scripts/check --integration`仍是2,332 passed／70 failed；70個失敗ID與前次完全相同，皆在保留的舊副本，不能稱整體gate通過。兩個既有Mock示範、新23類主503→備援成功示範、離線sdist/wheel皆通過；新示範46個Mock生成POST、回讀新增POST 0。wheel含新模組，但仍保留8份舊source副本，不是整理完畢的正式發行artifact。

原始整合檢查曾因iCloud回收native pydantic_core檔而有2項collection error；核對安裝RECORD後，從既有離線uv快取補回缺失檔，沒有下載或刪除。兩項審閱P1的Mock重現原檔、修正與獨立複驗均保留；不合格／竄改候選與到期資格／校準會在新增POST前拒絕，既有已付費結果仍能離線重播。

[23類92池具名主備表](category-backup-coverage-20261021.md)完整列出276位置：3個有限SQL研究准入、132待核提案、15來源逾期待核、126明示缺額。新備援政策可啟用及production合格位置都仍0；來源同名、零報價、已有單例研究不能替代新版本完整路徑證據。雲端開源提案不是Mac Studio128GB硬體資格清單。

[使用與限制](category-backup-runtime.md)；[最終來源指紋與證據](evidence/category-backup-offline-20261021.json)。151份整合前程式／測試／腳本、3份狀態記錄雜湊均核對，12份SQL驗收／題庫相關來源保持不變。既有SQL資格工作、凍結50題原始答案與費用記錄保留；不沿舊manifest換規則。真實模型API、來源GET、供應商聯絡、部署、推送、本機Git提交、刪除均0。


### 2026-10-21 三候選備援本機提交前檢查

使用者已授權檢查與本機提交；先前「未提交」描述當時的擷取狀態。本次審閱另修正兩項P2：共用健康紀錄的事件身分加入收據根目錄，避免不同實際故障被誤判成重播；每次新分類派送前重新檢查候選健康，避免已知故障仍收分類費。新增2項回歸及獨立複驗通過，runtime／budget73項通過；原始重現保留。

實際提交內容匯出至獨立目錄，`./scripts/check --integration` **1754項全部通過**、Ruff、兩個既有Mock示範與離線sdist/wheel通過；未設定測試忽略，也未將舊副本納入提交。另跑原工作區現行檔案1771項，1770項先通過、1項工作依賴紀錄失敗；只補工作計畫依賴後，51項狀態／依賴回歸全部通過，沒有再改程式或掩蓋原始失敗。舊副本及先前70項失敗的原始紀錄保留。新版23類Mock備援46個生成請求，重播新增0；不是實際模型／速度／降本證據。

[提交內容驗證](evidence/category-backup-tree-precommit-20261021.json)與[原工作區驗證](evidence/category-backup-workspace-precommit-20261021.json)分開綁定各自來源指紋與狀態索引。SQL資格工作、研究版family-only規則、舊副本及1,944份原始實驗檔案保持原樣；研究資料與收據不入Git。正式資格保持pending；無模型API、來源GET、供應商聯絡、部署或推送。

### 2026-10-21：完成進度更正與SQL主備包工作順序

本次依已完成研究的最終報告及機器可讀統計，修正RG-07與下一步清單仍稱50題「進行中」的文字；較早48/50批次及工作區原有23/150歷史快照分開保留，不改原始成績。新增[SQL主備包清單](sql-backup-pool-readiness-plan-20261021.md)，明列離線前置、多候選資格接入、費用保障、真實量測／校準及最終啟用的依賴；未執行清單內的開發或模型測試。

僅做文件及既有證據離線核對；`./scripts/check --quick`通過，不宣稱新一輪完整pytest／整合或真實備援驗證。金額、連結、正式狀態與檔案保存結果見[本次文件證據](evidence/release-readiness-sql-plan-20261021.json)。SQL規則、資格工作、舊副本、原始結果及十項pending維持原樣；沒有模型API、來源GET、供應商聯絡、本機提交、部署或推送。


### 2026-10-21：放行索引與SQL主備包文件提交前審閱

只納入本次進度更正、SQL主備工作清單及其檢查證據；較早SQL資格／來源整理及編號副本留在原工作區。修正工作清單指向未追蹤研究文件的連結，改引已追蹤盤點；原研究文件與原始答案／收據不入提交。原文件證據保留其當時工作區hash，不冒充本次暫存內容；[提交前證據](evidence/release-readiness-sql-plan-precommit-20261021.json)另列兩種檔案指紋、引用檢查與保存結果。

本次是文件提交，不重新宣稱完整pytest／integration通過；離線quick、專案紀錄與放行結構檢查依實際收據記錄。十項資格保持pending，P0／P1／P2僅為待辦，未執行模型API、來源GET、供應商聯絡、部署或推送。


## SQL主備候選與多設定資格接入 — 2026-10-21

已完成[候選與證據對照](sql-backup-candidate-evidence-20261021.md)及[明示選用的新執行器](sql-multi-model-runner.md)。SQL專用低池作待驗提案，廣泛提案原序／來源保留；12位置共5個完整profile，僅3個符合單一回應身分格式。原Qwen／GLM aliases與Lightning provider-default不改寫、不冒充可派送；3個原研究設定核對原包metadata，但不沿用新備援流程／Jev校準。正式資格pending、派送／current啟用0。

新增49項回歸（候選30、多設定17、跨模組2）；舊runner／reasoning相容性32項另已通過，與完整套件數不相加。實際無忽略的`./scripts/check --integration`為2383 passed／70 failed；JUnit按檔案分類後，現行檔案1820 passed／0 failed，保留的編號副本563 passed／70 failed，70個失敗ID與既有紀錄完全相同。標準入口在pytest失敗處停止，不能稱全工作區integration通過；兩個既有Mock示範與離線sdist／wheel另行執行通過。只加JUnit輸出，沒有刪副本或加測試忽略。

完整新Mock演練5個唯一設定、6個slot、各50題，最終250/250控制答案通過；256次Mock POST含5次身分確認及1次公開補答。驗收中斷接續不重送，完成重播新增POST 0；1次明確Mock GET核帳後合成費用US$0.259，未知0，真實模型API費0。答案映射只在Mock考官／上游側，不進capture；合成成績不是真實模型能力。原始中斷、回應與判分保存在`runtime/sql-multi-runner-demo-20261021-worker/`，本輪彙整在`runtime/sql-backup-multicandidate-20261021/`。

原始檢查曾有測試預期舊例外類型及一個未用import；原始說明／紀錄保留，按新API更正測試與移除未用import後通過，沒有因此改驗收或資格規則。根據[本輪證據](evidence/sql-backup-multicandidate-offline-20261021.json)核對來源指紋、SQL凍結檔案、原始實驗、狀態與文件。這次沒有模型API、來源GET、供應商聯絡、硬體、部署、推送或本機Git提交。

收尾時狀態檢查拒絕描述文字型scope，已保留原始失敗與更正前證據，將scope改為與狀態相同的sql-backup-multicandidate-v1；只改metadata，沒有改程式／SQL規則或重跑模型。


### 2026-10-21 SQL主備候選本機提交前檢查

使用者已授權檢查與本機提交；上段「未提交」描述當時的擷取狀態。本次審閱修正1項P2：不同模型標籤若共用相同請求版本，不能在同池計作兩個備援；跨池或不同供應商仍可獨立考試。新增2項回歸，相關83項及狀態／依賴51項通過。完整實作共51項新增回歸。原始重現保留。

實際提交的程式／測試／建置內容匯出至獨立目錄，`./scripts/check --integration` **1,805項全部通過**，包含鎖定離線依賴、紀錄檢查、Ruff、兩個既有Mock示範與sdist／wheel。不是把工作區舊副本設為忽略；舊70項失敗與原始紀錄保留，沒有宣稱整個dirty工作區全通過。

第一次提交前檢查因尾端空白更正在pytest前段主動中斷；下一次保留1804 passed／1 failed，其中唯一失敗來自工作計畫責任移交與舊已完成任務的依賴衝突。已封存原實作計畫，當前計畫只列後續審閱，再從完整入口重跑通過；沒有修改檢查器、SQL驗收或資格規則。另保留一次console-script不能import scripts的收集失敗，改用既定python -m pytest入口後51項通過。

[提交內容驗證](evidence/sql-backup-multi-tree-precommit-20261021.json)與[原工作區相關回歸](evidence/sql-backup-multi-workspace-precommit-20261021.json)分開綁定來源指紋。404個非本輪檔案及4119個原始實驗檔案核對未變；SQL資格工作、舊副本、原始中斷／答案／收據保留，不入本次提交。RG-01凍結，十項正式資格仍pending；沒有模型API、來源GET、供應商聯絡、部署或推送。


## 2026-10-21：低池兩候選離線準備與凍結

已備妥[宣告身分、分層准入契約、112格費用缺證與測試計畫](sql-low-two-candidate-plan-20261021.md)。兩候選各50題、最多202次生成／零Jev，US$1為研究目標；實際回應身分、供應商完整上界及新的支出授權仍缺，未啟用模型池或條件式runtime准入adapter。單一模型量測可以保留錯題，整批路由holdout仍要求全對；舊嚴格資格與RG-01不變。

原工作區審閱前77項相容性回歸通過；精度修正後26項專屬回歸通過，獨立審閱複驗無剩餘P1／P2。當前交付內容獨立匯出後，完整`./scripts/check --integration`為 **1,831項通過**，包括Ruff、兩個既有Mock示範及離線sdist／wheel；沒有刪副本或設測試忽略。這不是整個dirty工作區通過；既有70項舊副本失敗紀錄仍保留。

初次系統Python／SQLite契約錯配、數值字串斷言差異、Decimal context可重現性P2及審核命令誤用base Python的失敗均保留；固定鎖定環境、數值比較及獨立Decimal Context後通過，沒有改SQL規則或保障門檻。[工作區證據](evidence/sql-low-two-preparation-offline-20261021.json)與[交付內容證據](evidence/sql-low-two-preparation-tree-20261021.json)分開綁定來源。418個非本輪檔案、4119個原始實驗檔案及19份先前驗證／失敗收據核對不變。

本輪模型API、來源GET、供應商聯絡、部署、推送及Git提交均0，正式資格維持pending。


## 2026-10-22：低池兩候選本機提交審閱

[本輪證據](evidence/sql-low-two-precommit-20261022.json)分開記錄重新執行的26項專屬回歸／紀錄檢查，以及逐檔核對後沿用的1,831項完整離線整合結果，沒有宣稱本輪重跑完整套件或整個dirty工作區全通過。程式、凍結計畫、SQL規則與正式資格均不變；原始失敗及實驗紀錄保留。本次僅本機提交，沒有模型／來源API、供應商聯絡、部署或推送。


## 2026-10-22 四槽／託管 Jev 本機提交審閱

本次只擷取四槽並行、完整流程費用預留、增量核帳及託管 Jev 的研究程式／測試，來源見 [scripts/research_parallel](../scripts/research_parallel/README.md)；原始外部研究程式、付費答案、帳本及中斷保留，不加入 Git。審閱補上快取身分交叉比對、殘缺／不可讀證據停止與單一回應緩衝區，共10項新增回歸。新版使用獨立v2 harness／plan識別，不修改舊凍結版本或SQL驗收。

[實作與限制](research-parallel-four-arm.md)、[擷取對照](evidence/research-parallel-four-arm-extraction-20261022.json)、[提交審閱驗證](evidence/research-parallel-four-arm-commit-review-20261022.json)分開保存。30分鐘仍是條件式容量推估；沒有新的真實正確率或節費結果。正式資格pending、付費入口停用。使用者本次僅授權本機提交，沒有付費請求、部署、推送或供應商聯絡；未把其他SQL、模型池、監控與檢查入口的dirty變更納入。

提交快照完整離線整合 **1,962項通過**（含131項研究回歸）、Ruff、兩個既有Mock示範及sdist／wheel建置通過；獨立200流程／550次Mock、4槽、重播0新增呼叫。21,823份原研究／題庫／前輪資料雜湊不變。此為提交子集，不代表整個dirty工作區或正式服務通過。
