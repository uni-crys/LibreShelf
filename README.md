# Librovia・自由書閣

Librovia 是一套自架的個人電子書管理工具，將 Readmoo 讀墨與
Rakuten Kobo 的已購書櫃、待購清單及書籍資訊集中在同一個介面。

> 此專案仍在開發中，目前以前端內建的單一測試使用者運作，尚未提供完整的
> 多使用者登入與權限系統。請勿直接暴露在公開網路。

## 功能

- 整合 Readmoo 與 Kobo 已購書櫃
- 匯入、搜尋及依平台／分類篩選藏書
- 集中管理兩個平台的待購清單
- 顯示平台登入憑證及同步狀態
- 透過 Playwright 開啟可互動的登入瀏覽器並保存 session
- 從博客來、Readmoo、國家圖書館、Google Books 與 Open Library
  交叉補齊書名、作者、封面及分類
- 匯入時重新嘗試補齊「未知作者」、「未分類」或缺少封面的既有書籍
- metadata 來源失敗時提供重試、冷卻與候選資料評分
- 使用 SQLite 儲存藏書、購買紀錄與待購清單

## 技術架構

| 元件 | 技術 |
| --- | --- |
| 前端 | React、Vite、Tailwind CSS、Axios |
| 後端 | FastAPI、SQLModel、APScheduler |
| 瀏覽器自動化 | Playwright |
| 資料庫 | SQLite（WAL 模式） |
| Metadata | Google Books、Open Library、國家圖書館及書店資料 |

```text
Browser
   │
   ├── React / Vite (:3000)
   │
   └── FastAPI (:8000)
          ├── SQLite
          ├── Metadata providers
          └── Playwright ── Readmoo / Kobo
```

## 開始使用

### 系統需求

- Python 3.11 或更新版本
- Node.js 18 或更新版本
- npm
- macOS、Linux，或其他 Playwright 支援的環境

### 1. 取得專案

```bash
git clone https://github.com/uni-crys/Librovia.git
cd Librovia
```

### 2. 設定後端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium
cp backend/.env.example backend/.env
```

編輯 `backend/.env`：

```dotenv
# 選填；未設定時仍會嘗試其他 metadata 來源
GOOGLE_BOOKS_API_KEY=

# 本機同步代理與後端共用的長隨機字串
READMOO_SYNC_TOKEN=

# 選填：使用已安裝的正式版 Chrome
READMOO_BROWSER_CHANNEL=chrome

# 選填：只套用於 Readmoo 瀏覽器，例如家用網路的 SOCKS proxy
READMOO_BROWSER_PROXY=
```

啟動 API：

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

API 啟動後可開啟：

- 檢查服務：<http://127.0.0.1:8000/>
- Swagger API 文件：<http://127.0.0.1:8000/docs>

### 3. 設定前端

另開一個終端機：

```bash
cd fronted
npm ci
npm run dev
```

前往 <http://localhost:3000>。

如後端不在 `http://localhost:8000`，請在啟動前端時指定：

```bash
VITE_API_BASE_URL=https://your-private-api.example npm run dev
```

> 專案目前的前端目錄名稱是 `fronted/`，請依照實際名稱輸入指令。

## 基本操作

1. 進入「同步狀態」。
2. 為 Readmoo 與 Kobo 分別執行登入。
3. 在 Playwright 開啟的瀏覽器中，於三分鐘內完成平台登入。
4. 回到「藏書間」執行書櫃匯入。
5. 系統會加入新書，並重新補抓既有但資訊不完整的書籍。

登入資訊會保存在 `backend/user_profiles/`，資料庫位於
`backend/data/ebooks.db`。兩者均已被 Git 忽略，請勿手動提交。

## 測試

執行後端測試：

```bash
cd backend
python -m unittest discover -s tests
```

驗證前端可建置：

```bash
cd fronted
npm run build
```


## 專案結構

```text
Librovia/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI 路由
│   │   ├── services/     # 平台同步、登入與 metadata pipeline
│   │   ├── database.py
│   │   └── models.py
│   ├── scripts/          # 本機登入及同步輔助工具
│   ├── tests/
│   ├── main.py
│   └── requirements.txt
├── fronted/
│   └── src/              # React 前端
└── README.md
```

## 注意事項

- 平台網站改版後，選擇器或登入流程可能需要更新。
- 請遵守 Readmoo、Kobo 與各 metadata 來源的服務條款及使用限制。
- 請勿提交 `.env`、Cookie、Playwright storage state、資料庫或 API key。
- 大量同步前建議先備份資料庫，並避免短時間反覆觸發平台登入。
