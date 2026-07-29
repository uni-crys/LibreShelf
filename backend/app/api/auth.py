# app/api/auth.py
import os
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import Session, select
from pydantic import BaseModel, Field
from app.database import get_session  # 依您專案中的實例調整 (若為 get_db_session 請維持原樣)
from app.models import PlatformSession
from app.services.platform_auth import (
    PlatformLoginBlocked,
    PlatformLoginTimeout,
    get_platform_auth_cookies,
    login_and_save_platform_state,
)

router = APIRouter(tags = ["Auth"])

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SUPPORTED_PLATFORMS = ("readmoo", "kobo")

class CookieUploadSchema(BaseModel):
    user_id: str = Field(..., example="user_01")
    platform: str = Field(..., example="readmoo", description="限定 'readmoo' 或 'kobo'")
    cookies: list[dict] = Field(..., description="接收自書籤腳本導出的 Cookie 陣列")


@router.post("/login")
async def login_platform(user_id: str, platform: str):
    """開啟平台登入視窗，登入成功後直接儲存該使用者的 Playwright state。"""
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail="不支援的電子書平台")
    try:
        return await login_and_save_platform_state(user_id, platform)
    except PlatformLoginBlocked as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except PlatformLoginTimeout as error:
        raise HTTPException(status_code=408, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"更新 {platform} 登入憑證失敗：{error}",
        ) from error

@router.post("/upload-cookies")
async def upload_cookies(payload: CookieUploadSchema, db: Session = Depends(get_session)):
    """接收前端書籤腳本貼上的 Cookie，轉化為 Playwright 識別的儲存狀態 (Storage State)"""
    if payload.platform not in ["readmoo", "kobo"]:
        raise HTTPException(status_code=400, detail="不支援的電子書平台")

    # 1. 建立該用戶於該平台的獨立資料夾目錄
    profile_dir = BASE_DIR / "user_profiles" / payload.user_id / payload.platform
    os.makedirs(profile_dir, exist_ok=True)
    state_file = profile_dir / "state.json"

    # 2. 轉換為 Playwright 標準 storage_state 格式
    cleaned_cookies = []
    for c in payload.cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("key")
        value = c.get("value")

        if name and value is not None:
            cleaned_cookie = {
                "name": str(name),
                "value": str(value),
                "domain": str(c.get("domain", f".{payload.platform}.com")),
                "path": str(c.get("path", "/")),
                "expires": float(c.get("expires")) if c.get("expires") is not None else -1,
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": str(c.get("sameSite", "Lax"))
            }
            cleaned_cookies.append(cleaned_cookie)  
            
    if not cleaned_cookies:
        raise HTTPException(
            status_code=400, 
            detail="傳入的 Cookie 資料無效。每筆 Cookie 必須包含 'name'（或 'key'）與 'value' 欄位。"
        )

    storage_state = {
        "cookies": cleaned_cookies,
        "origins": []
    }

    # 3. 寫入實體 JSON 檔案
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(storage_state, f, indent=2, ensure_ascii=False)
    except IOError:
        raise HTTPException(status_code=500, detail="寫入儲存狀態檔案失敗")

    # 4. 更新資料庫中的 Session 狀態
    statement = select(PlatformSession).where(
        PlatformSession.user_id == payload.user_id,
        PlatformSession.platform == payload.platform
    )
    db_session = db.exec(statement).first()
    
    if not db_session:
        db_session = PlatformSession(
            user_id=payload.user_id,
            platform=payload.platform,
            status="unverified",
            updated_at=datetime.utcnow()
        )
        db.add(db_session)
    else:
        db_session.status = "unverified"
        db_session.updated_at = datetime.utcnow()
        
    db.commit()

    return {"status": "success", "message": f"{payload.platform} 的 Session Cookie 已成功清洗並綁定。"}


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _inspect_platform_state(
    user_id: str,
    platform: str,
    db_record: PlatformSession | None,
) -> dict:
    state_file = (
        BASE_DIR / "user_profiles" / user_id / platform / "state.json"
    )
    base = {
        "platform": platform,
        "status": "missing",
        "needs_update": True,
        "last_updated": None,
        "expires_at": None,
        "cookie_count": 0,
        "message": "尚未建立登入憑證",
    }
    database_status = db_record.status if db_record else None

    def remote_synced_status() -> dict:
        last_updated = base["last_updated"]
        if db_record and db_record.updated_at:
            last_updated = db_record.updated_at.replace(
                tzinfo=timezone.utc,
            ).isoformat()
        return {
            **base,
            "status": "remote_synced",
            "needs_update": False,
            "last_updated": last_updated,
            "message": "資料已由本機 Readmoo 同步；VPS 不直接使用登入憑證",
        }

    if not state_file.exists():
        if database_status == "blocked":
            return {
                **base,
                "status": "blocked",
                "message": "平台安全驗證拒絕登入，請暫停重試並稍後再試",
            }
        if database_status == "remote_synced":
            return remote_synced_status()
        return base

    base["last_updated"] = _iso_from_timestamp(state_file.stat().st_mtime)
    try:
        with state_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {
            **base,
            "status": "invalid",
            "message": "憑證檔案無法讀取，請重新登入更新",
        }

    cookies = [
        cookie for cookie in payload.get("cookies", [])
        if isinstance(cookie, dict)
        and cookie.get("name")
        and cookie.get("value") is not None
    ]
    base["cookie_count"] = len(cookies)
    if not cookies:
        if database_status == "remote_synced":
            return remote_synced_status()
        return {
            **base,
            "status": "invalid",
            "message": "憑證中沒有有效 Cookie，請重新登入更新",
        }

    auth_cookies = get_platform_auth_cookies(cookies, platform)
    if not auth_cookies:
        if database_status == "remote_synced":
            return remote_synced_status()
        return {
            **base,
            "status": "expired",
            "needs_update": True,
            "message": "找不到平台登入 Cookie，請重新登入更新",
        }

    now = time.time()
    future_expirations = [
        float(cookie["expires"])
        for cookie in auth_cookies
        if isinstance(cookie.get("expires"), (int, float))
        and float(cookie["expires"]) > now
    ]
    has_session_cookie = any(
        cookie.get("expires") in (None, -1, 0, "-1", "0")
        for cookie in auth_cookies
    )
    has_valid_cookie = has_session_cookie or bool(future_expirations)
    if future_expirations:
        base["expires_at"] = _iso_from_timestamp(min(future_expirations))

    if database_status == "remote_synced":
        return remote_synced_status()

    if not has_valid_cookie:
        return {
            **base,
            "status": "expired",
            "needs_update": True,
            "message": "登入憑證已失效，請重新登入更新 Cookie",
        }

    if database_status == "blocked":
        return {
            **base,
            "status": "blocked",
            "needs_update": True,
            "message": "平台安全驗證拒絕登入，請暫停重試並稍後再試",
        }

    if database_status == "expired":
        return {
            **base,
            "status": "expired",
            "needs_update": True,
            "message": "登入憑證已失效，請重新登入更新 Cookie",
        }

    if database_status == "parser_error":
        return {
            **base,
            "status": "parser_error",
            "needs_update": False,
            "message": "登入憑證存在，但平台頁面格式無法確認；請稍後重新檢查",
        }

    if database_status != "active":
        return {
            **base,
            "status": "unverified",
            "needs_update": True,
            "message": "憑證尚未通過最近一次平台驗證，請重新登入或同步測試",
        }

    return {
        **base,
        "status": "active",
        "needs_update": False,
        "message": "登入憑證目前可供同步使用",
    }


