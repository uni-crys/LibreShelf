# backend/app/services/worker_utils.py

from sqlmodel import Session, select
from app.database import engine
from app.models import WishlistItem

def mark_session_expired(user_id: str, platform: str):
    """統一處理平台 Session 過期狀態更新"""
    try:
        with Session(engine) as db:
            statement = select(WishlistItem).where(
                WishlistItem.user_id == user_id,
                WishlistItem.platform == platform
            )
            items = db.exec(statement).all()
            for item in items:
                item.sync_status = "failed"
            db.commit()
            print(f"[{platform.capitalize()}] Session 已過期，更新狀態完成")
    except Exception as e:
        print(f"[worker_utils] 更新失敗: {e}")