"""Safe reconciliation of locally cached platform wishlists."""

from sqlmodel import Session, select

from app.models import Book, WishlistItem


def _normalized_title(value: str | None) -> str:
    """Use titles as a fallback because Kobo ProductId is not always an ISBN."""
    return "".join(
        character.casefold()
        for character in (value or "")
        if character.isalnum()
    )


def remove_stale_synced_wishlist_items(
    db: Session,
    user_id: str,
    platform: str,
    remote_books: list[dict],
) -> int:
    """Remove only confirmed-synced items absent from a successful remote import.

    Pending/failed items are deliberately retained: they may be waiting for an
    add/remove action and must not disappear merely because a remote import ran.
    """
    remote_identifiers = {
        str(book.get("isbn") or "").strip()
        for book in remote_books
    }
    remote_titles = {
        _normalized_title(str(book.get("title") or ""))
        for book in remote_books
    }
    remote_titles.discard("")

    synced_items = db.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == user_id,
            WishlistItem.platform == platform,
            WishlistItem.sync_status == "synced",
        )
    ).all()

    removed = 0
    for item in synced_items:
        book = db.get(Book, item.isbn)
        title = _normalized_title(book.title if book else "")
        if item.isbn in remote_identifiers or (title and title in remote_titles):
            continue
        db.delete(item)
        removed += 1
    return removed
