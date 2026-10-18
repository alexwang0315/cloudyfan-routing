# 評分快照更新與操作紀錄（T03）

更新使用既有來源 adapter、資格審查包及 SnapshotStore。沒有安裝排程、通知、實讀來源或呼叫模型；下列步驟供營運者審核啟用。

## 一次更新

1. 保存原有 qualification pack 與 snapshot ID，將新來源資料另存不可覆寫的 observation 檔。`fetch`／`import-codexradar` 延用原命令與存取限制。來源失敗時保存命令結束碼與經去敏的錯誤類型，停止，不用舊資料冒充更新。
2. 使用 `qualify` 產生新的審查包。新觀測／來源版本／模型設定的輸入雜湊改變，舊核定不能沿用；未知、過期與缺少池仍阻擋。保存前後兩份完整 pack，`input_sha256` 可核對兩份完整觀測、registry 與 policy。
3. 產出 preview，核對入池新增／移除、分群改變、資格排除原因、來源／版本／metric／policy ID、pack 與快照 ID。
4. 核定後使用明確 operation ID 發布；原子 current 更新只影響新任務。
5. 檢查操作紀錄與新的 current。例行錯誤不得觸發付費請求、無聲降級、放寬模型池或另選來源。

```sh
cloudyfan-scores update-preview --previous previous-pack.json --proposed next-pack.json \
  --output update-preview.json
cloudyfan-scores update --previous previous-pack.json --proposed next-pack.json \
  --store runtime/scores-coding --operation coding-20261018-01
cloudyfan-scores update-inspect --store runtime/scores-coding --operation coding-20261018-01
```

初次發布才省略 `--previous`；這會要求 current 尚未存在。合成 fixture 加 `--synthetic`，狀態仍非正式核定；此旗標不授權網路請求。`update-preview` 不寫 store；`update` 只有 `published` 結束碼 0，`blocked`／`failed` 為 1。`update-inspect` 讀取原紀錄，不重新執行來源抓取、核定或發布。

## 格式與並行保護

公開 API：`preview_update(previous_pack, proposed_pack, now=..., allow_synthetic=False)`；`ScoreUpdater(store).apply(operation_id, previous_pack, proposed_pack, now=..., allow_synthetic=False)`；`inspect(operation_id)`。

Preview 的 `added`／`removed` 是合格三池成員變化，`tier_changes` 是仍合格設定的分群變化，`qualification_changes` 保留 eligible 與排除原因的轉變。`before`／`after` 連結輸入及資格包 hash、來源版本與預覽快照；`proposed_snapshot_id` 是通過核定後準備發布的實際快照 ID，未通過時為 null。預覽不把未知候選降為低分，也不會替人員核定設定。

操作紀錄位於 store 的 `updates/<operation_id>.json`，包含固定輸入身分、attempts、pending/published/blocked/failed、前後差異、expected current／目標 snapshot ID 與 history。失敗紀錄用 `phase:ExceptionType`，不保存可能含憑證的任意例外文字。輸入及 synthetic 旗標不同時不得重用同一 operation ID。已發布結果重跑直接回傳同一紀錄；失敗可用相同輸入重試，保留先前失敗歷史。修改核定／資料後使用新 operation ID。

Updater 的鎖序列化同一 store 的更新操作；SnapshotStore 在既有發布鎖內新增 `expected_current_id` 比對，防止在 preview 之後另一寫入者已更新 current 而被覆蓋。第一個成功者保留，落後更新記錄衝突，須讀新 current 後重新審查。沒有改寫來源 adapter 或 snapshot 檔案格式／原子替換機制。

發布前先保存 pending 及目標 ID。若 current 已換到同一目標但最後 audit 寫入中斷，原 operation 可復原 published，無須重新切換 current。若 current 已是不同更新，不強行覆蓋。若磁碟連第一份 pending 都無法寫入，操作直接失敗且不嘗試發布；若最後 audit 無法落盤，呼叫者會收到錯誤，原 pending 留供續跑。不能把未落盤 audit 宣稱已確認完成。

## 任務綁定與效期

`ScoreCore` 新任務從有效 current 綁定；原任務與續跑使用原 snapshot ID，不因更新中途改模型。過期快照不能開新任務，已存在 manifest 的原任務仍依原規則續跑。Updater 比對舊核定包時使用原建立時間，只為核對原 snapshot ID；新快照發布仍以本次時間檢查效期。

## 啟用責任清單（本單未部署）

- [ ] 指定來源／條款與金鑰存取核對人；AA／OpenRouter authenticated access、CodexRadar 遠端能力未驗證時不得標完成。
- [ ] 指定領域、門檻、覆蓋率、期限及模型資格核定者；禁止 holdout 調參。
- [ ] 指定排程所有人、執行帳號、時區、頻率、離線命令與來源檔保留位置；現在沒有已啟用背景工作。
- [ ] 限制檔案權限，保留前後完整資格包、來源檔、preview、operation audit；避免與私人 prompt／收據混存。
- [ ] 驗證排程不重疊及 operation ID 規則；重試只能相同輸入，核定內容改變必須新 ID。
- [ ] 配置來源失敗、current conflict、資格阻擋、磁碟錯誤、audit pending 的人工處理責任與通知政策；此票沒有發送通知。
- [ ] 模擬過期 current：停止新任務；不自動使用不合格候選，也不修改續跑中的已凍結模型。
- [ ] 真正啟用來源存取／排程須另外授權及驗證；離線 fixture 不代表已部署。