@router.get("/status")
def get_platform_status(
    user_id: str,
    db: Session = Depends(get_session),
):
    records = db.exec(
        select(PlatformSession).where(PlatformSession.user_id == user_id)
    ).all()
    records_by_platform = {
        record.platform.lower(): record for record in records
    }
    platforms = [
        _inspect_platform_state(
            user_id,
            platform,
            records_by_platform.get(platform),
        )
        for platform in SUPPORTED_PLATFORMS
    ]
    return {
        "user_id": user_id,
        "needs_update": any(
            platform["needs_update"] for platform in platforms
        ),
        "platforms": platforms,
    }

@router.post("/sync-from-local")
async def sync_from_local(user_id: str, platform: str, db: Session = Depends(get_session)):
    """直接讀取專案根目錄下的 {platform}_state.json，清洗並寫入多用戶目錄中"""
    if platform not in ["readmoo", "kobo"]:
        raise HTTPException(status_code=400, detail="不支援的電子書平台")

    # 1. 定義根目錄舊檔的路徑
    old_state_path = BASE_DIR / f"{platform}_state.json" # 視您的專案結構調整
    print("[Debug] 正在尋找舊憑證檔")
    # 如果舊檔和 main.py 在同一層，也可以寫成 BASE_DIR / f"{platform}_state.json"

    if not old_state_path.exists():
        raise HTTPException(
            status_code=404, 
            detail=f"在根目錄找不到 {platform}_state.json，請先執行登入腳本產生檔案。"
        )

    # 2. 讀取舊檔
    try:
        with open(old_state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取舊憑證檔案失敗: {str(e)}")

    # 3. 進行 Cookie 清洗與標準化
    cookies = data.get("cookies", [])
    cleaned_cookies = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("key")
        value = c.get("value")

        if name and value is not None:
            cleaned_cookie = {
                "name": str(name),
                "value": str(value),
                "domain": str(c.get("domain", f".{platform}.com")),
                "path": str(c.get("path", "/")),
                "expires": float(c.get("expires")) if c.get("expires") is not None else -1,
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": str(c.get("sameSite", "Lax"))
            }
            cleaned_cookies.append(cleaned_cookie)

    if not cleaned_cookies:
        raise HTTPException(status_code=400, detail="根目錄的憑證檔內沒有有效的 Cookie 資料")

    # 4. 建立新目標目錄並寫入
    profile_dir = BASE_DIR / "user_profiles" / user_id / platform
    os.makedirs(profile_dir, exist_ok=True)
    state_file = profile_dir / "state.json"

    storage_state = {
        "cookies": cleaned_cookies,
        "origins": data.get("origins", [])
    }

    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(storage_state, f, indent=2, ensure_ascii=False)
    except IOError:
        raise HTTPException(status_code=500, detail="寫入多用戶狀態檔案失敗")

    # 5. 更新資料庫 PlatformSession 狀態
    statement = select(PlatformSession).where(
        PlatformSession.user_id == user_id,
        PlatformSession.platform == platform
    )
    db_session = db.exec(statement).first()
    
    if not db_session:
        db_session = PlatformSession(
            user_id=user_id,
            platform=platform,
            status="unverified",
            updated_at=datetime.utcnow()
        )
        db.add(db_session)
    else:
        db_session.status = "unverified"
        db_session.updated_at = datetime.utcnow()
        
    db.commit()

    return {"status": "success", "message": f"已成功將根目錄的 {platform} 憑證同步至使用者 {user_id} 的資料夾！"}
