# LibreShelf 操作手冊

本手冊分成本機開發與 VPS 維運兩種情境。指令中的角括號內容需替換成自己的值；不要把 API key、Cookie、`state.json` 或 VPS 密碼貼進 Git、終端紀錄或聊天室。

## 常用位置

| 用途 | 位置 |
| --- | --- |
| 本機專案 | `/Users/crystal/VS code/Github/LibreShelf` |
| 本機後端 | `backend/` |
| 本機前端 | `fronted/` |
| VPS 專案 Git 工作目錄 | `/opt/libreshelf/app` |
| VPS Compose 設定 | `/opt/libreshelf/deploy` |
| VPS 持久化資料 | `/opt/libreshelf/data` |

## 本機開發

### 進入專案

```bash
cd "/Users/crystal/VS code/Github/LibreShelf"
```

### 第一次安裝後端

```bash
cd "/Users/crystal/VS code/Github/LibreShelf"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
python -m playwright install chromium
```

Readmoo 登入與同步預設使用 Playwright Chromium。若該機器已安裝正式版
Google Chrome，並希望只在該機器切換瀏覽器，可在 `backend/.env` 設定：

```dotenv
READMOO_BROWSER_CHANNEL=chrome
```

在 `backend/.env` 設定本機用的 `GOOGLE_BOOKS_API_KEY`。這個檔案不可提交。

### 本機 Readmoo 同步 agent（上傳結果到 VPS）

Readmoo Cookie 一律留在 Mac；agent 不會上傳 `state.json`。它只把本機已解析的書櫃與待購資料傳到 VPS。

先以 [`backend/.env.example`](../backend/.env.example) 為參考，在本機 `backend/.env` 加入：

```dotenv
READMOO_SYNC_TOKEN=<長且隨機的共享密鑰>
READMOO_SYNC_VPS_URL=https://<你的-tailscale-serve-網址>
```

VPS backend 也必須設定相同的 `READMOO_SYNC_TOKEN` 環境變數；設定後可在容器內安全確認是否存在（只會輸出 `True` 或 `False`）：

```bash
cd /opt/libreshelf/deploy
sudo docker compose exec backend python -c \
'from app.config import settings; print(bool(settings.READMOO_SYNC_TOKEN))'
```

從本機執行同步。首次建議只同步書櫃，避免在 Readmoo 待購清單路徑尚未確認前改動 VPS 待購資料：

```bash
cd "/Users/crystal/VS code/Github/LibreShelf/backend"
source ../.venv/bin/activate
PYTHONPATH=. python scripts/sync_readmoo_to_vps.py \
  --user-id test_user_001 \
  --limit 3 \
  --skip-wishlist
```

待購清單解析確認後，移除 `--skip-wishlist`。只有本機待購同步回傳 `success` 時，agent 才會將 Readmoo 待購 snapshot 上傳並在 VPS 執行雙向對帳；失敗時 VPS 既有待購資料會保留。

`--limit 3` 是首次測試建議值，限制本次新增到本機資料庫並上傳的新書數；確認正確後可移除 `--limit` 做完整同步。

### 啟動後端

開一個終端機：

```bash
cd "/Users/crystal/VS code/Github/LibreShelf"
source .venv/bin/activate
cd backend
python3 -m uvicorn main:app
```

後端健康檢查：

```bash
curl http://127.0.0.1:8000/
```

本機資料庫會建立在 `backend/data/ebooks.db`；登入狀態會建立在 `backend/user_profiles/`。兩者都屬於本機私有資料。

### 第一次安裝與啟動前端

另開一個終端機：

```bash
cd "/Users/crystal/VS code/Github/LibreShelf/fronted"
npm ci
npm run dev
```

開啟 `http://localhost:3000`。Vite 會將 `/api` 代理到本機後端 `http://localhost:8000`。

### 停止服務

在各自啟動後端／前端的終端機按 `Ctrl+C`。離開 Python 虛擬環境可使用：

```bash
deactivate
```

### 本機測試與建置檢查

```bash
cd "/Users/crystal/VS code/Github/LibreShelf"
source .venv/bin/activate
PYTHONPATH=backend python -m unittest discover -s backend/tests

cd fronted
npm run build
```

### 本機常用 API 測試

以下使用目前測試帳號；改成自己的 `user_id` 即可。

```bash
# 平台登入狀態
curl -s "http://127.0.0.1:8000/auth/status?user_id=test_user_001"

# 同步已購書櫃，每個平台最多測 3 本
curl -X POST \
  "http://127.0.0.1:8000/library/import?user_id=test_user_001&limit=3"

# 觸發兩平台待購清單同步；Readmoo 被 blocked 時不要執行
curl -X POST \
  "http://127.0.0.1:8000/wishlist/import?user_id=test_user_001"
```

## Git 日常流程

修改完成後，先測試再提交：

```bash
cd "/Users/crystal/VS code/Github/LibreShelf"
git status --short
git diff

git add <檔案路徑>
git commit -m "簡短說明"
git push
```

不要提交 `.env`、`ebooks.db`、`state.json`、`user_profiles/` 或 `node_modules/`。

## 連線至 VPS

