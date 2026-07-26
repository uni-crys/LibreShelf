# Mac 本機 Readmoo 憑證與同步指南

Readmoo 的登入 Cookie 只保留在 Mac。本機使用正常網路登入並爬取資料後，再把書櫃／待購清單結果上傳到 VPS；**不會把 Cookie 或 `state.json` 傳到 VPS**。

## 何時需要執行

- Readmoo 顯示本機憑證過期。
- 新買書後，想更新手機上的 LibreShelf 書櫃。
- 想手動更新 Readmoo 待購清單。

VPS 顯示「本機已同步」是正常狀態，代表資料已由 Mac 更新，但 VPS 不會直接登入 Readmoo。

## 第一次確認設定

本機 [`backend/.env`](../backend/.env) 必須有：

```dotenv
READMOO_SYNC_TOKEN=<與 VPS 相同的同步 Token>
READMOO_SYNC_VPS_URL=https://<你的 Tailscale Serve 網址>
```

Mac 必須已安裝 Tailscale，並使用與 VPS 相同帳號登入。不要將 Token、`.env`、Cookie 或 `state.json` 提交到 Git。

## 更新本機 Readmoo 憑證

### 1. 啟動本機後端

開一個 Mac 終端機：

```bash
cd "/Users/crystal/VS code/Github/LibreShelf"
source .venv/bin/activate

cd backend
python3 -m uvicorn main:app
```

後端保持運行。

### 2. 開啟登入視窗

另開一個終端機：

```bash
curl -X POST \
  "http://127.0.0.1:8000/auth/login?user_id=test_user_001&platform=readmoo"
```

在跳出的 Chromium 內完成 Readmoo 登入。成功時 curl 會回傳：

```json
{"status":"success","platform":"readmoo",...}
```

此時本機會更新：

```text
backend/user_profiles/test_user_001/readmoo/state.json
```

若畫面顯示安全驗證拒絕，停止重試並稍後再試。

## 上傳同步結果到 VPS

### 首次或只同步書櫃

```bash
cd "/Users/crystal/VS code/Github/LibreShelf/backend"
source ../.venv/bin/activate

PYTHONPATH=. python3 scripts/sync_readmoo_to_vps.py \
  --user-id test_user_001 \
  --limit 3 \
  --skip-wishlist
```

`--limit 3` 是安全測試用，限制本次新增書籍數。確認無誤後可移除 `--limit`，做完整書櫃同步。

### 同步書櫃與待購清單

待購清單解析已確認可用後，移除 `--skip-wishlist`：

```bash
PYTHONPATH=. python3 scripts/sync_readmoo_to_vps.py \
  --user-id test_user_001
```

只有待購清單本機同步明確回傳 `success` 時，VPS 才會用這份 snapshot 做 Readmoo 待購清單雙向對帳；若待購失敗，VPS 原有待購資料會保留。

## 成功判斷

最後應看到：

```text
[Local Readmoo Agent] VPS 結果: {'status': 'success', ...}
```

之後在手機或 Mac 瀏覽 VPS 網頁，Readmoo 狀態會顯示「本機已同步」。

## 停止本機後端

回到執行 `uvicorn` 的終端機，按 `Ctrl+C`，再執行：

```bash
deactivate
```
