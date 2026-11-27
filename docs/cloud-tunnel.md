# 在這台電腦上跑雲端角色，經通道對外

第一階段的平台主機（#90）採用 Cloudflare 的快速通道：轉送服務跑在這台電腦上，`cloudflared` 從這台電腦主動連出到 Cloudflare，換到一個 `https://….trycloudflare.com` 的公開位址。不用註冊帳號，不用開路由器的埠，也不花錢。

**要先知道的三件事：**

- **位址每次啟動都會換。** 重新啟動後，要把新位址告訴各客戶端。
- **這台電腦要一直開著。** 關機或睡眠時，平台就連不上。
- **第一階段沒有授權檢查。** 拿到位址的人就能收發任何客戶端名稱的訊息。只在測試時開啟，測完就停。

## 安裝 cloudflared

需要 Cloudflare 官方的 `cloudflared`。這台 Mac 是 macOS 13，Homebrew 已不提供預先編好的版本，所以從官方 GitHub 下載執行檔。以下用 2026.10.7，Intel 版（`cloudflared-darwin-amd64.tgz`，約 22 MB）：

```bash
gh release download 2026.10.7 --repo cloudflare/cloudflared --pattern cloudflared-darwin-amd64.tgz --dir /tmp/cloudflared-dl
```

核對雜湊值，兩行要相同：

```bash
gh release view 2026.10.7 --repo cloudflare/cloudflared --json assets --jq '.assets[]|select(.name=="cloudflared-darwin-amd64.tgz")|.digest'
```

```bash
shasum -a 256 /tmp/cloudflared-dl/cloudflared-darwin-amd64.tgz
```

解壓到使用者目錄：

```bash
mkdir -p ~/.local/opt/cloudflared-2026.10.7 && tar -xzf /tmp/cloudflared-dl/cloudflared-darwin-amd64.tgz -C ~/.local/opt/cloudflared-2026.10.7
```

## 啟動、查狀態、停止

以下在 `cloudyfami-portal` 資料夾執行。`CLOUDFLARED` 指向剛才解壓的執行檔。

```bash
CLOUDFLARED=~/.local/opt/cloudflared-2026.10.7/cloudflared scripts/cloud-tunnel start
```

印出 `portal: https://….trycloudflare.com` 就是公開位址。

```bash
scripts/cloud-tunnel status
```

`public health check: ok` 代表從網際網路連得到。

```bash
scripts/cloud-tunnel stop
```

資料、紀錄與程序編號放在 `~/.local/share/cloudyfami-portal/`。可用 `CLOUDYFAMI_PORTAL_DATA` 換位置，`CLOUDYFAMI_PORTAL_PORT` 換本機埠，預設 8790。`scripts/cloud-tunnel logs` 看最後 50 行紀錄。

## 撤除

1. `scripts/cloud-tunnel stop`，公開位址立即失效。
2. 不再需要時刪掉 `~/.local/opt/cloudflared-2026.10.7`。
3. 不需要保留訊息紀錄時，刪掉 `~/.local/share/cloudyfami-portal/`。

快速通道沒有在 Cloudflare 留下任何設定，所以沒有其他要清的東西。