在 Mac 終端機使用 OVH 的公開 IPv4：

```bash
ssh ubuntu@<VPS_PUBLIC_IPV4>
```

若已設定可用的 Tailscale SSH／私有連線，也可使用 Tailscale IP 或主機名稱：

```bash
ssh ubuntu@<TAILSCALE_IP_OR_HOSTNAME>
```

登入後可確認身分與網路：

```bash
whoami
hostname
tailscale status
```

## VPS 部署與維運

### 更新已推送的程式

先在本機完成 `git push`，再登入 VPS：

```bash
cd /opt/libreshelf/app
git status --short
git pull

cd /opt/libreshelf/deploy
sudo docker compose up -d --build
sudo docker compose ps
```

若 `git pull` 表示 VPS 有手動修改，先看差異，**不要**直接強制覆蓋：

```bash
cd /opt/libreshelf/app
git diff
git stash push -m "temporary-vps-change"
git pull
git stash list
```

確認新版正常後再決定是否需要套回 stash；不要未檢查就執行 `git stash pop`。

### 檢查容器與日誌

```bash
cd /opt/libreshelf/deploy

sudo docker compose ps
sudo docker compose logs --tail=100 backend
sudo docker compose logs -f --tail=100 backend
sudo docker compose logs --tail=100 frontend
```

離開追蹤日誌請按 `Ctrl+C`；容器仍會在背景繼續運行。

健康檢查：

```bash
curl -i http://127.0.0.1:3000/api/
```

重新啟動既有容器：

```bash
cd /opt/libreshelf/deploy
sudo docker compose restart
```

避免使用 `docker compose down -v`，它會移除 volume，可能造成資料庫或登入狀態遺失。

### 查看平台登入狀態

```bash
curl -s \
  "http://127.0.0.1:3000/api/auth/status?user_id=test_user_001" \
  | python3 -m json.tool
```

狀態意義：

| 狀態 | 意義與動作 |
| --- | --- |
| `active` | 最近一次驗證／同步成功，可使用。 |
| `unverified` | 有 Cookie 但尚未完成平台驗證，請登入或同步測試。 |
| `expired` / `auth_required` | Cookie 已失效或沒有 state，需重新登入。 |
| `blocked` | 平台 WAF／安全驗證拒絕；立刻停止重試，等待冷卻。 |
| `parser_error` | 登入未必失效，但平台 HTML/API 格式無法解析；查看 backend 日誌。 |

### 透過 noVNC 手動登入

1. 在瀏覽器開啟已設定的 Tailscale／反向代理 noVNC 路徑（通常為 `/novnc/`）。
2. 在 VPS 呼叫一次登入 API：

```bash
curl -X POST \
  "http://127.0.0.1:3000/api/auth/login?user_id=test_user_001&platform=kobo"
```

Readmoo 將 `platform=kobo` 改為 `platform=readmoo`。

3. 立刻切換到 noVNC 的 Chromium，完成平台登入。
4. 等 curl 回傳結果後，以 `/api/auth/status` 確認為 `active`。

Readmoo 若回傳「安全驗證拒絕」或畫面顯示 `Max challenge attempts exceeded`，不要再按登入、不要觸發同步；等平台冷卻後再做一次人工登入。

### 單獨同步 Kobo 待購清單

Readmoo 被封鎖時，可只測 Kobo，避免任何 Readmoo 請求：

```bash
cd /opt/libreshelf/deploy

sudo docker compose exec backend python -c \
'import asyncio; from app.services.kobo_worker import import_kobo_wishlist_to_db; print(asyncio.run(import_kobo_wishlist_to_db("test_user_001")))'
```

成功後日誌會列出本次遠端書籍數與移除的舊同步項目數。

### 持久化資料與權限

VPS 的資料位於 `/opt/libreshelf/data`，包含 SQLite 資料庫和 `user_profiles`。檔案可能由容器 root 使用者建立；手動移動檔案時使用明確路徑與 `sudo`，例如：

```bash
sudo mv \
  /opt/libreshelf/data/user_profiles/test_user_001/readmoo/state.json \
  /opt/libreshelf/data/user_profiles/test_user_001/readmoo/state.backup.json
```

這是可復原的備份式移動。不要刪除整個 `/opt/libreshelf/data` 目錄。

## 快速故障對照

| 現象 | 先做什麼 |
| --- | --- |
| `ModuleNotFoundError: apscheduler` | 更新程式並重建 backend；`backend/requirements.txt` 已列入 APScheduler。 |
| Docker socket permission denied | 指令前加 `sudo`，或重新登入讓 docker 群組設定生效。 |
| frontend build 失敗 | 在 `fronted/` 執行 `npm ci` 後再 `npm run build`。 |
| 平台顯示 `blocked` | 停止該平台的一切登入與同步，僅查看狀態／日誌。 |
| Kobo 遠端已刪除，頁面仍有卡片 | 執行 Kobo-only 同步；成功的雙向對帳會清除已同步但遠端不存在的 Kobo 項目。 |
| `git pull` 被本機修改擋住 | 先 `git diff`，再使用 `git stash push`；不要強制覆蓋未檢查的修改。 |
