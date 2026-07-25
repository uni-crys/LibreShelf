"""Authenticated endpoint for a local Readmoo sync agent.

The agent sends book metadata and wishlist records, never browser storage or
cookies.  It is intended to be reachable only over the user's Tailscale URL.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import Book, Purchase, WishlistItem
from app.services.platform_auth import set_platform_session_status
from app.services.wishlist_reconciliation import (
    remove_stale_synced_wishlist_items,
)

router = APIRouter(tags=["Readmoo replication"])


class ReadmooBookPayload(BaseModel):
    isbn: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=1000)
    author: str | None = Field(default=None, max_length=500)
    cover_url: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=255)
    platform_book_id: str | None = Field(default=None, max_length=255)


class ReadmooSnapshotPayload(BaseModel):
    user_id: str = Field(min_length=1, max_length=255)
    books: list[ReadmooBookPayload] = Field(default_factory=list)
    wishlist_synced: bool = False
    wishlist: list[ReadmooBookPayload] = Field(default_factory=list)


def _require_sync_token(
    supplied_token: str | None = Header(
        default=None,
        alias="X-LibreShelf-Sync-Token",
    ),
) -> None:
    expected_token = settings.READMOO_SYNC_TOKEN
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Readmoo 本機同步尚未設定伺服器 Token",
        )
    if not supplied_token or not secrets.compare_digest(
        supplied_token,
        expected_token,
    ):
        raise HTTPException(status_code=401, detail="Readmoo 同步 Token 無效")


def _is_useful(
    value: str | None,
    unknown_values: set[str] | None = None,
) -> bool:
    return bool(
        value
        and value.strip()
        and value.casefold() not in (unknown_values or set())
    )


def _upsert_book(db: Session, payload: ReadmooBookPayload) -> Book:
    book = db.get(Book, payload.isbn)
    if book is None:
        book = Book(
            isbn=payload.isbn,
            title=payload.title,
            author=payload.author or "未知作者",
            cover_url=payload.cover_url,
            category=payload.category or "未分類",
        )
        db.add(book)
        return book

    if _is_useful(payload.title):
        book.title = payload.title
    if _is_useful(payload.author, {"未知作者", "unknown", "unkown"}):
        book.author = payload.author
    if _is_useful(payload.category, {"未分類", "unknown", "unkown"}):
        book.category = payload.category
    if _is_useful(payload.cover_url):
        book.cover_url = payload.cover_url
    db.add(book)
    return book


def apply_readmoo_snapshot(
    db: Session,
    payload: ReadmooSnapshotPayload,
) -> dict:
    """Apply a local snapshot without ever receiving a login credential."""
    purchases_added = 0
    for item in payload.books:
        _upsert_book(db, item)
        existing = db.exec(
            select(Purchase).where(
                Purchase.user_id == payload.user_id,
                Purchase.platform == "readmoo",
                Purchase.isbn == item.isbn,
            )
        ).first()
        if existing is None:
            db.add(Purchase(
                user_id=payload.user_id,
                platform="readmoo",
                platform_book_id=item.platform_book_id or item.isbn,
                isbn=item.isbn,
            ))
            purchases_added += 1

    wishlist_removed = 0
    if payload.wishlist_synced:
        for item in payload.wishlist:
            _upsert_book(db, item)
            existing = db.exec(
                select(WishlistItem).where(
                    WishlistItem.user_id == payload.user_id,
                    WishlistItem.platform == "readmoo",
                    WishlistItem.isbn == item.isbn,
                )
            ).first()
            if existing is None:
                db.add(WishlistItem(
                    user_id=payload.user_id,
                    platform="readmoo",
                    isbn=item.isbn,
                    sync_status="synced",
                ))
            else:
                existing.sync_status = "synced"
                db.add(existing)
        wishlist_removed = remove_stale_synced_wishlist_items(
            db,
            payload.user_id,
            "readmoo",
            [item.model_dump() for item in payload.wishlist],
        )

    db.commit()
    set_platform_session_status(payload.user_id, "readmoo", "active")
    return {
        "books_received": len(payload.books),
        "purchases_added": purchases_added,
        "wishlist_received": len(payload.wishlist) if payload.wishlist_synced else None,
        "wishlist_removed": wishlist_removed if payload.wishlist_synced else None,
    }


@router.post("/readmoo-snapshot", dependencies=[Depends(_require_sync_token)])
def receive_readmoo_snapshot(
    payload: ReadmooSnapshotPayload,
    db: Session = Depends(get_session),
):
    result = apply_readmoo_snapshot(db, payload)
    return {
        "status": "success",
        "message": "已接收本機 Readmoo 同步結果",
        **result,
    }
