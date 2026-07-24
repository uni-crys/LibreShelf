from dotenv import load_dotenv
load_dotenv()
# main.py
import asyncio
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import SQLModel
from contextlib import asynccontextmanager
from app.database import engine
from app.api import wishlist, books, auth
from app.services.readmoo_worker import import_readmoo_wishlist_to_db
from app.services.readmoo_library_worker import import_readmoo_library_to_db
from app.services.kobo_worker import import_kobo_wishlist_to_db
from app.services.kobo_library_worker import import_kobo_library_to_db
from app.services.metadata_pipeline import close_metadata_client

app = FastAPI(
    title="LibreShelf API",
    description="電子書與待購清單自動化管理系統",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 掛載各模組路由
app.include_router(wishlist.router, prefix="/wishlist", tags=["Wishlist"])
app.include_router(books.router, prefix="/books", tags=["Books"])
app.include_router(auth.router,prefix = "/auth", tags=["Auth"])

# 初始化背景排程器
scheduler = BackgroundScheduler()

def scheduled_sync_job():
    """
    定時自動同步任務 (例如每 24 小時執行一次)
    由於 Worker 內的 Playwright 函式是非同步 (async) 的，
    因此需透過 asyncio.run 在同步排程中執行它們。
    """
    print("[Scheduler] 開始執行定時自動同步任務...")
    
    # 預設使用者 ID，可依您的系統設計調整
    default_user_id = "default_user" 
    
    try:
        asyncio.run(import_readmoo_wishlist_to_db(default_user_id))
        asyncio.run(import_kobo_wishlist_to_db(default_user_id))
        print("[Scheduler] 定時自動同步任務執行完畢。")
    except Exception as e:
        print(f"[Scheduler] 定時自動同步執行失敗: {e}")

@app.post("/library/import")
async def import_library(
    user_id: str,
    limit: int | None = Query(default=None, ge=1, le=50),
):
    results = []
    workers = (
        ("readmoo", import_readmoo_library_to_db),
        ("kobo", import_kobo_library_to_db),
    )
    for platform, worker in workers:
        try:
            result = await worker(user_id, limit=limit)
            results.append(result or {
                "platform": platform,
                "status": "failed",
                "message": f"{platform} 同步未回傳結果",
                "new_books": 0,
            })
        except Exception as error:
            results.append({
                "platform": platform,
                "status": "failed",
                "message": str(error),
                "new_books": 0,
            })

    needs_auth = [
        result["platform"]
        for result in results
        if result["status"] == "auth_required"
    ]
    failed = [
        result["platform"]
        for result in results
        if result["status"] == "failed"
    ]

    if needs_auth:
        status = "auth_required"
        labels = [
            "Readmoo" if platform == "readmoo" else "Kobo"
            for platform in needs_auth
        ]
        message = f"{'、'.join(labels)} 登入憑證需要更新"
    elif failed:
        status = "partial_failure"
        labels = [
            "Readmoo" if platform == "readmoo" else "Kobo"
            for platform in failed
        ]
        message = f"{'、'.join(labels)} 同步失敗，其他平台結果已保留"
    else:
        status = "success"
        message = "Readmoo 與 Kobo 已購書櫃同步完成"

    return {
        "status": status,
        "message": message,
        "needs_auth": needs_auth,
        "results": results,
        "limit_per_platform": limit,
    }

@app.on_event("startup")
def startup_event():
    SQLModel.metadata.create_all(engine)
    print("[App] 資料庫表格初始化／驗證完成。")

    """FastAPI 啟動時觸發"""
    # 設定每 24 小時執行一次同步 (若要測試可先改為 minutes=5 或 hours=1)
    scheduler.add_job(scheduled_sync_job, "interval", hours=24)
    scheduler.start()
    print("[App] 背景排程器已成功啟動 (設定週期: 24小時)")

@app.on_event("shutdown")
async def shutdown_event():
    """FastAPI 關閉時觸發"""
    scheduler.shutdown()
    await close_metadata_client()
    print("[App] 背景排程器已正常關閉")

@app.get("/")
def root():
    return {"message": "Welcome to LibreShelf API is running!"}
