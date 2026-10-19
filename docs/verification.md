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
