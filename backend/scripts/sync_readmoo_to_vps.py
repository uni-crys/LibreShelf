"""Synchronize Readmoo locally, then upload the result to the VPS.

Run from the backend directory:
    PYTHONPATH=. python scripts/sync_readmoo_to_vps.py --user-id test_user_001

The local state.json stays on this computer.  Only book and wishlist metadata
is sent to the configured VPS endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from sqlmodel import Session, select

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")

from app.database import engine  # noqa: E402
from app.models import Book, Purchase, WishlistItem  # noqa: E402
from app.services.readmoo_library_worker import (  # noqa: E402
    import_readmoo_library_to_db,
)
from app.services.readmoo_worker import import_readmoo_wishlist_to_db  # noqa: E402


def _serialize_book(book: Book, platform_book_id: str | None = None) -> dict:
    return {
        "isbn": book.isbn,
        "title": book.title,
        "author": book.author,
        "cover_url": book.cover_url,
        "category": book.category,
        "platform_book_id": platform_book_id,
    }


def build_snapshot(user_id: str, include_wishlist: bool) -> dict:
    with Session(engine) as db:
        purchases = db.exec(
            select(Purchase).where(
                Purchase.user_id == user_id,
                Purchase.platform == "readmoo",
            )
        ).all()
        books = []
        for purchase in purchases:
            book = db.get(Book, purchase.isbn)
            if book:
                books.append(_serialize_book(book, purchase.platform_book_id))

        wishlist = []
        if include_wishlist:
            items = db.exec(
                select(WishlistItem).where(
                    WishlistItem.user_id == user_id,
                    WishlistItem.platform == "readmoo",
                )
            ).all()
            for item in items:
                book = db.get(Book, item.isbn)
                if book:
                    wishlist.append(_serialize_book(book))

    return {
        "user_id": user_id,
        "books": books,
        "wishlist_synced": include_wishlist,
        "wishlist": wishlist,
    }


async def upload_snapshot(snapshot: dict, vps_url: str, token: str) -> dict:
    endpoint = f"{vps_url.rstrip('/')}/api/internal/readmoo-snapshot"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            endpoint,
            json=snapshot,
            headers={"X-LibreShelf-Sync-Token": token},
        )
        response.raise_for_status()
        return response.json()


async def run(user_id: str, skip_wishlist: bool) -> int:
    vps_url = os.getenv("READMOO_SYNC_VPS_URL", "").strip()
    sync_token = os.getenv("READMOO_SYNC_TOKEN", "").strip()
    if not vps_url or not sync_token:
        print(
            "缺少 READMOO_SYNC_VPS_URL 或 READMOO_SYNC_TOKEN；"
            "請在 backend/.env 設定。",
            file=sys.stderr,
        )
        return 2

    library_result = await import_readmoo_library_to_db(user_id)
    print("[Local Readmoo Agent] 書櫃結果:", library_result)
    if library_result.get("status") != "success":
        print("[Local Readmoo Agent] 書櫃未成功，不上傳不完整 snapshot")
        return 1

    wishlist_result = None
    wishlist_synced = False
    if not skip_wishlist:
        wishlist_result = await import_readmoo_wishlist_to_db(user_id)
        wishlist_synced = wishlist_result.get("status") == "success"
        print("[Local Readmoo Agent] 待購結果:", wishlist_result)
        if not wishlist_synced:
            print("[Local Readmoo Agent] 待購未成功；保留 VPS 既有待購資料")

    snapshot = build_snapshot(user_id, include_wishlist=wishlist_synced)
    print(
        "[Local Readmoo Agent] 準備上傳: "
        f"書櫃 {len(snapshot['books'])} 本、"
        f"待購 {'已同步' if wishlist_synced else '略過'}"
    )
    try:
        result = await upload_snapshot(snapshot, vps_url, sync_token)
    except httpx.HTTPError as error:
        print(f"[Local Readmoo Agent] 上傳 VPS 失敗: {error}", file=sys.stderr)
        return 1
    print("[Local Readmoo Agent] VPS 結果:", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在本機同步 Readmoo，再安全上傳結果到 VPS",
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--skip-wishlist",
        action="store_true",
        help="僅同步並上傳 Readmoo 書櫃，不碰待購清單",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.user_id, args.skip_wishlist))


if __name__ == "__main__":
    raise SystemExit(main())
